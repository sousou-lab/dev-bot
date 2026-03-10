from __future__ import annotations

import unittest

from app.agent_sdk_client import (
    AgentContextOverloadError,
    AgentForbiddenToolError,
    AgentJsonResponseError,
    AgentOversizedReadError,
    AgentRateLimitError,
    AgentResult,
    ClaudeAgentClient,
    _extract_context_overload_error,
    _extract_oversized_read_error,
    _extract_rate_limit_error,
    _extract_usage_limit_message,
)
from app.planning_agent import PLAN_SYSTEM_PROMPT, READ_ONLY_TOOLS, TEST_PLAN_SYSTEM_PROMPT


class StubClaudeAgentClient(ClaudeAgentClient):
    def __init__(self, responses: list[AgentResult]) -> None:
        super().__init__(api_key="dummy")
        self._responses = responses
        self.prompts: list[str] = []

    def run_text(self, *args, **kwargs):  # type: ignore[override]
        self.prompts.append(str(kwargs.get("prompt", "")))
        return self._responses.pop(0)


class ClaudeAgentClientTests(unittest.TestCase):
    def test_json_response_retries_after_non_json_text(self) -> None:
        client = StubClaudeAgentClient(
            [
                AgentResult(result="Let me explore the codebase directly.", session_id="sess_a"),
                AgentResult(result='{"goal":"ok"}', session_id="sess_b"),
            ]
        )

        result = client.json_response("system", "prompt", prompt_kind="plan")

        self.assertEqual({"goal": "ok"}, result)
        self.assertEqual(2, len(client.prompts))
        self.assertIn("前の応答は無効です", client.prompts[1])

    def test_json_response_with_meta_normalizes_byte_session_id(self) -> None:
        client = StubClaudeAgentClient(
            [
                AgentResult(result='{"goal":"ok"}', session_id=b"sess_bytes"),  # type: ignore[arg-type]
            ]
        )

        result = client.json_response_with_meta("system", "prompt", prompt_kind="plan")

        self.assertEqual({"goal": "ok"}, result.payload)
        self.assertEqual("sess_bytes", result.session_id)

    def test_json_response_raises_structured_error_after_retry_failure(self) -> None:
        client = StubClaudeAgentClient(
            [
                AgentResult(result="not json", session_id="sess_a", stderr=["stderr1"]),
                AgentResult(result="still not json", session_id="sess_b", stderr=["stderr2"]),
            ]
        )

        with self.assertRaises(AgentJsonResponseError) as ctx:
            client.json_response("system", "prompt", prompt_kind="test_plan")

        self.assertEqual("test_plan", ctx.exception.prompt_kind)
        self.assertEqual("sess_b", ctx.exception.session_id)
        self.assertEqual("still not json", ctx.exception.raw_response)
        self.assertEqual(["stderr1", "stderr2"], ctx.exception.stderr)

    def test_json_response_raises_forbidden_tool_error_with_tool_details(self) -> None:
        client = StubClaudeAgentClient(
            [
                AgentResult(
                    result="",
                    session_id="sess_forbidden",
                    stderr=[
                        '2026-03-09T13:30:54.771Z [DEBUG] Hook PreToolUse (callback) returned permissionDecision: deny (reason: `ToolSearch` is disabled during planning)',
                        "2026-03-09T13:30:54.772Z [DEBUG] Hook denied tool use for ToolSearch",
                    ],
                )
            ]
        )

        with self.assertRaises(AgentForbiddenToolError) as ctx:
            client.json_response("system", "prompt", prompt_kind="plan")

        self.assertEqual("plan", ctx.exception.prompt_kind)
        self.assertEqual("sess_forbidden", ctx.exception.session_id)
        self.assertEqual("ToolSearch", ctx.exception.tool_name)
        self.assertIn("disabled during planning", ctx.exception.reason)

    def test_json_response_retry_prompt_is_planning_specific(self) -> None:
        client = StubClaudeAgentClient(
            [
                AgentResult(result="not json", session_id="sess_a"),
                AgentResult(result='{"goal":"ok"}', session_id="sess_b"),
            ]
        )

        client.json_response("system", "prompt", prompt_kind="plan")

        self.assertIn("追加のツール探索は禁止です", client.prompts[1])
        self.assertIn("Write / Edit / Bash は禁止です", client.prompts[1])

    def test_json_response_retries_once_after_retryable_forbidden_tool_in_planning(self) -> None:
        client = StubClaudeAgentClient(
            [
                AgentResult(
                    result="",
                    session_id="sess_a",
                    stderr=["2026-03-10T02:47:35.835Z [DEBUG] Write tool permission denied"],
                ),
                AgentResult(result='{"goal":"ok"}', session_id="sess_b", stderr=["retry-ok"]),
            ]
        )

        result = client.json_response("system", "prompt", prompt_kind="plan")

        self.assertEqual({"goal": "ok"}, result)
        self.assertEqual(2, len(client.prompts))
        self.assertIn("禁止ツール `Write`", client.prompts[1])

    def test_extract_rate_limit_error_parses_request_id_and_message(self) -> None:
        parsed = _extract_rate_limit_error(
            [
                '2026-03-09T15:15:21.352Z [ERROR] API error (attempt 1/11): 429 429 {"type":"error","error":{"type":"rate_limit_error","message":"This request would exceed your account\'s rate limit. Please try again later."},"request_id":"req_011CYsj3yc9jtLoGkguBUjnF"}'
            ]
        )

        self.assertEqual(
            ("req_011CYsj3yc9jtLoGkguBUjnF", "This request would exceed your account's rate limit. Please try again later."),
            parsed,
        )

    def test_rate_limit_error_is_structured_exception(self) -> None:
        error = AgentRateLimitError(
            "rate limited",
            stderr=["line1"],
            prompt_kind="plan",
            request_id="req_123",
        )

        self.assertEqual(["line1"], error.stderr)
        self.assertEqual("plan", error.prompt_kind)
        self.assertEqual("req_123", error.request_id)

    def test_extract_usage_limit_message_parses_claude_limit_error(self) -> None:
        parsed = _extract_usage_limit_message("You've hit your limit · resets 1pm (Asia/Tokyo)")

        self.assertEqual("You've hit your limit · resets 1pm (Asia/Tokyo)", parsed)

    def test_planning_prompts_do_not_depend_on_skill_tool(self) -> None:
        self.assertEqual(
            ["Read", "Grep", "Glob", "ToolSearch", "Agent", "Skill", "WebSearch", "TodoWrite", "AskUserQuestion"],
            READ_ONLY_TOOLS,
        )
        self.assertIn("planning エージェント", PLAN_SYSTEM_PROMPT)
        self.assertIn("plan.json というファイルを保存しない", PLAN_SYSTEM_PROMPT)
        self.assertIn("test planning エージェント", TEST_PLAN_SYSTEM_PROMPT)
        self.assertIn("test_plan.json というファイルを保存しない", TEST_PLAN_SYSTEM_PROMPT)

    def test_extract_oversized_read_error_parses_token_counts(self) -> None:
        parsed = _extract_oversized_read_error(
            [
                "2026-03-09T17:48:55.438Z [DEBUG] Read tool error (517ms): File content (35186 tokens) exceeds maximum allowed tokens (25000)."
            ]
        )

        self.assertEqual(("35186", "25000"), parsed)

    def test_json_response_raises_oversized_read_error(self) -> None:
        client = StubClaudeAgentClient(
            [
                AgentResult(
                    result="",
                    session_id="sess_oversized",
                    stderr=[
                        "2026-03-09T17:48:55.438Z [DEBUG] Read tool error (517ms): File content (35186 tokens) exceeds maximum allowed tokens (25000)."
                    ],
                )
            ]
        )

        with self.assertRaises(AgentOversizedReadError) as ctx:
            client.json_response("system", "prompt", prompt_kind="plan")

        self.assertEqual("plan", ctx.exception.prompt_kind)
        self.assertEqual("sess_oversized", ctx.exception.session_id)
        self.assertEqual("35186", ctx.exception.observed_tokens)
        self.assertEqual("25000", ctx.exception.max_tokens)

    def test_extract_context_overload_error_parses_peak_tokens_and_reads(self) -> None:
        parsed = _extract_context_overload_error(
            [
                "2026-03-09T23:45:35.661Z [DEBUG] autocompact: tokens=31600 threshold=167000 effectiveWindow=180000",
                "2026-03-09T23:45:35.359Z [DEBUG] executePreToolHooks called for tool: Read",
                "2026-03-09T23:45:42.741Z [DEBUG] autocompact: tokens=52696 threshold=167000 effectiveWindow=180000",
                "2026-03-09T23:45:42.371Z [DEBUG] executePreToolHooks called for tool: Read",
                "2026-03-09T23:45:50.658Z [DEBUG] executePreToolHooks called for tool: Read",
            ]
        )

        self.assertEqual(("52696", 3), parsed)

    def test_json_response_raises_context_overload_error_on_empty_response(self) -> None:
        client = StubClaudeAgentClient(
            [
                AgentResult(
                    result="",
                    session_id="sess_a",
                    stderr=[
                        "2026-03-09T23:45:35.359Z [DEBUG] executePreToolHooks called for tool: Read",
                        "2026-03-09T23:45:42.371Z [DEBUG] executePreToolHooks called for tool: Read",
                    ],
                ),
                AgentResult(
                    result="",
                    session_id="sess_b",
                    stderr=[
                        "2026-03-09T23:45:50.658Z [DEBUG] executePreToolHooks called for tool: Read",
                        "2026-03-09T23:45:42.741Z [DEBUG] autocompact: tokens=52696 threshold=167000 effectiveWindow=180000",
                    ],
                ),
            ]
        )

        with self.assertRaises(AgentContextOverloadError) as ctx:
            client.json_response("system", "prompt", prompt_kind="plan")

        self.assertEqual("plan", ctx.exception.prompt_kind)
        self.assertEqual("sess_b", ctx.exception.session_id)
        self.assertEqual("52696", ctx.exception.peak_tokens)
        self.assertEqual(3, ctx.exception.read_count)

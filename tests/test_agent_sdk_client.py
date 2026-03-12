from __future__ import annotations

import sys
import types
import unittest
from unittest.mock import patch

from app.agent_sdk_client import (
    AgentBufferOverflowError,
    AgentContextOverloadError,
    AgentForbiddenToolError,
    AgentJsonResponseError,
    AgentOversizedReadError,
    AgentRateLimitError,
    AgentResult,
    ClaudeAgentClient,
    _build_options,
    _extract_api_error_details,
    _extract_buffer_overflow_error,
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
        self.assertEqual("test_plan", ctx.exception.diagnostics["prompt_kind"])
        self.assertEqual(2, len(ctx.exception.diagnostics["response_attempts"]))

    def test_json_response_raises_forbidden_tool_error_with_tool_details(self) -> None:
        client = StubClaudeAgentClient(
            [
                AgentResult(
                    result="",
                    session_id="sess_forbidden",
                    stderr=[
                        "2026-03-09T13:30:54.771Z [DEBUG] Hook PreToolUse (callback) returned permissionDecision: deny (reason: `ToolSearch` is disabled during planning)",
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

    def test_json_response_with_meta_records_debug_attempts(self) -> None:
        client = StubClaudeAgentClient([AgentResult(result='{"goal":"ok"}', session_id=b"sess_bytes")])
        debug_events: list[dict[str, object]] = []

        client.json_response_with_meta(
            "system",
            "prompt",
            prompt_kind="plan",
            debug_recorder=debug_events.append,
            debug_context={"phase": "plan"},
        )

        self.assertEqual(1, len(debug_events))
        self.assertEqual("plan", debug_events[0]["prompt_kind"])
        self.assertEqual("plan", debug_events[0]["phase"])
        self.assertEqual(0, debug_events[0]["attempt_index"])
        self.assertEqual(b"sess_bytes", debug_events[0]["session_id"])

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
            (
                "req_011CYsj3yc9jtLoGkguBUjnF",
                "This request would exceed your account's rate limit. Please try again later.",
            ),
            parsed,
        )

    def test_extract_api_error_details_parses_overloaded_message(self) -> None:
        parsed = _extract_api_error_details(
            [
                '2026-03-12T08:25:20.000Z [ERROR] API error (attempt 1/3): 529 {"type":"error","error":{"type":"overloaded_error","message":"Overloaded"}}'
            ]
        )

        self.assertEqual(("overloaded", "Overloaded"), parsed)

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
        self.assertIn("recommended_direction は高レベルな方向づけ", PLAN_SYSTEM_PROMPT)
        self.assertIn("repo の実態調査より優先してはいけない", PLAN_SYSTEM_PROMPT)
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

    def test_extract_buffer_overflow_error_reports_likely_tool_source(self) -> None:
        parsed = _extract_buffer_overflow_error(
            "Failed to decode JSON: JSON message exceeded maximum buffer size of 5242880 bytes",
            [
                "2026-03-09T23:45:35.359Z [DEBUG] executePreToolHooks called for tool: Read",
            ],
            max_buffer_size=5 * 1024 * 1024,
        )

        self.assertEqual((5 * 1024 * 1024, "tool_output", "last_tool=Read"), parsed)

    def test_agent_buffer_overflow_error_keeps_diagnostics(self) -> None:
        error = AgentBufferOverflowError(
            "overflow",
            stderr=["line1"],
            prompt_kind="plan",
            max_buffer_size=5 * 1024 * 1024,
            likely_source="tool_output",
            source_detail="last_tool=Read",
        )

        self.assertEqual(["line1"], error.stderr)
        self.assertEqual("plan", error.prompt_kind)
        self.assertEqual(5 * 1024 * 1024, error.max_buffer_size)
        self.assertEqual("tool_output", error.likely_source)
        self.assertEqual("last_tool=Read", error.source_detail)

    def test_build_options_sets_max_buffer_size(self) -> None:
        fake_module = types.ModuleType("claude_agent_sdk")

        class FakeOptions:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

        fake_module.ClaudeAgentOptions = FakeOptions

        with patch.dict(sys.modules, {"claude_agent_sdk": fake_module}):
            with patch("app.agent_sdk_client._resolve_claude_cli", return_value="/tmp/claude"):
                with patch("app.agent_sdk_client._claude_version", return_value="1.0.0"):
                    with patch("app.agent_sdk_client._claude_preflight", return_value={"ok": True}):
                        options, _ = _build_options(
                            api_key="dummy",
                            system="system",
                            cwd="/tmp/workspace",
                            max_turns=2,
                            max_buffer_size=5 * 1024 * 1024,
                            allowed_tools=["Read"],
                            permission_mode="default",
                            setting_sources=[],
                            hooks=None,
                            agents=None,
                            output_schema={"type": "object"},
                        )

        self.assertEqual(5 * 1024 * 1024, options.kwargs["max_buffer_size"])

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

    def test_json_response_empty_response_includes_attempt_diagnostics(self) -> None:
        client = StubClaudeAgentClient(
            [
                AgentResult(
                    result="",
                    session_id="sess_a",
                    stderr=[
                        '2026-03-12T08:24:41.950Z [ERROR] API error (attempt 1/2): 529 {"type":"error","error":{"type":"overloaded_error","message":"Overloaded"}}'
                    ],
                    diagnostics={
                        "final_result_present": True,
                        "final_stop_reason": "end_turn",
                        "final_is_error": False,
                        "final_result_length": 0,
                        "final_structured_output_present": False,
                        "assistant_text_block_count": 1,
                        "assistant_text_total_length": 12,
                        "event_trace": ["assistant", "result"],
                    },
                ),
                AgentResult(
                    result="",
                    session_id="sess_b",
                    stderr=["retry stderr"],
                    diagnostics={
                        "final_result_present": True,
                        "final_stop_reason": "end_turn",
                        "final_is_error": False,
                        "final_result_length": 0,
                        "final_structured_output_present": False,
                        "assistant_text_block_count": 0,
                        "assistant_text_total_length": 0,
                        "event_trace": ["result"],
                    },
                ),
            ]
        )

        with self.assertRaises(AgentJsonResponseError) as ctx:
            client.json_response("system", "prompt", prompt_kind="test_plan")

        self.assertEqual("overloaded", ctx.exception.diagnostics["api_error_class"])
        self.assertEqual("Overloaded", ctx.exception.diagnostics["api_error_message"])
        self.assertEqual(
            [
                {
                    "retry_attempt": 0,
                    "session_id": "sess_a",
                    "final_result_present": True,
                    "final_stop_reason": "end_turn",
                    "final_is_error": False,
                    "final_result_length": 0,
                    "final_structured_output_present": False,
                    "assistant_text_block_count": 1,
                    "assistant_text_total_length": 12,
                    "event_trace": ["assistant", "result"],
                },
                {
                    "retry_attempt": 1,
                    "session_id": "sess_b",
                    "final_result_present": True,
                    "final_stop_reason": "end_turn",
                    "final_is_error": False,
                    "final_result_length": 0,
                    "final_structured_output_present": False,
                    "assistant_text_block_count": 0,
                    "assistant_text_total_length": 0,
                    "event_trace": ["result"],
                },
            ],
            ctx.exception.diagnostics["response_attempts"],
        )

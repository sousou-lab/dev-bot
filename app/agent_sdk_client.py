from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import threading
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class AgentResult:
    result: str
    structured_output: Any = None
    stderr: list[str] | None = None
    session_id: str | None = None
    total_cost_usd: float | None = None
    usage: dict[str, Any] | None = None


@dataclass(frozen=True)
class AgentDebugInfo:
    cli_path: str
    cli_version: str
    auth_status: dict[str, Any] | None
    env_snapshot: dict[str, str]
    preflight: dict[str, Any] | None = None


@dataclass(frozen=True)
class AgentJsonEnvelope:
    payload: dict[str, Any]
    session_id: str | None = None
    stderr: list[str] | None = None


class AgentJsonResponseError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        raw_response: str = "",
        stderr: list[str] | None = None,
        session_id: str | None = None,
        prompt_kind: str | None = None,
    ) -> None:
        super().__init__(message)
        self.raw_response = raw_response
        self.stderr = stderr or []
        self.session_id = session_id
        self.prompt_kind = prompt_kind


class AgentForbiddenToolError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        tool_name: str = "",
        reason: str = "",
        stderr: list[str] | None = None,
        session_id: str | None = None,
        prompt_kind: str | None = None,
    ) -> None:
        super().__init__(message)
        self.tool_name = tool_name
        self.reason = reason
        self.stderr = stderr or []
        self.session_id = session_id
        self.prompt_kind = prompt_kind


class AgentTimeoutError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        stderr: list[str] | None = None,
        session_id: str | None = None,
        prompt_kind: str | None = None,
    ) -> None:
        super().__init__(message)
        self.stderr = stderr or []
        self.session_id = session_id
        self.prompt_kind = prompt_kind


class AgentRateLimitError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        stderr: list[str] | None = None,
        session_id: str | None = None,
        prompt_kind: str | None = None,
        request_id: str = "",
    ) -> None:
        super().__init__(message)
        self.stderr = stderr or []
        self.session_id = session_id
        self.prompt_kind = prompt_kind
        self.request_id = request_id


class AgentOversizedReadError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        stderr: list[str] | None = None,
        session_id: str | None = None,
        prompt_kind: str | None = None,
        observed_tokens: str = "",
        max_tokens: str = "",
    ) -> None:
        super().__init__(message)
        self.stderr = stderr or []
        self.session_id = session_id
        self.prompt_kind = prompt_kind
        self.observed_tokens = observed_tokens
        self.max_tokens = max_tokens


class AgentBufferOverflowError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        stderr: list[str] | None = None,
        session_id: str | None = None,
        prompt_kind: str | None = None,
        max_buffer_size: int = 0,
        likely_source: str = "",
        source_detail: str = "",
    ) -> None:
        super().__init__(message)
        self.stderr = stderr or []
        self.session_id = session_id
        self.prompt_kind = prompt_kind
        self.max_buffer_size = max_buffer_size
        self.likely_source = likely_source
        self.source_detail = source_detail


class AgentContextOverloadError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        stderr: list[str] | None = None,
        session_id: str | None = None,
        prompt_kind: str | None = None,
        peak_tokens: str = "",
        read_count: int = 0,
    ) -> None:
        super().__init__(message)
        self.stderr = stderr or []
        self.session_id = session_id
        self.prompt_kind = prompt_kind
        self.peak_tokens = peak_tokens
        self.read_count = read_count


class ClaudeAgentClient:
    def __init__(
        self,
        api_key: str | None = None,
        timeout_seconds: float | None = 90,
        max_buffer_size: int | None = None,
    ) -> None:
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.max_buffer_size = max_buffer_size

    def json_response(
        self,
        system: str | dict[str, Any],
        prompt: str,
        cwd: str | None = None,
        max_turns: int = 1,
        allowed_tools: list[str] | None = None,
        permission_mode: str | None = None,
        setting_sources: list[str] | None = None,
        hooks: dict[str, list[Any]] | None = None,
        agents: dict[str, Any] | None = None,
        output_schema: dict[str, Any] | None = None,
        prompt_kind: str | None = None,
    ) -> dict:
        return self.json_response_with_meta(
            system,
            prompt,
            cwd=cwd,
            max_turns=max_turns,
            allowed_tools=allowed_tools,
            permission_mode=permission_mode,
            setting_sources=setting_sources,
            hooks=hooks,
            agents=agents,
            output_schema=output_schema,
            prompt_kind=prompt_kind,
        ).payload

    def json_response_with_meta(
        self,
        system: str | dict[str, Any],
        prompt: str,
        cwd: str | None = None,
        max_turns: int = 1,
        allowed_tools: list[str] | None = None,
        permission_mode: str | None = None,
        setting_sources: list[str] | None = None,
        hooks: dict[str, list[Any]] | None = None,
        agents: dict[str, Any] | None = None,
        output_schema: dict[str, Any] | None = None,
        prompt_kind: str | None = None,
    ) -> AgentJsonEnvelope:
        result = self.run_text(
            system=system,
            prompt=prompt,
            cwd=cwd,
            max_turns=max_turns,
            allowed_tools=allowed_tools,
            permission_mode=permission_mode,
            setting_sources=setting_sources,
            hooks=hooks,
            agents=agents,
            output_schema=output_schema or {"type": "object"},
            prompt_kind=prompt_kind,
        )
        prefetched_stderr: list[str] = []
        self._raise_for_oversized_read(result, prompt_kind=prompt_kind)
        forbidden_tool = _extract_forbidden_tool_attempt(result.stderr or [])
        if forbidden_tool is not None and _should_retry_forbidden_tool(
            prompt_kind=prompt_kind, tool_name=forbidden_tool[0]
        ):
            prefetched_stderr = list(result.stderr or [])
            result = self.run_text(
                system=system,
                prompt=_build_json_retry_prompt(
                    prompt,
                    prompt_kind=prompt_kind,
                    forbidden_tool=forbidden_tool[0],
                ),
                cwd=cwd,
                max_turns=max_turns,
                allowed_tools=allowed_tools,
                permission_mode=permission_mode,
                setting_sources=setting_sources,
                hooks=hooks,
                agents=agents,
                output_schema=output_schema or {"type": "object"},
                prompt_kind=prompt_kind,
            )
            self._raise_for_oversized_read(result, prompt_kind=prompt_kind)
        self._raise_for_forbidden_tool(result, prompt_kind=prompt_kind)
        retry_result: AgentResult | None = None
        result_stderr = prefetched_stderr + list(result.stderr or [])
        if isinstance(result.structured_output, dict):
            return AgentJsonEnvelope(
                payload=result.structured_output,
                session_id=_coerce_session_id(result.session_id),
                stderr=result_stderr,
            )
        text = result.result.strip()
        if not text:
            retry_result = self.run_text(
                system=system,
                prompt=_build_json_retry_prompt(prompt, prompt_kind=prompt_kind),
                cwd=cwd,
                max_turns=max_turns,
                allowed_tools=allowed_tools,
                permission_mode=permission_mode,
                setting_sources=setting_sources,
                hooks=hooks,
                agents=agents,
                output_schema=None,
                prompt_kind=prompt_kind,
            )
            self._raise_for_oversized_read(retry_result, prompt_kind=prompt_kind)
            self._raise_for_forbidden_tool(retry_result, prompt_kind=prompt_kind)
            text = retry_result.result.strip()
            if isinstance(retry_result.structured_output, dict):
                return AgentJsonEnvelope(
                    payload=retry_result.structured_output,
                    session_id=_coerce_session_id(retry_result.session_id),
                    stderr=result_stderr + list(retry_result.stderr or []),
                )
            if not text:
                combined_stderr = result_stderr + list(retry_result.stderr or [])
                context_overload = _extract_context_overload_error(combined_stderr)
                if context_overload is not None:
                    peak_tokens, read_count = context_overload
                    raise AgentContextOverloadError(
                        "Claude Agent SDK likely exhausted context after repeated Read operations and returned no JSON.",
                        stderr=combined_stderr,
                        session_id=retry_result.session_id or result.session_id,
                        prompt_kind=prompt_kind,
                        peak_tokens=peak_tokens,
                        read_count=read_count,
                    )
                detail = "\n".join((result.stderr or [])[-20:] + (retry_result.stderr or [])[-20:]).strip()
                message = "Claude Agent SDK returned an empty response when JSON was expected."
                if detail:
                    message = f"{message} stderr:\n{detail}"
                raise AgentJsonResponseError(
                    message,
                    raw_response=text[:500],
                    stderr=(result.stderr or []) + (retry_result.stderr or []),
                    session_id=retry_result.session_id or result.session_id,
                    prompt_kind=prompt_kind,
                )
        try:
            return AgentJsonEnvelope(
                payload=json.loads(text),
                session_id=_coerce_session_id(retry_result.session_id if retry_result else result.session_id),
                stderr=(result_stderr + list(retry_result.stderr or [])) if retry_result else result_stderr,
            )
        except json.JSONDecodeError:
            extracted = _extract_json_object(text)
            if extracted is not None:
                return AgentJsonEnvelope(
                    payload=extracted,
                    session_id=_coerce_session_id(retry_result.session_id if retry_result else result.session_id),
                    stderr=(result_stderr + list(retry_result.stderr or [])) if retry_result else result_stderr,
                )
            retry_result = self.run_text(
                system=system,
                prompt=_build_json_retry_prompt(prompt, prompt_kind=prompt_kind),
                cwd=cwd,
                max_turns=max_turns,
                allowed_tools=allowed_tools,
                permission_mode=permission_mode,
                setting_sources=setting_sources,
                hooks=hooks,
                agents=agents,
                output_schema=None,
                prompt_kind=prompt_kind,
            )
            self._raise_for_oversized_read(retry_result, prompt_kind=prompt_kind)
            self._raise_for_forbidden_tool(retry_result, prompt_kind=prompt_kind)
            text = retry_result.result.strip()
            if isinstance(retry_result.structured_output, dict):
                return AgentJsonEnvelope(
                    payload=retry_result.structured_output,
                    session_id=_coerce_session_id(retry_result.session_id),
                    stderr=result_stderr + list(retry_result.stderr or []),
                )
            if text:
                try:
                    return AgentJsonEnvelope(
                        payload=json.loads(text),
                        session_id=_coerce_session_id(retry_result.session_id),
                        stderr=result_stderr + list(retry_result.stderr or []),
                    )
                except json.JSONDecodeError:
                    extracted = _extract_json_object(text)
                    if extracted is not None:
                        return AgentJsonEnvelope(
                            payload=extracted,
                            session_id=_coerce_session_id(retry_result.session_id),
                            stderr=result_stderr + list(retry_result.stderr or []),
                        )
            detail = "\n".join(
                result_stderr[-20:] + ((retry_result.stderr or [])[-20:] if retry_result else [])
            ).strip()
            message = f"Claude Agent SDK did not return valid JSON. Raw response: {text[:500]!r}"
            if detail:
                message = f"{message} stderr:\n{detail}"
            raise AgentJsonResponseError(
                message,
                raw_response=text[:500],
                stderr=result_stderr + list(retry_result.stderr or []),
                session_id=retry_result.session_id or result.session_id,
                prompt_kind=prompt_kind,
            ) from None

    def _raise_for_forbidden_tool(self, result: AgentResult, *, prompt_kind: str | None) -> None:
        denied = _extract_forbidden_tool_attempt(result.stderr or [])
        if denied is None:
            return
        tool_name, reason = denied
        message = f"Claude Agent SDK attempted a forbidden tool: `{tool_name}`."
        if reason:
            message = f"{message} reason: {reason}"
        raise AgentForbiddenToolError(
            message,
            tool_name=tool_name,
            reason=reason,
            stderr=result.stderr,
            session_id=result.session_id,
            prompt_kind=prompt_kind,
        )

    def _raise_for_oversized_read(self, result: AgentResult, *, prompt_kind: str | None) -> None:
        oversized = _extract_oversized_read_error(result.stderr or [])
        if oversized is None:
            return
        observed_tokens, max_tokens = oversized
        message = "Claude Agent SDK attempted to read a file that exceeded the maximum token limit."
        raise AgentOversizedReadError(
            message,
            stderr=result.stderr,
            session_id=result.session_id,
            prompt_kind=prompt_kind,
            observed_tokens=observed_tokens,
            max_tokens=max_tokens,
        )

    def persistent_json_responses(
        self,
        system: str | dict[str, Any],
        prompts: list[str],
        cwd: str | None = None,
        max_turns: int = 1,
        allowed_tools: list[str] | None = None,
        permission_mode: str | None = None,
        setting_sources: list[str] | None = None,
        hooks: dict[str, list[Any]] | None = None,
        agents: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        return _run_async(
            _persistent_json_responses(
                self.api_key,
                system,
                prompts,
                cwd,
                max_turns,
                self.timeout_seconds,
                allowed_tools,
                permission_mode,
                setting_sources,
                hooks,
                agents,
            )
        )

    def run_text(
        self,
        system: str | dict[str, Any],
        prompt: str,
        cwd: str | None = None,
        max_turns: int = 1,
        allowed_tools: list[str] | None = None,
        permission_mode: str | None = None,
        setting_sources: list[str] | None = None,
        hooks: dict[str, list[Any]] | None = None,
        agents: dict[str, Any] | None = None,
        output_schema: dict[str, Any] | None = None,
        prompt_kind: str | None = None,
    ) -> AgentResult:
        return _run_async(
            _query_text(
                self.api_key,
                system,
                prompt,
                cwd,
                max_turns,
                self.timeout_seconds,
                self.max_buffer_size,
                allowed_tools,
                permission_mode,
                setting_sources,
                hooks,
                agents,
                output_schema,
                prompt_kind,
            )
        )


def _run_async(coro: Any) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    box: dict[str, Any] = {}

    def runner() -> None:
        try:
            box["value"] = asyncio.run(coro)
        except Exception as exc:  # pragma: no cover - thread handoff
            box["error"] = exc

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    thread.join()
    if "error" in box:
        raise box["error"]
    return box["value"]


async def _query_text(
    api_key: str | None,
    system: str | dict[str, Any],
    prompt: str,
    cwd: str | None,
    max_turns: int,
    timeout_seconds: float | None,
    max_buffer_size: int | None,
    allowed_tools: list[str] | None,
    permission_mode: str | None,
    setting_sources: list[str] | None,
    hooks: dict[str, list[Any]] | None,
    agents: dict[str, Any] | None,
    output_schema: dict[str, Any] | None,
    prompt_kind: str | None,
) -> AgentResult:
    try:
        from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, query
    except ImportError as exc:  # pragma: no cover - depends on local environment
        raise RuntimeError(
            "claude-agent-sdk が未インストールです。"
            " `venv/bin/pip install claude-agent-sdk` を実行し、Claude Code runtime をセットアップしてください。"
        ) from exc
    options, stderr_lines = _build_options(
        api_key=api_key,
        system=system,
        cwd=cwd,
        max_turns=max_turns,
        max_buffer_size=max_buffer_size,
        allowed_tools=allowed_tools,
        permission_mode=permission_mode,
        setting_sources=setting_sources,
        hooks=hooks,
        agents=agents,
        output_schema=output_schema,
    )

    async def _collect_result() -> AgentResult:
        final_result: AgentResult | None = None
        assistant_chunks: list[str] = []
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock) and block.text.strip():
                        assistant_chunks.append(block.text)
            if isinstance(message, ResultMessage):
                if message.is_error:
                    raise RuntimeError(message.result or "Claude Agent SDK execution failed.")
                final_result = AgentResult(
                    result=message.result or "\n".join(assistant_chunks).strip(),
                    structured_output=message.structured_output,
                    stderr=stderr_lines,
                    session_id=_coerce_session_id(message.session_id),
                    total_cost_usd=message.total_cost_usd,
                    usage=message.usage,
                )
        if final_result is None:
            raise RuntimeError("Claude Agent SDK returned no final result.")
        return final_result

    try:
        if timeout_seconds is None:
            return await _collect_result()
        return await asyncio.wait_for(_collect_result(), timeout=timeout_seconds)
    except RuntimeError as exc:
        overflow = _extract_buffer_overflow_error(str(exc), stderr_lines, max_buffer_size=max_buffer_size)
        if overflow is not None:
            observed_max_buffer, likely_source, source_detail = overflow
            message = "Claude Agent SDK exceeded the maximum JSON message buffer before returning a final result."
            if likely_source:
                message = f"{message} likely_source={likely_source}"
            if source_detail:
                message = f"{message} evidence={source_detail}"
            if observed_max_buffer:
                message = f"{message} max_buffer_size={observed_max_buffer}"
            raise AgentBufferOverflowError(
                message,
                stderr=stderr_lines,
                prompt_kind=prompt_kind,
                max_buffer_size=observed_max_buffer,
                likely_source=likely_source,
                source_detail=source_detail,
            ) from exc
        usage_limit_message = _extract_usage_limit_message(str(exc))
        if usage_limit_message is not None:
            raise AgentRateLimitError(
                f"Claude Agent SDK hit an account usage limit before returning a final result. {usage_limit_message}",
                stderr=stderr_lines,
                prompt_kind=prompt_kind,
                request_id="",
            ) from exc
        raise
    except TimeoutError as exc:
        detail = "\n".join(stderr_lines[-20:]).strip()
        rate_limit = _extract_rate_limit_error(stderr_lines)
        if rate_limit is not None:
            request_id, rate_limit_message = rate_limit
            message = "Claude Agent SDK hit an API rate limit before returning a final result."
            if rate_limit_message:
                message = f"{message} {rate_limit_message}"
            raise AgentRateLimitError(
                message,
                stderr=stderr_lines,
                prompt_kind=prompt_kind,
                request_id=request_id,
            ) from exc
        message = "Claude Agent SDK timed out before returning a final result."
        if detail:
            message = f"{message} stderr:\n{detail}"
        raise AgentTimeoutError(
            message,
            stderr=stderr_lines,
            prompt_kind=prompt_kind,
        ) from exc


async def _persistent_json_responses(
    api_key: str | None,
    system: str | dict[str, Any],
    prompts: list[str],
    cwd: str | None,
    max_turns: int,
    timeout_seconds: float | None,
    allowed_tools: list[str] | None,
    permission_mode: str | None,
    setting_sources: list[str] | None,
    hooks: dict[str, list[Any]] | None,
    agents: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    from claude_agent_sdk import ClaudeSDKClient

    options, _stderr_lines = _build_options(
        api_key=api_key,
        system=system,
        cwd=cwd,
        max_turns=max_turns,
        max_buffer_size=None,
        allowed_tools=allowed_tools,
        permission_mode=permission_mode,
        setting_sources=setting_sources,
        hooks=hooks,
        agents=agents,
        output_schema={"type": "object"},
    )

    async def _run_sequence() -> list[dict[str, Any]]:
        outputs: list[dict[str, Any]] = []
        async with ClaudeSDKClient(options=options) as client:
            for prompt in prompts:
                await client.query(prompt)
                result = await _collect_client_json_response(client)
                outputs.append(result)
        return outputs

    if timeout_seconds is None:
        return await _run_sequence()
    try:
        return await asyncio.wait_for(_run_sequence(), timeout=timeout_seconds)
    except TimeoutError as exc:
        raise RuntimeError("Claude Agent SDK timed out before returning a final result.") from exc


def _extract_json_object(text: str) -> dict[str, Any] | None:
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


def _build_json_retry_prompt(prompt: str, *, prompt_kind: str | None = None, forbidden_tool: str = "") -> str:
    extra = ""
    if prompt_kind in {"plan", "test_plan"}:
        extra = (
            "\n追加のツール探索は禁止です。"
            "\n新しい情報を探しに行かず、既に取得済みの要件と読取結果だけで完結してください。"
            "\n不明点は assumptions / risks / regression_risks に残してください。"
            "\nWrite / Edit / Bash は禁止です。ファイル保存は行わず、最終メッセージで JSON オブジェクトだけを返してください。"
        )
    forbidden_note = ""
    if forbidden_tool:
        forbidden_note = f"\n直前に禁止ツール `{forbidden_tool}` を使おうとしたため、その操作は中止されました。"
    return (
        f"{prompt}\n\n"
        "前の応答は無効です。理由: JSON オブジェクトではありませんでした。\n"
        "次は schema に一致する JSON オブジェクトのみを返してください。\n"
        "説明文、前置き、思考過程、Markdown コードブロックは禁止です。\n"
        "ツールを使った後でも最終メッセージは JSON のみです。"
        f"{forbidden_note}"
        f"{extra}"
    )


def _should_retry_forbidden_tool(*, prompt_kind: str | None, tool_name: str) -> bool:
    return prompt_kind in {"plan", "test_plan"} and tool_name in {"Write", "Edit", "Bash"}


def _extract_forbidden_tool_attempt(stderr_lines: list[str]) -> tuple[str, str] | None:
    tool_name = ""
    reason = ""
    reason_pattern = re.compile(r"permissionDecision: deny \(reason: (?P<reason>.+?)\)")
    denied_tool_pattern = re.compile(r"Hook denied tool use for (?P<tool>[A-Za-z0-9_-]+)")
    permission_denied_pattern = re.compile(r"(?P<tool>[A-Za-z0-9_-]+) tool permission denied")
    for line in stderr_lines:
        if not reason:
            match = reason_pattern.search(line)
            if match:
                reason = match.group("reason").strip()
        if not tool_name:
            match = denied_tool_pattern.search(line)
            if match:
                tool_name = match.group("tool").strip()
                continue
            match = permission_denied_pattern.search(line)
            if match:
                tool_name = match.group("tool").strip()
    if not tool_name and not reason:
        return None
    return tool_name, reason


def _extract_rate_limit_error(stderr_lines: list[str]) -> tuple[str, str] | None:
    request_id = ""
    message = ""
    request_id_pattern = re.compile(r'"request_id":"(?P<request_id>[^"]+)"')
    message_pattern = re.compile(r'"message":"(?P<message>[^"]+)"')
    for line in stderr_lines:
        if "rate_limit_error" not in line and "429" not in line:
            continue
        if not request_id:
            match = request_id_pattern.search(line)
            if match:
                request_id = match.group("request_id").strip()
        if not message:
            match = message_pattern.search(line)
            if match:
                message = match.group("message").strip()
    if not request_id and not message:
        return None
    return request_id, message


def _extract_usage_limit_message(message: str) -> str | None:
    normalized = message.strip()
    if "You've hit your limit" in normalized:
        return normalized
    return None


def _extract_oversized_read_error(stderr_lines: list[str]) -> tuple[str, str] | None:
    pattern = re.compile(
        r"Read tool error .*File content \((?P<observed>\d+) tokens\) exceeds maximum allowed tokens \((?P<maximum>\d+)\)"
    )
    for line in stderr_lines:
        match = pattern.search(line)
        if match:
            return match.group("observed"), match.group("maximum")
    return None


def _extract_context_overload_error(stderr_lines: list[str]) -> tuple[str, int] | None:
    autocompact_pattern = re.compile(r"autocompact: tokens=(?P<tokens>\d+)")
    read_pattern = re.compile(r"executePreToolHooks called for tool: Read")
    peak_tokens = 0
    read_count = 0
    for line in stderr_lines:
        if read_pattern.search(line):
            read_count += 1
        match = autocompact_pattern.search(line)
        if match:
            peak_tokens = max(peak_tokens, int(match.group("tokens")))
    if peak_tokens >= 50000 and read_count >= 3:
        return str(peak_tokens), read_count
    return None


def _extract_buffer_overflow_error(
    message: str,
    stderr_lines: list[str],
    *,
    max_buffer_size: int | None,
) -> tuple[int, str, str] | None:
    if "JSON message exceeded maximum buffer size" not in message:
        return None

    observed_max_buffer = max_buffer_size or 0
    match = re.search(r"maximum buffer size of (?P<bytes>\d+) bytes", message)
    if match:
        observed_max_buffer = int(match.group("bytes"))

    for line in reversed(stderr_lines):
        tool_match = re.search(r"executePreToolHooks called for tool: (?P<tool>[A-Za-z0-9_-]+)", line)
        if tool_match:
            return observed_max_buffer, "tool_output", f"last_tool={tool_match.group('tool')}"
        if "Sending " in line and " skills via attachment" in line:
            attach_match = re.search(r"Sending (?P<count>\d+) skills via attachment", line)
            detail = f"attachments={attach_match.group('count')}" if attach_match else "skills_attachment"
            return observed_max_buffer, "initial_attachments", detail
        if "Stream started - received first chunk" in line:
            return observed_max_buffer, "assistant_output", "stream_started"
        if "image" in line.lower():
            return observed_max_buffer, "input_image", "image_reference"

    return observed_max_buffer, "unknown", ""


def _build_options(
    *,
    api_key: str | None,
    system: str | dict[str, Any],
    cwd: str | None,
    max_turns: int,
    max_buffer_size: int | None,
    allowed_tools: list[str] | None,
    permission_mode: str | None,
    setting_sources: list[str] | None,
    hooks: dict[str, list[Any]] | None,
    agents: dict[str, Any] | None,
    output_schema: dict[str, Any] | None,
) -> tuple[Any, list[str]]:
    from claude_agent_sdk import ClaudeAgentOptions

    stderr_lines: list[str] = []
    env: dict[str, str] = {}
    if api_key:
        env["ANTHROPIC_API_KEY"] = api_key
    cli_path = _resolve_claude_cli()
    cli_version = _claude_version(cli_path)
    auth_status: dict[str, Any] | None = None
    if not api_key:
        auth_status = _claude_auth_status(cli_path)
        if not auth_status.get("loggedIn"):
            raise RuntimeError(
                f"Claude Code CLI が未ログインです。 status={json.dumps(auth_status, ensure_ascii=False)}"
            )
    debug_info = AgentDebugInfo(
        cli_path=cli_path,
        cli_version=cli_version,
        auth_status=auth_status,
        env_snapshot={
            "HOME": os.environ.get("HOME", ""),
            "PATH": os.environ.get("PATH", ""),
            "PWD": os.environ.get("PWD", ""),
            "CLAUDE_CODE_SSE_PORT": os.environ.get("CLAUDE_CODE_SSE_PORT", ""),
        },
        preflight=_claude_preflight(cli_path, cwd),
    )
    stderr_lines.append(f"[agent_sdk_client] debug={json.dumps(debug_info.__dict__, ensure_ascii=False)}")
    options = ClaudeAgentOptions(
        tools=[] if allowed_tools == [] else None,
        allowed_tools=allowed_tools or [],
        system_prompt=system,
        cwd=cwd,
        max_turns=max_turns,
        permission_mode=permission_mode,
        setting_sources=setting_sources,
        output_format={"type": "json_schema", "schema": output_schema} if output_schema is not None else None,
        cli_path=cli_path,
        env=env,
        extra_args={"debug-to-stderr": None},
        max_buffer_size=max_buffer_size,
        stderr=stderr_lines.append,
        hooks=hooks,
        agents=agents,
    )
    return options, stderr_lines


async def _collect_client_json_response(client: Any) -> dict[str, Any]:
    result = await _collect_client_agent_result(client)
    if isinstance(result.structured_output, dict):
        return result.structured_output
    text = result.result.strip()
    if not text:
        raise RuntimeError("Claude Agent SDK returned an empty response when JSON was expected.")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        extracted = _extract_json_object(text)
        if extracted is not None:
            return extracted
        raise RuntimeError(f"Claude Agent SDK did not return valid JSON. Raw response: {text[:500]!r}") from None


async def _collect_client_agent_result(client: Any) -> AgentResult:
    from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

    assistant_chunks: list[str] = []
    final_result: ResultMessage | None = None
    async for message in client.receive_response():
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock) and block.text.strip():
                    assistant_chunks.append(block.text)
        if isinstance(message, ResultMessage):
            if message.is_error:
                raise RuntimeError(message.result or "Claude Agent SDK execution failed.")
            final_result = message
    if final_result is None:
        raise RuntimeError("Claude Agent SDK returned no final result.")
    return AgentResult(
        result=(final_result.result or "\n".join(assistant_chunks)).strip(),
        structured_output=final_result.structured_output,
        session_id=_coerce_session_id(final_result.session_id),
        total_cost_usd=final_result.total_cost_usd,
        usage=final_result.usage,
    )


def _coerce_session_id(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    text = str(value).strip()
    return text or None


def _resolve_claude_cli() -> str:
    cli_path = shutil.which("claude")
    if cli_path:
        return cli_path
    raise RuntimeError(
        f"Claude Code CLI が見つかりません。 PATH={os.environ.get('PATH', '')!r} HOME={os.environ.get('HOME', '')!r}"
    )


def _claude_auth_status(cli_path: str) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            [cli_path, "auth", "status"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(f"Claude Code CLI が見つかりません: {cli_path}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("`claude auth status` がタイムアウトしました。") from exc
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()
        raise RuntimeError(f"`claude auth status` に失敗しました: {stderr or exc}") from exc

    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"`claude auth status` の出力を JSON として解釈できませんでした: {completed.stdout[:500]!r}"
        ) from exc


def _claude_version(cli_path: str) -> str:
    try:
        completed = subprocess.run(
            [cli_path, "--version"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception as exc:  # pragma: no cover - defensive diagnostics
        raise RuntimeError(f"`claude --version` に失敗しました: {exc}") from exc
    return completed.stdout.strip()


def _claude_preflight(cli_path: str, cwd: str | None) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            [cli_path, "-p", "ok", "--print", "--max-turns", "1"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=15,
        )
        return {
            "exit_code": completed.returncode,
            "stdout": completed.stdout[:500],
            "stderr": completed.stderr[:500],
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "timeout": True,
            "stdout": (exc.stdout or "")[:500],
            "stderr": (exc.stderr or "")[:500],
        }
    except Exception as exc:  # pragma: no cover - defensive diagnostics
        return {"error": str(exc)}

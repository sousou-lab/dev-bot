from __future__ import annotations

import asyncio
import json
import os
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


class ClaudeAgentClient:
    def __init__(self, api_key: str | None = None, timeout_seconds: float | None = 90) -> None:
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds

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
    ) -> dict:
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
            output_schema={"type": "object"},
        )
        if isinstance(result.structured_output, dict):
            return result.structured_output
        text = result.result.strip()
        if not text:
            retry_result = self.run_text(
                system=system,
                prompt=(
                    f"{prompt}\n\n"
                    "必ずJSONオブジェクトだけを返してください。"
                    " 前置き・説明・Markdownコードブロックは禁止です。"
                ),
                cwd=cwd,
                max_turns=max_turns,
                allowed_tools=allowed_tools,
                permission_mode=permission_mode,
                setting_sources=setting_sources,
                hooks=hooks,
                agents=agents,
                output_schema=None,
            )
            text = retry_result.result.strip()
            if isinstance(retry_result.structured_output, dict):
                return retry_result.structured_output
            if not text:
                detail = "\n".join((result.stderr or [])[-20:] + (retry_result.stderr or [])[-20:]).strip()
                message = "Claude Agent SDK returned an empty response when JSON was expected."
                if detail:
                    message = f"{message} stderr:\n{detail}"
                raise RuntimeError(message)
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            extracted = _extract_json_object(text)
            if extracted is not None:
                return extracted
            raise RuntimeError(f"Claude Agent SDK did not return valid JSON. Raw response: {text[:500]!r}")

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
    ) -> AgentResult:
        return _run_async(
            _query_text(
                self.api_key,
                system,
                prompt,
                cwd,
                max_turns,
                self.timeout_seconds,
                allowed_tools,
                permission_mode,
                setting_sources,
                hooks,
                agents,
                output_schema,
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
    allowed_tools: list[str] | None,
    permission_mode: str | None,
    setting_sources: list[str] | None,
    hooks: dict[str, list[Any]] | None,
    agents: dict[str, Any] | None,
    output_schema: dict[str, Any] | None,
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
                    session_id=message.session_id,
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
    except asyncio.TimeoutError as exc:
        detail = "\n".join(stderr_lines[-20:]).strip()
        message = "Claude Agent SDK timed out before returning a final result."
        if detail:
            message = f"{message} stderr:\n{detail}"
        raise RuntimeError(message) from exc


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
    except asyncio.TimeoutError as exc:
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


def _build_options(
    *,
    api_key: str | None,
    system: str | dict[str, Any],
    cwd: str | None,
    max_turns: int,
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
                "Claude Code CLI が未ログインです。"
                f" status={json.dumps(auth_status, ensure_ascii=False)}"
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
        stderr=stderr_lines.append,
        hooks=hooks,
        agents=agents,
    )
    return options, stderr_lines


async def _collect_client_json_response(client: Any) -> dict[str, Any]:
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
    if isinstance(final_result.structured_output, dict):
        return final_result.structured_output
    text = (final_result.result or "\n".join(assistant_chunks)).strip()
    if not text:
        raise RuntimeError("Claude Agent SDK returned an empty response when JSON was expected.")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        extracted = _extract_json_object(text)
        if extracted is not None:
            return extracted
        raise RuntimeError(f"Claude Agent SDK did not return valid JSON. Raw response: {text[:500]!r}")


def _resolve_claude_cli() -> str:
    cli_path = shutil.which("claude")
    if cli_path:
        return cli_path
    raise RuntimeError(
        "Claude Code CLI が見つかりません。"
        f" PATH={os.environ.get('PATH', '')!r} HOME={os.environ.get('HOME', '')!r}"
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

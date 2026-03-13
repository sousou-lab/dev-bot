from __future__ import annotations

import asyncio
import json
import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.runners.codex_app_server_backend import CodexAppServerBackend
from app.runners.execution_backend import RunSpec


@dataclass(frozen=True)
class CodexRunResult:
    returncode: int
    stdout_path: str
    changed_files: list[str]
    summary: str
    mode: str


class CodexRunner:
    _APP_SERVER_OVERSIZED_JSON_MARKERS = (
        "Fatal error in message reader:",
        "JSON message exceeded maximum buffer size",
    )

    def __init__(
        self,
        codex_bin: str = "codex",
        *,
        app_server_command: str = "codex app-server",
        model: str = "gpt-5.4",
        app_server_backend_factory: Callable[[str], CodexAppServerBackend] | None = None,
    ) -> None:
        self.codex_bin = codex_bin
        self.app_server_command = app_server_command
        self.model = model
        self.app_server_backend_factory = app_server_backend_factory or CodexAppServerBackend

    def build_prompt(
        self,
        *,
        issue: dict,
        requirement_summary: dict,
        plan: dict,
        test_plan: dict,
        workflow_text: str,
    ) -> str:
        return (
            "You are the implementation worker for this repository.\n"
            "Follow the repository workflow contract strictly.\n\n"
            f"[ISSUE]\n{json.dumps(issue, ensure_ascii=False, indent=2)}\n\n"
            f"[REQUIREMENT_SUMMARY]\n{json.dumps(requirement_summary, ensure_ascii=False, indent=2)}\n\n"
            f"[PLAN]\n{json.dumps(plan, ensure_ascii=False, indent=2)}\n\n"
            f"[TEST_PLAN]\n{json.dumps(test_plan, ensure_ascii=False, indent=2)}\n\n"
            f"[WORKFLOW_MD]\n{workflow_text}\n\n"
            "Rules:\n"
            "- Implement only what the plan requires.\n"
            "- Prefer minimal diffs.\n"
            "- Add or update tests when required.\n"
            "- Do not touch protected paths unless explicitly allowed.\n"
            "- Do not request interactive approvals; stop instead if blocked by policy.\n"
            "- At the end, output a short implementation summary.\n"
        )

    def run(
        self,
        *,
        workspace: str,
        run_dir: str,
        issue: dict,
        requirement_summary: dict,
        plan: dict,
        test_plan: dict,
        workflow_text: str,
        on_process_start: Callable[[int], None] | None = None,
        on_process_exit: Callable[[], None] | None = None,
    ) -> CodexRunResult:
        artifacts = Path(run_dir) / "artifacts"
        artifacts.mkdir(parents=True, exist_ok=True)
        stdout_path = artifacts / "codex_run.log"
        prompt = self.build_prompt(
            issue=issue,
            requirement_summary=requirement_summary,
            plan=plan,
            test_plan=test_plan,
            workflow_text=workflow_text,
        )
        if self._app_server_disabled():
            with stdout_path.open("w", encoding="utf-8") as fh:
                fh.write("[app-server-disabled] using codex exec fallback\n")
            return self._run_exec_fallback(
                workspace=workspace,
                stdout_path=stdout_path,
                prompt=prompt,
                on_process_start=on_process_start,
                on_process_exit=on_process_exit,
            )
        try:
            return self._run_app_server(
                workspace=workspace,
                run_dir=run_dir,
                stdout_path=stdout_path,
                prompt=prompt,
                on_process_start=on_process_start,
                on_process_exit=on_process_exit,
            )
        except Exception as exc:
            with stdout_path.open("a", encoding="utf-8") as fh:
                fh.write(f"\n[app-server-fallback] {exc}\n")
            return self._run_exec_fallback(
                workspace=workspace,
                stdout_path=stdout_path,
                prompt=prompt,
                on_process_start=on_process_start,
                on_process_exit=on_process_exit,
            )

    def _run_app_server(
        self,
        *,
        workspace: str,
        run_dir: str,
        stdout_path: Path,
        prompt: str,
        on_process_start: Callable[[int], None] | None,
        on_process_exit: Callable[[], None] | None,
    ) -> CodexRunResult:
        with stdout_path.open("w", encoding="utf-8") as log_fh:
            try:
                result = asyncio.run(
                    self._run_app_server_backend(
                        workspace=workspace,
                        run_dir=run_dir,
                        prompt=prompt,
                        on_process_start=on_process_start,
                    )
                )
            finally:
                if on_process_exit is not None:
                    on_process_exit()
        if result.raw_event_log_path:
            raw_event_log = Path(result.raw_event_log_path)
            if raw_event_log.exists():
                log_fh = stdout_path.open("a", encoding="utf-8")
                try:
                    log_fh.write(raw_event_log.read_text(encoding="utf-8"))
                finally:
                    log_fh.close()
        default_changed_files = self._detect_changed_files(workspace)
        changed_files = result.changed_files or default_changed_files
        summary = result.summary
        returncode = result.returncode
        self._write_implementation_result(
            artifacts_dir=Path(stdout_path).parent,
            summary=summary,
            changed_files=changed_files,
            payload=result.implementation_result,
        )
        (Path(stdout_path).parent / "changed_files.json").write_text(
            json.dumps({"changed_files": changed_files}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return CodexRunResult(
            returncode=returncode,
            stdout_path=str(stdout_path),
            changed_files=changed_files,
            summary=summary,
            mode="app-server",
        )

    async def _run_app_server_backend(
        self,
        *,
        workspace: str,
        run_dir: str,
        prompt: str,
        on_process_start: Callable[[int], None] | None,
    ):
        backend = self.app_server_backend_factory(self.app_server_command)
        handle = await backend.start_run(
            RunSpec(
                run_id="run",
                issue_key="issue",
                candidate_id="primary",
                cwd=workspace,
                prompt=prompt,
                model=self.model,
                service_name="dev-bot",
                output_schema_name="implementation_result_v1",
                artifacts_dir=str(Path(run_dir) / "artifacts"),
                writable_roots=[workspace],
                read_only_roots=[],
                network_access=False,
                allow_turn_steer=False,
                allow_thread_resume_same_run_only=True,
            )
        )
        if on_process_start is not None and handle.process_id is not None:
            on_process_start(handle.process_id)
        return await backend.collect_outputs(handle)

    def _run_exec_fallback(
        self,
        *,
        workspace: str,
        stdout_path: Path,
        prompt: str,
        on_process_start: Callable[[int], None] | None,
        on_process_exit: Callable[[], None] | None,
    ) -> CodexRunResult:
        cmd = [
            self.codex_bin,
            "exec",
            "--sandbox",
            "workspace-write",
            "--skip-git-repo-check",
            "-C",
            workspace,
            "-",
        ]
        env = os.environ.copy()
        with stdout_path.open("a", encoding="utf-8") as fh:
            process = subprocess.Popen(
                cmd,
                cwd=workspace,
                env=env,
                stdin=subprocess.PIPE,
                stdout=fh,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
            )
            if on_process_start is not None:
                on_process_start(process.pid)
            try:
                assert process.stdin is not None
                process.stdin.write(prompt)
                process.stdin.close()
                returncode = process.wait()
            finally:
                if on_process_exit is not None:
                    on_process_exit()

        changed_files = self._detect_changed_files(workspace)
        summary = f"Codex exec fallback finished with return code {returncode}"
        self._write_implementation_result(
            artifacts_dir=Path(stdout_path).parent,
            summary=summary,
            changed_files=changed_files,
        )
        (Path(stdout_path).parent / "changed_files.json").write_text(
            json.dumps({"changed_files": changed_files}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return CodexRunResult(
            returncode=returncode,
            stdout_path=str(stdout_path),
            changed_files=changed_files,
            summary=summary,
            mode="exec-fallback",
        )

    def _app_server_disabled(self) -> bool:
        return self.app_server_command.strip().lower() in {"", "disabled", "off", "false", "0"}

    def _wait_for_response(self, stdout, log_fh, expected_id: int) -> dict[str, Any]:
        for raw_line in stdout:
            log_fh.write(raw_line)
            log_fh.flush()
            payload = self._safe_json(raw_line)
            if payload is None:
                continue
            if payload.get("id") != expected_id:
                continue
            if "error" in payload:
                raise RuntimeError(f"app-server error for id={expected_id}: {payload['error']}")
            return payload.get("result", {})
        raise RuntimeError(f"app-server response not received for id={expected_id}")

    def _build_turn_start_message(
        self, *, request_id: int, thread_id: str, prompt: str, workspace: str
    ) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "turn/start",
            "params": {
                "threadId": thread_id,
                "input": [{"type": "text", "text": prompt}],
                "cwd": workspace,
                "approvalPolicy": "never",
                "model": self.model,
                "effort": "medium",
                "summary": "concise",
                "sandboxPolicy": {
                    "type": "workspace-write",
                    "writableRoots": [workspace],
                    "networkAccess": False,
                },
                "serviceName": "dev-bot",
                "outputSchema": self._implementation_output_schema(),
            },
        }

    def _extract_thread_id(self, response: dict[str, Any]) -> str:
        candidates = [response.get("threadId"), response.get("id")]
        thread = response.get("thread")
        if isinstance(thread, dict):
            candidates.extend([thread.get("id"), thread.get("threadId")])
        for value in candidates:
            if isinstance(value, str) and value:
                return value
        raise RuntimeError(f"app-server thread/start response missing thread id: {response}")

    def _send_message(self, stdin, payload: dict[str, Any]) -> None:
        stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        stdin.flush()

    def _safe_json(self, raw_line: str) -> dict[str, Any] | None:
        line = raw_line.strip()
        if not line.startswith("{"):
            return None
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, dict) else None

    def _extract_text_delta(self, payload: dict[str, Any]) -> str:
        params = payload.get("params")
        if not isinstance(params, dict):
            return ""
        for key in ("delta", "text", "message"):
            value = params.get(key)
            if isinstance(value, str):
                return value
        item = params.get("item")
        if isinstance(item, dict):
            for key in ("text", "delta"):
                value = item.get(key)
                if isinstance(value, str):
                    return value
            content = item.get("content")
            if isinstance(content, list):
                texts = [
                    part.get("text", "")
                    for part in content
                    if isinstance(part, dict) and isinstance(part.get("text"), str)
                ]
                return "".join(texts)
        return ""

    def _is_oversized_json_reader_failure(self, raw_line: str) -> bool:
        return all(marker in raw_line for marker in self._APP_SERVER_OVERSIZED_JSON_MARKERS)

    def _implementation_output_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "candidate_id": {"type": "string"},
                "summary": {"type": "string"},
                "changed_files": {"type": "array", "items": {"type": "string"}},
                "tests_run": {"type": "array", "items": {"type": "string"}},
                "followups": {"type": "array", "items": {"type": "string"}},
                "blocked_reasons": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["candidate_id", "summary", "changed_files"],
            "additionalProperties": False,
        }

    def _extract_structured_output(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        params = payload.get("params")
        if not isinstance(params, dict):
            return None
        candidates = [
            params.get("output"),
            params.get("structuredOutput"),
            params.get("structured_output"),
        ]
        result = params.get("result")
        if isinstance(result, dict):
            candidates.extend(
                [
                    result.get("output"),
                    result.get("structuredOutput"),
                    result.get("structured_output"),
                ]
            )
        for candidate in candidates:
            if isinstance(candidate, dict):
                return candidate
        return None

    def _normalize_changed_files(self, value: Any, fallback: list[str]) -> list[str]:
        if not isinstance(value, list):
            return fallback
        changed_files = [str(item).strip() for item in value if str(item).strip()]
        return changed_files or fallback

    def _write_implementation_result(
        self,
        *,
        artifacts_dir: Path,
        summary: str,
        changed_files: list[str],
        payload: dict[str, Any] | None = None,
    ) -> None:
        data = {
            "candidate_id": "primary",
            "summary": summary,
            "changed_files": changed_files,
            "tests_run": [],
            "followups": [],
            "blocked_reasons": [],
        }
        if isinstance(payload, dict):
            candidate_id = str(payload.get("candidate_id", "")).strip()
            if candidate_id:
                data["candidate_id"] = candidate_id
            tests_run = payload.get("tests_run")
            if isinstance(tests_run, list):
                data["tests_run"] = [str(item) for item in tests_run if str(item).strip()]
            followups = payload.get("followups")
            if isinstance(followups, list):
                data["followups"] = [str(item) for item in followups if str(item).strip()]
            blocked_reasons = payload.get("blocked_reasons")
            if isinstance(blocked_reasons, list):
                data["blocked_reasons"] = [str(item) for item in blocked_reasons if str(item).strip()]
        (artifacts_dir / "implementation_result.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _detect_changed_files(self, workspace: str) -> list[str]:
        try:
            output = subprocess.check_output(["git", "status", "--porcelain"], cwd=workspace, text=True)
        except Exception:
            return []
        changed: list[str] = []
        for line in output.splitlines():
            if len(line) >= 4:
                changed.append(line[3:])
        return changed

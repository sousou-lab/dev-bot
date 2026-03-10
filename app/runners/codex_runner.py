from __future__ import annotations

import json
import os
import shlex
import subprocess
from dataclasses import dataclass
from itertools import count
from pathlib import Path
from typing import Any, Callable


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
        model: str = "gpt-5-codex",
    ) -> None:
        self.codex_bin = codex_bin
        self.app_server_command = app_server_command
        self.model = model

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
        stdout_path: Path,
        prompt: str,
        on_process_start: Callable[[int], None] | None,
        on_process_exit: Callable[[], None] | None,
    ) -> CodexRunResult:
        cmd = shlex.split(self.app_server_command)
        env = os.environ.copy()
        request_ids = count(1)
        summary_chunks: list[str] = []
        turn_completed = False
        returncode = 1
        oversized_json_failure = False

        with stdout_path.open("w", encoding="utf-8") as log_fh:
            process = subprocess.Popen(
                cmd,
                cwd=workspace,
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
                bufsize=1,
            )
            if on_process_start is not None:
                on_process_start(process.pid)
            try:
                assert process.stdin is not None
                assert process.stdout is not None

                init_id = next(request_ids)
                self._send_message(
                    process.stdin,
                    {
                        "jsonrpc": "2.0",
                        "id": init_id,
                        "method": "initialize",
                        "params": {"protocolVersion": "0.1", "experimentalApi": True},
                    },
                )
                self._wait_for_response(process.stdout, log_fh, init_id)
                self._send_message(
                    process.stdin,
                    {"jsonrpc": "2.0", "method": "initialized", "params": {}},
                )

                thread_id = next(request_ids)
                self._send_message(
                    process.stdin,
                    {
                        "jsonrpc": "2.0",
                        "id": thread_id,
                        "method": "thread/start",
                        "params": {
                            "model": self.model,
                            "cwd": workspace,
                            "approvalPolicy": "never",
                            "sandbox": {
                                "type": "workspace-write",
                                "writableRoots": [workspace],
                                "networkAccess": False,
                            },
                        },
                    },
                )
                thread_response = self._wait_for_response(process.stdout, log_fh, thread_id)
                server_thread_id = self._extract_thread_id(thread_response)

                turn_id = next(request_ids)
                self._send_message(
                    process.stdin,
                    self._build_turn_start_message(
                        request_id=turn_id,
                        thread_id=server_thread_id,
                        prompt=prompt,
                        workspace=workspace,
                    ),
                )
                self._wait_for_response(process.stdout, log_fh, turn_id)

                for raw_line in process.stdout:
                    log_fh.write(raw_line)
                    log_fh.flush()
                    if self._is_oversized_json_reader_failure(raw_line):
                        oversized_json_failure = True
                    payload = self._safe_json(raw_line)
                    if payload is None:
                        continue
                    if "id" in payload and "method" in payload and "result" not in payload and "error" not in payload:
                        self._send_message(
                            process.stdin,
                            {
                                "jsonrpc": "2.0",
                                "id": payload["id"],
                                "error": {"code": -32000, "message": "interactive requests disabled"},
                            },
                        )
                        continue
                    method = str(payload.get("method", ""))
                    if method:
                        delta = self._extract_text_delta(payload)
                        if delta:
                            summary_chunks.append(delta)
                        if method == "turn/completed":
                            turn_completed = True
                            returncode = 0
                            break
                        if method == "turn/failed":
                            returncode = 1
                            break
                if not turn_completed and returncode == 0:
                    returncode = 1
            finally:
                try:
                    process.terminate()
                    process.wait(timeout=1)
                except Exception:
                    pass
                if on_process_exit is not None:
                    on_process_exit()

        if oversized_json_failure:
            raise RuntimeError("codex app-server failed due to oversized JSON message")

        changed_files = self._detect_changed_files(workspace)
        summary = "".join(summary_chunks).strip() or f"Codex app-server finished with return code {returncode}"
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

    def _build_turn_start_message(self, *, request_id: int, thread_id: str, prompt: str, workspace: str) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "turn/start",
            "params": {
                "threadId": thread_id,
                "input": [{"type": "text", "text": prompt}],
                "cwd": workspace,
                "approvalPolicy": "never",
                "sandboxPolicy": {
                    "type": "workspace-write",
                    "writableRoots": [workspace],
                    "networkAccess": False,
                },
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
                texts = [part.get("text", "") for part in content if isinstance(part, dict) and isinstance(part.get("text"), str)]
                return "".join(texts)
        return ""

    def _is_oversized_json_reader_failure(self, raw_line: str) -> bool:
        return all(marker in raw_line for marker in self._APP_SERVER_OVERSIZED_JSON_MARKERS)

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

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from app.runners.execution_backend import ExecutionBackend, RunArtifacts, RunHandle, RunSpec


class CodexAppServerBackend(ExecutionBackend):
    def __init__(self, command: str = "codex app-server") -> None:
        self.command = command
        self._proc: asyncio.subprocess.Process | None = None
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._reader_task: asyncio.Task[None] | None = None
        self._event_log_path: Path | None = None
        self._artifacts_dir: Path | None = None
        self._request_id = 100
        self._active_run_id: str | None = None
        self._allow_turn_steer = False
        self._allow_thread_resume_same_run_only = True
        self._summary_chunks: list[str] = []
        self._implementation_result: dict[str, Any] | None = None
        self._returncode = 1
        self._turn_completed = False
        self._closed = False

    async def start_run(self, spec: RunSpec) -> RunHandle:
        self._artifacts_dir = (
            Path(spec.artifacts_dir).resolve() if spec.artifacts_dir else (Path(spec.cwd) / "artifacts")
        )
        self._artifacts_dir.mkdir(parents=True, exist_ok=True)
        self._event_log_path = self._artifacts_dir / "raw_codex_events.jsonl"
        self._active_run_id = spec.run_id
        self._allow_turn_steer = spec.allow_turn_steer
        self._allow_thread_resume_same_run_only = spec.allow_thread_resume_same_run_only
        self._summary_chunks = []
        self._implementation_result = None
        self._returncode = 1
        self._turn_completed = False
        self._closed = False
        await self._boot_client(cwd=spec.cwd)

        strategy = spec.session_strategy.strip() or "fresh"
        if strategy == "compact":
            thread_id, turn_id = await self._compact_start(spec)
        else:
            thread_id = await self._start_thread(spec, strategy=strategy)
            turn_id = await self._start_turn(spec, thread_id)
        return RunHandle(
            run_id=spec.run_id,
            thread_id=thread_id,
            turn_id=turn_id,
            process_id=self._proc.pid if self._proc else None,
        )

    async def read_thread(self, thread_id: str) -> dict[str, Any]:
        if not thread_id.strip():
            raise RuntimeError("thread/read requires thread_id")
        self._event_log_path = None
        self._artifacts_dir = None
        self._summary_chunks = []
        self._implementation_result = None
        self._returncode = 1
        self._turn_completed = False
        self._closed = False
        await self._boot_client(cwd=str(Path.cwd()))
        try:
            payload = await self._request("thread/read", {"threadId": thread_id}, request_id=2)
            result = payload.get("result")
            if isinstance(result, dict):
                thread = result.get("thread")
                if isinstance(thread, dict):
                    normalized = dict(thread)
                    normalized.setdefault("id", thread_id)
                    return normalized
                normalized = dict(result)
                normalized.setdefault("id", thread_id)
                return normalized
            return {"id": thread_id}
        finally:
            await self._shutdown()

    async def _start_thread(self, spec: RunSpec, *, strategy: str) -> str:
        if strategy == "fork":
            if not spec.session_id:
                raise RuntimeError("thread/fork requires session_id")
            thread_resp = await self._request(
                "thread/fork",
                {"threadId": spec.session_id},
                request_id=2,
            )
            return self._extract_nested_id(thread_resp, "thread")
        thread_resp = await self._request(
            "thread/start",
            {
                "model": spec.model,
                "cwd": spec.cwd,
                "approvalPolicy": "never",
                "sandbox": "workspace-write",
                "serviceName": spec.service_name,
            },
            request_id=2,
        )
        return self._extract_nested_id(thread_resp, "thread")

    async def _start_turn(self, spec: RunSpec, thread_id: str) -> str:
        turn_resp = await self._request(
            "turn/start",
            {
                "threadId": thread_id,
                "input": [{"type": "text", "text": spec.prompt}],
                "cwd": spec.cwd,
                "approvalPolicy": "never",
                "model": spec.model,
                "effort": "medium",
                "summary": "concise",
                "sandboxPolicy": {
                    "type": "workspace-write",
                    "writableRoots": spec.writable_roots or [spec.cwd],
                    "readOnlyAccess": {
                        "type": "restricted",
                        "includePlatformDefaults": True,
                        "readableRoots": spec.read_only_roots,
                    },
                    "networkAccess": spec.network_access,
                },
                "outputSchema": self._load_output_schema(spec.output_schema_name),
            },
            request_id=3,
        )
        return self._extract_nested_id(turn_resp, "turn")

    async def _compact_start(self, spec: RunSpec) -> tuple[str, str]:
        if not spec.session_id:
            raise RuntimeError("thread/compact/start requires session_id")
        resp = await self._request(
            "thread/compact/start",
            {
                "threadId": spec.session_id,
                "input": [{"type": "text", "text": spec.prompt}],
                "cwd": spec.cwd,
                "approvalPolicy": "never",
                "model": spec.model,
                "effort": "medium",
                "summary": "concise",
                "sandboxPolicy": {
                    "type": "workspace-write",
                    "writableRoots": spec.writable_roots or [spec.cwd],
                    "readOnlyAccess": {
                        "type": "restricted",
                        "includePlatformDefaults": True,
                        "readableRoots": spec.read_only_roots,
                    },
                    "networkAccess": spec.network_access,
                },
                "outputSchema": self._load_output_schema(spec.output_schema_name),
            },
            request_id=2,
        )
        thread_id = self._extract_nested_id(resp, "thread")
        try:
            turn_id = self._extract_nested_id(resp, "turn")
        except RuntimeError:
            turn_id = await self._start_turn(spec, thread_id)
        return thread_id, turn_id

    async def steer(self, handle: RunHandle, message: str) -> None:
        if not self._allow_turn_steer:
            raise RuntimeError("turn/steer is disabled for this run")
        await self._request(
            "turn/steer",
            {
                "threadId": handle.thread_id,
                "expectedTurnId": handle.turn_id,
                "input": [{"type": "text", "text": message}],
            },
        )

    async def interrupt(self, handle: RunHandle) -> None:
        await self._request("turn/interrupt", {"threadId": handle.thread_id, "turnId": handle.turn_id})

    async def resume_same_run(self, handle: RunHandle) -> RunHandle:
        if self._allow_thread_resume_same_run_only and self._active_run_id != handle.run_id:
            raise RuntimeError("thread/resume is restricted to the active run_id")
        resp = await self._request("thread/resume", {"threadId": handle.thread_id})
        return RunHandle(
            run_id=handle.run_id,
            thread_id=self._extract_nested_id(resp, "thread"),
            turn_id=handle.turn_id,
            process_id=self._proc.pid if self._proc else None,
        )

    async def collect_outputs(self, _handle: RunHandle) -> RunArtifacts:
        await self._wait_for_completion()
        await self._shutdown()
        changed_files = self._normalize_changed_files(
            self._implementation_result.get("changed_files") if isinstance(self._implementation_result, dict) else None
        )
        summary = "".join(self._summary_chunks).strip()
        if isinstance(self._implementation_result, dict):
            summary = str(self._implementation_result.get("summary", "")).strip() or summary
        if not summary:
            summary = f"Codex app-server finished with return code {self._returncode}"
        implementation_result_path = ""
        if self._artifacts_dir is not None:
            implementation_result_path = str(self._artifacts_dir / "implementation_result.json")
        return RunArtifacts(
            implementation_result=self._implementation_result,
            changed_files=changed_files,
            summary=summary,
            returncode=self._returncode,
            mode="app-server",
            implementation_result_path=implementation_result_path,
            raw_event_log_path=str(self._event_log_path) if self._event_log_path else "",
            session_id=_handle.thread_id,
        )

    async def _boot_client(self, *, cwd: str) -> None:
        self._proc = await asyncio.create_subprocess_shell(
            self.command,
            cwd=cwd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        self._reader_task = asyncio.create_task(self._reader_loop())
        await self._request(
            "initialize",
            {"clientInfo": {"name": "dev-bot", "version": "phase1"}, "capabilities": {}},
            request_id=1,
        )
        await self._notify("initialized", {})

    async def _reader_loop(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        while True:
            line = await self._proc.stdout.readline()
            if not line:
                break
            try:
                payload = json.loads(line.decode("utf-8"))
            except json.JSONDecodeError:
                continue
            await self._append_event(payload)
            if "id" in payload and "method" in payload and "result" not in payload and "error" not in payload:
                await self._write_json(
                    {
                        "jsonrpc": "2.0",
                        "id": payload["id"],
                        "error": {"code": -32000, "message": "interactive requests disabled"},
                    }
                )
                continue
            method = str(payload.get("method", ""))
            if method:
                delta = self._extract_text_delta(payload)
                if delta:
                    self._summary_chunks.append(delta)
                if method == "turn/completed":
                    self._implementation_result = self._extract_structured_output(payload)
                    self._turn_completed = True
                    self._returncode = 0
                    continue
                if method == "turn/failed":
                    self._returncode = 1
                    continue
            req_id = payload.get("id")
            if isinstance(req_id, int) and req_id in self._pending:
                future = self._pending.pop(req_id)
                if not future.done():
                    future.set_result(payload)

    async def _append_event(self, payload: dict[str, Any]) -> None:
        if self._event_log_path is None:
            return
        with self._event_log_path.open("a", encoding="utf-8") as fp:
            fp.write(json.dumps(payload, ensure_ascii=False) + "\n")

    async def _request(self, method: str, params: dict[str, Any], request_id: int | None = None) -> dict[str, Any]:
        rid = request_id or self._next_request_id()
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[rid] = future
        await self._write_json({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        payload = await future
        if "error" in payload:
            raise RuntimeError(f"app-server request failed for {method}: {payload['error']}")
        return payload

    async def _notify(self, method: str, params: dict[str, Any]) -> None:
        await self._write_json({"jsonrpc": "2.0", "method": method, "params": params})

    async def _write_json(self, payload: dict[str, Any]) -> None:
        assert self._proc is not None and self._proc.stdin is not None
        self._proc.stdin.write((json.dumps(payload) + "\n").encode("utf-8"))
        await self._proc.stdin.drain()

    def _next_request_id(self) -> int:
        self._request_id += 1
        return self._request_id

    def _extract_nested_id(self, payload: dict[str, Any], key: str) -> str:
        result = payload.get("result")
        if isinstance(result, dict):
            nested = result.get(key)
            if isinstance(nested, dict) and isinstance(nested.get("id"), str):
                return nested["id"]
            if isinstance(result.get(f"{key}Id"), str):
                return result[f"{key}Id"]
        raise RuntimeError(f"missing {key} id in payload: {payload}")

    def _load_output_schema(self, name: str) -> dict[str, Any]:
        if name != "implementation_result_v1":
            raise KeyError(name)
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
                return "".join(
                    part.get("text", "")
                    for part in content
                    if isinstance(part, dict) and isinstance(part.get("text"), str)
                )
        return ""

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
            candidates.extend([result.get("output"), result.get("structuredOutput"), result.get("structured_output")])
        for candidate in candidates:
            if isinstance(candidate, dict):
                return candidate
        return None

    def _normalize_changed_files(self, value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        return [str(item).strip() for item in value if str(item).strip()]

    async def _wait_for_completion(self) -> None:
        while self._proc is not None and self._proc.returncode is None and not self._turn_completed:
            await asyncio.sleep(0.05)
        if self._proc is not None and self._proc.returncode is None and self._turn_completed:
            return
        if self._proc is not None and self._proc.returncode is None:
            await self._proc.wait()

    async def _shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._proc is not None and self._proc.returncode is None:
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=1)
            except TimeoutError:
                self._proc.kill()
                await self._proc.wait()
        if self._reader_task is not None:
            await self._reader_task

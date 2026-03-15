from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from app.runners.codex_app_server_backend import CodexAppServerBackend
from app.runners.execution_backend import RunHandle, RunSpec


class CodexAppServerBackendTests(unittest.TestCase):
    def test_load_output_schema_returns_expected_required_fields(self) -> None:
        backend = CodexAppServerBackend()

        schema = backend._load_output_schema("implementation_result_v1")

        self.assertEqual(["candidate_id", "summary", "changed_files"], schema["required"])

    def test_extract_nested_id_reads_result_payload(self) -> None:
        backend = CodexAppServerBackend()

        thread_id = backend._extract_nested_id({"result": {"thread": {"id": "thread_123"}}}, "thread")

        self.assertEqual("thread_123", thread_id)

    def test_steer_rejects_when_disabled(self) -> None:
        backend = CodexAppServerBackend()

        with self.assertRaisesRegex(RuntimeError, "turn/steer is disabled"):
            asyncio.run(backend.steer(RunHandle(run_id="run-1", thread_id="thread_1", turn_id="turn_1"), "nudge"))

    def test_resume_same_run_rejects_different_run_id(self) -> None:
        backend = CodexAppServerBackend()
        backend._active_run_id = "run-1"

        with self.assertRaisesRegex(RuntimeError, "active run_id"):
            asyncio.run(backend.resume_same_run(RunHandle(run_id="run-2", thread_id="thread_1", turn_id="turn_1")))

    def test_resume_same_run_preserves_run_id(self) -> None:
        backend = CodexAppServerBackend()
        backend._active_run_id = "run-1"
        backend._proc = type("Proc", (), {"pid": 123})()

        async def run_test() -> None:
            async def fake_request(
                method: str, params: dict[str, str], request_id: int | None = None
            ) -> dict[str, object]:
                self.assertEqual("thread/resume", method)
                self.assertEqual({"threadId": "thread_1"}, params)
                return {"result": {"thread": {"id": "thread_2"}}}

            backend._request = fake_request  # type: ignore[method-assign]
            handle = await backend.resume_same_run(RunHandle(run_id="run-1", thread_id="thread_1", turn_id="turn_1"))
            self.assertEqual("run-1", handle.run_id)
            self.assertEqual("thread_2", handle.thread_id)
            self.assertEqual("turn_1", handle.turn_id)
            self.assertEqual(123, handle.process_id)

        asyncio.run(run_test())

    def test_read_thread_returns_thread_payload(self) -> None:
        backend = CodexAppServerBackend()

        async def run_test() -> None:
            calls: list[str] = []
            fake_proc = type("Proc", (), {"pid": 123, "stdout": None, "stdin": None})()

            async def fake_request(
                method: str, params: dict[str, object], request_id: int | None = None
            ) -> dict[str, object]:
                del request_id
                calls.append(method)
                if method == "initialize":
                    return {"result": {}}
                if method == "thread/read":
                    self.assertEqual({"threadId": "thread_1"}, params)
                    return {"result": {"thread": {"id": "thread_1", "turn_count": 3, "status": "active"}}}
                raise AssertionError(method)

            async def fake_notify(method: str, params: dict[str, object]) -> None:
                del params
                calls.append(method)

            async def fake_reader_loop() -> None:
                return None

            async def fake_shutdown() -> None:
                calls.append("shutdown")

            with patch("app.runners.codex_app_server_backend.asyncio.create_subprocess_shell", return_value=fake_proc):
                backend._request = fake_request  # type: ignore[method-assign]
                backend._notify = fake_notify  # type: ignore[method-assign]
                backend._reader_loop = fake_reader_loop  # type: ignore[method-assign]
                backend._shutdown = fake_shutdown  # type: ignore[method-assign]
                payload = await backend.read_thread("thread_1")
            self.assertEqual("thread_1", payload["id"])
            self.assertEqual(3, payload["turn_count"])
            self.assertEqual(["initialize", "initialized", "thread/read", "shutdown"], calls)

        asyncio.run(run_test())

    def test_start_run_forks_from_existing_thread_when_requested(self) -> None:
        backend = CodexAppServerBackend()

        async def run_test() -> None:
            calls: list[str] = []
            fake_proc = type("Proc", (), {"pid": 123, "stdout": None, "stdin": None})()

            async def fake_request(
                method: str, params: dict[str, object], request_id: int | None = None
            ) -> dict[str, object]:
                del request_id
                calls.append(method)
                if method == "initialize":
                    return {"result": {}}
                if method == "thread/fork":
                    self.assertEqual({"threadId": "thread_base"}, params)
                    return {"result": {"thread": {"id": "thread_forked"}}}
                if method == "turn/start":
                    self.assertEqual("thread_forked", params["threadId"])
                    return {"result": {"turn": {"id": "turn_1"}}}
                raise AssertionError(method)

            async def fake_notify(method: str, params: dict[str, object]) -> None:
                del params
                calls.append(method)

            async def fake_reader_loop() -> None:
                return None

            with patch("app.runners.codex_app_server_backend.asyncio.create_subprocess_shell", return_value=fake_proc):
                backend._request = fake_request  # type: ignore[method-assign]
                backend._notify = fake_notify  # type: ignore[method-assign]
                backend._reader_loop = fake_reader_loop  # type: ignore[method-assign]
                handle = await backend.start_run(
                    RunSpec(
                        run_id="run-1",
                        issue_key="owner/repo#1",
                        candidate_id="alt1",
                        cwd="/tmp/work",
                        prompt="implement",
                        session_id="thread_base",
                        session_strategy="fork",
                    )
                )
            self.assertEqual("thread_forked", handle.thread_id)
            self.assertEqual("turn_1", handle.turn_id)
            self.assertEqual(["initialize", "initialized", "thread/fork", "turn/start"], calls)

        asyncio.run(run_test())

    def test_start_run_uses_compact_start_for_rollover(self) -> None:
        backend = CodexAppServerBackend()

        async def run_test() -> None:
            calls: list[str] = []
            fake_proc = type("Proc", (), {"pid": 123, "stdout": None, "stdin": None})()

            async def fake_request(
                method: str, params: dict[str, object], request_id: int | None = None
            ) -> dict[str, object]:
                del request_id
                calls.append(method)
                if method == "initialize":
                    return {"result": {}}
                if method == "thread/compact/start":
                    self.assertEqual("thread_old", params["threadId"])
                    return {"result": {"thread": {"id": "thread_compact"}, "turn": {"id": "turn_9"}}}
                raise AssertionError(method)

            async def fake_notify(method: str, params: dict[str, object]) -> None:
                del params
                calls.append(method)

            async def fake_reader_loop() -> None:
                return None

            with patch("app.runners.codex_app_server_backend.asyncio.create_subprocess_shell", return_value=fake_proc):
                backend._request = fake_request  # type: ignore[method-assign]
                backend._notify = fake_notify  # type: ignore[method-assign]
                backend._reader_loop = fake_reader_loop  # type: ignore[method-assign]
                handle = await backend.start_run(
                    RunSpec(
                        run_id="run-1",
                        issue_key="owner/repo#1",
                        candidate_id="primary",
                        cwd="/tmp/work",
                        prompt="resume",
                        session_id="thread_old",
                        session_strategy="compact",
                    )
                )
            self.assertEqual("thread_compact", handle.thread_id)
            self.assertEqual("turn_9", handle.turn_id)
            self.assertEqual(["initialize", "initialized", "thread/compact/start"], calls)

        asyncio.run(run_test())

    def test_start_run_uses_workspace_write_sandbox_values(self) -> None:
        backend = CodexAppServerBackend()

        async def run_test() -> None:
            calls: list[str] = []
            fake_proc = type("Proc", (), {"pid": 123, "stdout": None, "stdin": None})()

            async def fake_request(
                method: str, params: dict[str, object], request_id: int | None = None
            ) -> dict[str, object]:
                del request_id
                calls.append(method)
                if method == "initialize":
                    return {"result": {}}
                if method == "thread/start":
                    self.assertEqual("workspace-write", params["sandbox"])
                    return {"result": {"thread": {"id": "thread_1"}}}
                if method == "turn/start":
                    sandbox_policy = params["sandboxPolicy"]
                    assert isinstance(sandbox_policy, dict)
                    self.assertEqual("workspace-write", sandbox_policy["type"])
                    return {"result": {"turn": {"id": "turn_1"}}}
                raise AssertionError(method)

            async def fake_notify(method: str, params: dict[str, object]) -> None:
                del params
                calls.append(method)

            async def fake_reader_loop() -> None:
                return None

            with patch("app.runners.codex_app_server_backend.asyncio.create_subprocess_shell", return_value=fake_proc):
                backend._request = fake_request  # type: ignore[method-assign]
                backend._notify = fake_notify  # type: ignore[method-assign]
                backend._reader_loop = fake_reader_loop  # type: ignore[method-assign]
                handle = await backend.start_run(
                    RunSpec(
                        run_id="run-1",
                        issue_key="owner/repo#1",
                        candidate_id="primary",
                        cwd="/tmp/work",
                        prompt="implement",
                    )
                )
            self.assertEqual("thread_1", handle.thread_id)
            self.assertEqual("turn_1", handle.turn_id)
            self.assertEqual(["initialize", "initialized", "thread/start", "turn/start"], calls)

        asyncio.run(run_test())

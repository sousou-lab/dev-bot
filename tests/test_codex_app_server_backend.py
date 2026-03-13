from __future__ import annotations

import asyncio
import unittest

from app.runners.codex_app_server_backend import CodexAppServerBackend
from app.runners.execution_backend import RunHandle


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

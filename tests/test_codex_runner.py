from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from app.runners.codex_runner import CodexRunResult, CodexRunner


class CodexRunnerTests(unittest.TestCase):
    def test_run_skips_app_server_when_disabled(self) -> None:
        with TemporaryDirectory() as tmpdir:
            runner = CodexRunner(app_server_command="disabled")

            with patch.object(runner, "_run_app_server") as run_app_server:
                with patch.object(
                    runner,
                    "_run_exec_fallback",
                    return_value=CodexRunResult(
                        returncode=0,
                        stdout_path=str(Path(tmpdir) / "artifacts" / "codex_run.log"),
                        changed_files=[],
                        summary="ok",
                        mode="exec-fallback",
                    ),
                ) as run_exec_fallback:
                    result = runner.run(
                        workspace=tmpdir,
                        run_dir=tmpdir,
                        issue={},
                        requirement_summary={},
                        plan={},
                        test_plan={},
                        workflow_text="",
                    )

            run_app_server.assert_not_called()
            run_exec_fallback.assert_called_once()
            self.assertEqual("exec-fallback", result.mode)

    def test_detects_app_server_oversized_json_reader_failure(self) -> None:
        runner = CodexRunner()

        detected = runner._is_oversized_json_reader_failure(
            "Fatal error in message reader: Failed to decode JSON: "
            "JSON message exceeded maximum buffer size of 1048576 bytes..."
        )

        self.assertTrue(detected)

    def test_extract_thread_id_accepts_nested_thread_object(self) -> None:
        runner = CodexRunner()

        thread_id = runner._extract_thread_id({"thread": {"id": "thread_123"}})

        self.assertEqual("thread_123", thread_id)

    def test_extract_text_delta_reads_item_content(self) -> None:
        runner = CodexRunner()

        delta = runner._extract_text_delta(
            {"params": {"item": {"content": [{"type": "text", "text": "hello"}]}}}
        )

        self.assertEqual("hello", delta)

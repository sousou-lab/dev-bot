from __future__ import annotations
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from app.runners.codex_runner import CodexRunner, CodexRunResult
from app.runners.execution_backend import RunArtifacts, RunHandle


class CodexRunnerTests(unittest.TestCase):
    def test_run_uses_app_server_backend_abstraction(self) -> None:
        with TemporaryDirectory() as tmpdir:
            events_path = Path(tmpdir) / "artifacts" / "raw_codex_events.jsonl"
            events_path.parent.mkdir(parents=True, exist_ok=True)
            events_path.write_text('{"method":"turn/completed"}\n', encoding="utf-8")

            class _Backend:
                def __init__(self, command: str) -> None:
                    self.command = command

                async def start_run(self, spec):
                    self.spec = spec
                    return RunHandle(run_id="run", thread_id="thread_1", turn_id="turn_1", process_id=123)

                async def collect_outputs(self, _handle):
                    return RunArtifacts(
                        implementation_result={"candidate_id": "primary", "summary": "done", "changed_files": ["app/x.py"]},
                        changed_files=["app/x.py"],
                        summary="done",
                        returncode=0,
                        mode="app-server",
                        implementation_result_path=str(Path(tmpdir) / "artifacts" / "implementation_result.json"),
                        raw_event_log_path=str(events_path),
                    )

            runner = CodexRunner(app_server_backend_factory=_Backend)

            result = runner.run(
                workspace=tmpdir,
                run_dir=tmpdir,
                issue={},
                requirement_summary={},
                plan={},
                test_plan={},
                workflow_text="",
            )

            self.assertEqual("app-server", result.mode)
            self.assertEqual(["app/x.py"], result.changed_files)
            payload = json.loads((Path(tmpdir) / "artifacts" / "implementation_result.json").read_text(encoding="utf-8"))
            self.assertEqual("done", payload["summary"])

    def test_run_skips_app_server_when_disabled(self) -> None:
        with TemporaryDirectory() as tmpdir:
            runner = CodexRunner(app_server_command="disabled")

            with (
                patch.object(runner, "_run_app_server") as run_app_server,
                patch.object(
                    runner,
                    "_run_exec_fallback",
                    return_value=CodexRunResult(
                        returncode=0,
                        stdout_path=str(Path(tmpdir) / "artifacts" / "codex_run.log"),
                        changed_files=[],
                        summary="ok",
                        mode="exec-fallback",
                    ),
                ) as run_exec_fallback,
            ):
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

        delta = runner._extract_text_delta({"params": {"item": {"content": [{"type": "text", "text": "hello"}]}}})

        self.assertEqual("hello", delta)

    def test_extract_structured_output_reads_turn_completed_payload(self) -> None:
        runner = CodexRunner()

        payload = {
            "method": "turn/completed",
            "params": {
                "result": {
                    "structuredOutput": {
                        "candidate_id": "primary",
                        "summary": "done",
                        "changed_files": ["app/x.py"],
                        "tests_run": ["pytest -q"],
                    }
                }
            },
        }

        structured = runner._extract_structured_output(payload)

        self.assertEqual("done", structured["summary"])
        self.assertEqual(["app/x.py"], structured["changed_files"])

    def test_default_model_is_gpt_5_4(self) -> None:
        runner = CodexRunner()

        self.assertEqual("gpt-5.4", runner.model)

    def test_turn_start_message_requests_output_schema(self) -> None:
        runner = CodexRunner(model="gpt-5.4")

        payload = runner._build_turn_start_message(
            request_id=1,
            thread_id="thread_123",
            prompt="implement",
            workspace="/tmp/work",
        )

        self.assertEqual("gpt-5.4", payload["params"]["model"])
        self.assertEqual("dev-bot", payload["params"]["serviceName"])
        self.assertEqual(
            ["candidate_id", "summary", "changed_files"],
            payload["params"]["outputSchema"]["required"],
        )

    def test_write_implementation_result_creates_artifact(self) -> None:
        with TemporaryDirectory() as tmpdir:
            runner = CodexRunner()

            runner._write_implementation_result(
                artifacts_dir=Path(tmpdir),
                summary="implemented",
                changed_files=["app/x.py"],
            )

            payload = json.loads((Path(tmpdir) / "implementation_result.json").read_text(encoding="utf-8"))
            self.assertEqual("primary", payload["candidate_id"])
            self.assertEqual(["app/x.py"], payload["changed_files"])

    def test_write_implementation_result_prefers_structured_output_payload(self) -> None:
        with TemporaryDirectory() as tmpdir:
            runner = CodexRunner()

            runner._write_implementation_result(
                artifacts_dir=Path(tmpdir),
                summary="fallback summary",
                changed_files=["app/fallback.py"],
                payload={
                    "candidate_id": "alt1",
                    "tests_run": ["pytest -q"],
                    "followups": ["check integration"],
                    "blocked_reasons": ["needs secret"],
                },
            )

            payload = json.loads((Path(tmpdir) / "implementation_result.json").read_text(encoding="utf-8"))
            self.assertEqual("alt1", payload["candidate_id"])
            self.assertEqual("fallback summary", payload["summary"])
            self.assertEqual(["app/fallback.py"], payload["changed_files"])
            self.assertEqual(["pytest -q"], payload["tests_run"])
            self.assertEqual(["check integration"], payload["followups"])
            self.assertEqual(["needs secret"], payload["blocked_reasons"])

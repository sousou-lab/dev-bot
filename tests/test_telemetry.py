from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from app.approvals import ApprovalCoordinator
from app.contracts.workflow_schema import TelemetryConfig, WorkflowConfig
from app.pipeline import DevelopmentPipeline
from app.process_registry import ProcessRegistry
from app.state_store import FileStateStore
from app.telemetry.jsonl import JsonlTelemetrySink
from tests.helpers import make_test_settings


class JsonlTelemetrySinkTests(unittest.TestCase):
    def test_write_event_emits_otel_ready_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "telemetry" / "events.jsonl"

            JsonlTelemetrySink(path).write_event(
                event="verification_finished",
                issue_key="owner/repo#1",
                run_id="run-1",
                status="success",
                provider="claude-agent-sdk",
                model="sonnet",
                duration_ms=123,
                tokens_in=45,
                tokens_out=67,
            )

            payload = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual("verification_finished", payload["event"])
            self.assertEqual("owner/repo#1", payload["issue_key"])
            self.assertEqual("run-1", payload["run_id"])
            self.assertEqual("success", payload["status"])
            self.assertEqual("claude-agent-sdk", payload["provider"])
            self.assertEqual("sonnet", payload["model"])
            self.assertEqual(123, payload["duration_ms"])
            self.assertEqual(45, payload["tokens_in"])
            self.assertEqual(67, payload["tokens_out"])


class PipelineTelemetryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.state_store = FileStateStore(self.tmpdir.name)
        self.settings = make_test_settings(workspace_root=self.tmpdir.name, state_dir=self.tmpdir.name)
        self.pipeline = DevelopmentPipeline(
            settings=self.settings,
            state_store=self.state_store,
            github_client=MagicMock(),
            process_registry=ProcessRegistry(self.tmpdir.name),
            approval_coordinator=ApprovalCoordinator(self.state_store),
        )

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_record_telemetry_event_writes_run_scoped_jsonl(self) -> None:
        issue_key = "owner/repo#2"
        self.state_store.create_issue_record(issue_key)
        run_id = self.state_store.create_execution_run(issue_key)

        self.pipeline._record_telemetry_event(
            workflow={"config": WorkflowConfig(telemetry=TelemetryConfig())},
            issue_key=issue_key,
            run_id=run_id,
            event="run_started",
            status="running",
        )

        path = self.state_store.execution_artifacts_dir(issue_key, run_id) / "telemetry" / "events.jsonl"
        payload = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual("run_started", payload["event"])
        self.assertEqual("running", payload["status"])

    def test_record_telemetry_event_skips_when_sink_is_not_jsonl(self) -> None:
        issue_key = "owner/repo#3"
        self.state_store.create_issue_record(issue_key)
        run_id = self.state_store.create_execution_run(issue_key)

        self.pipeline._record_telemetry_event(
            workflow={"config": WorkflowConfig(telemetry=TelemetryConfig(sink="disabled"))},
            issue_key=issue_key,
            run_id=run_id,
            event="run_started",
            status="running",
        )

        path = self.state_store.execution_artifacts_dir(issue_key, run_id) / "telemetry" / "events.jsonl"
        self.assertFalse(path.exists())

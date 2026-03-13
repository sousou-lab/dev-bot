from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

from app.approvals import ApprovalCoordinator
from app.contracts.workflow_schema import IncidentBundleConfig, WorkflowConfig
from app.pipeline import DevelopmentPipeline
from app.process_registry import ProcessRegistry
from app.state_store import FileStateStore
from tests.helpers import make_test_settings


class PipelineUnitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.state_store = FileStateStore(self.tmpdir.name)
        self.settings = make_test_settings(workspace_root=self.tmpdir.name, state_dir=self.tmpdir.name)
        self.github_client = MagicMock()
        self.pipeline = DevelopmentPipeline(
            settings=self.settings,
            state_store=self.state_store,
            github_client=self.github_client,
            process_registry=ProcessRegistry(self.tmpdir.name),
            approval_coordinator=ApprovalCoordinator(self.state_store),
        )

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_commit_and_push_bootstraps_main_when_remote_branch_is_missing(self) -> None:
        completed = [
            SimpleNamespace(stdout=" M README.md\n", returncode=0),
            SimpleNamespace(stdout="", returncode=0),
            SimpleNamespace(stdout="", returncode=0),
            SimpleNamespace(stdout="", returncode=0),
            SimpleNamespace(stdout="", returncode=0),
        ]

        with patch("app.pipeline.subprocess.run", side_effect=completed) as run_mock:
            self.pipeline.workspace_manager.push_branch = MagicMock()  # type: ignore[method-assign]

            pushed = self.pipeline._commit_and_push("/tmp/work", "agent/gh-1-test", 1)

        self.assertTrue(pushed)
        self.pipeline.workspace_manager.push_branch.assert_called_once_with(  # type: ignore[attr-defined]
            "/tmp/work", "agent/gh-1-test", bootstrap_base_branch="main"
        )
        self.assertEqual(
            [
                call(["git", "-C", "/tmp/work", "status", "--porcelain"], capture_output=True, text=True, check=True),
                call(["git", "-C", "/tmp/work", "add", "-A"], check=True),
                call(
                    ["git", "-C", "/tmp/work", "commit", "-m", "feat: automated changes for issue #1"],
                    check=True,
                ),
                call(["git", "-C", "/tmp/work", "rev-parse", "--verify", "main"], capture_output=True),
                call(
                    ["git", "-C", "/tmp/work", "ls-remote", "--heads", "origin", "main"],
                    capture_output=True,
                    text=True,
                    check=True,
                ),
            ],
            run_mock.call_args_list,
        )

    def test_sync_runner_generated_artifacts_imports_implementation_result(self) -> None:
        issue_key = "owner/repo#1"
        self.state_store.create_issue_record(issue_key)
        run_id = self.state_store.create_execution_run(issue_key)
        artifacts_dir = Path(self.tmpdir.name) / "runner-artifacts"
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        (artifacts_dir / "implementation_result.json").write_text(
            '{"candidate_id":"primary","summary":"ok","changed_files":["app/x.py"]}',
            encoding="utf-8",
        )

        self.pipeline._sync_runner_generated_artifacts(issue_key=issue_key, run_id=run_id, artifacts_dir=artifacts_dir)

        payload = self.state_store.load_artifact(issue_key, "implementation_result.json")
        self.assertEqual("ok", payload["summary"])

    def test_materialize_and_finalize_incident_bundle(self) -> None:
        issue_key = "owner/repo#2"
        self.state_store.create_issue_record(issue_key)
        run_id = self.state_store.create_execution_run(issue_key)
        workflow = {"config": WorkflowConfig(incident_bundle=IncidentBundleConfig())}

        bundle_dir = self.pipeline._materialize_incident_bundle(
            workflow=workflow,
            issue_key=issue_key,
            run_id=run_id,
            workspace="/tmp/work",
            issue={"title": "Issue"},
            summary={"goal": "Goal"},
            plan={"steps": ["one"]},
            test_plan={"unit": []},
        )
        assert bundle_dir is not None

        self.pipeline._finalize_incident_bundle(bundle_dir, issue_key, run_id)

        manifest = self.state_store.load_execution_artifact(issue_key, "incident_bundle_manifest.json", run_id)
        self.assertEqual(issue_key, manifest["issue_key"])

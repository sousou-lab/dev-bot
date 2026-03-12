from __future__ import annotations

import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

from app.approvals import ApprovalCoordinator
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

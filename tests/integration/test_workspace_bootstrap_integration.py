from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.approvals import ApprovalCoordinator
from app.pipeline import DevelopmentPipeline
from app.process_registry import ProcessRegistry
from app.state_store import FileStateStore
from app.workspace_manager import WorkspaceManager
from tests.helpers import make_test_settings


class _FakeGitHubClient:
    def __init__(self, *, tmpdir: str) -> None:
        self.tmpdir = tmpdir

    def build_git_env(self) -> dict[str, str]:
        return {"GIT_TERMINAL_PROMPT": "0", "TMPDIR": self.tmpdir}

    def get_default_branch(self, repo_full_name: str) -> str:
        del repo_full_name
        return ""


class _LocalWorkspaceManager(WorkspaceManager):
    def __init__(self, *, root: str, remote_path: str) -> None:
        settings = SimpleNamespace(workspace_root=root)
        super().__init__(settings, github_client=_FakeGitHubClient(tmpdir=root))
        self.remote_path = remote_path
        self.push_calls: list[tuple[str, str, str]] = []

    def _clone_url(self, repo_full_name: str) -> str:
        del repo_full_name
        return f"file://{self.remote_path}"

    def push_branch(self, workspace: str, branch_name: str, bootstrap_base_branch: str = "") -> None:
        self.push_calls.append((workspace, branch_name, bootstrap_base_branch))


class WorkspaceBootstrapIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.remote_dir = Path(self.tmpdir.name) / "remote.git"
        subprocess.run(["git", "init", "--bare", str(self.remote_dir)], check=True, capture_output=True, text=True)
        subprocess.run(
            ["git", "--git-dir", str(self.remote_dir), "config", "core.createObject", "rename"],
            check=True,
            capture_output=True,
            text=True,
        )
        self.manager = _LocalWorkspaceManager(root=self.tmpdir.name, remote_path=str(self.remote_dir))

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_prepare_bootstraps_local_main_and_feature_branch_for_empty_repo(self) -> None:
        result = self.manager.prepare("owner/repo", 1, issue_title="Bootstrap test")

        self.assertTrue(result["is_empty_repo"])
        self.assertEqual("main", result["base_branch"])
        self.assertEqual("main", result["bootstrap_base_branch"])
        self.assertEqual("agent/gh-1-bootstrap-test", result["branch_name"])

        current_branch = subprocess.run(
            ["git", "-C", result["workspace"], "rev-parse", "--abbrev-ref", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        self.assertEqual("agent/gh-1-bootstrap-test", current_branch)

        branches = subprocess.run(
            ["git", "-C", result["workspace"], "branch", "--format=%(refname:short)"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.splitlines()
        self.assertIn("main", branches)
        self.assertIn("agent/gh-1-bootstrap-test", branches)

    def test_commit_and_push_requests_bootstrap_main_before_feature_branch(self) -> None:
        workspace_info = self.manager.prepare("owner/repo", 1, issue_title="Bootstrap test")
        workspace = Path(workspace_info["workspace"])
        (workspace / "README.md").write_text("# bootstrap\n", encoding="utf-8")

        state_store = FileStateStore(self.tmpdir.name)
        settings = make_test_settings(workspace_root=self.tmpdir.name, state_dir=self.tmpdir.name)
        pipeline = DevelopmentPipeline(
            settings=settings,
            state_store=state_store,
            github_client=MagicMock(),
            process_registry=ProcessRegistry(self.tmpdir.name),
            approval_coordinator=ApprovalCoordinator(state_store),
        )
        pipeline.workspace_manager = self.manager

        pushed = pipeline._commit_and_push(str(workspace), workspace_info["branch_name"], 1)

        self.assertTrue(pushed)
        self.assertEqual(
            [(str(workspace), workspace_info["branch_name"], "main")],
            self.manager.push_calls,
        )
        head_message = subprocess.run(
            ["git", "-C", str(workspace), "log", "-1", "--pretty=%s"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        self.assertEqual("feat: automated changes for issue #1", head_message)

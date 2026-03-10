from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.workspace_manager import WorkspaceManager


class FakeGitHubClient:
    def build_git_env(self) -> dict[str, str]:
        return {"GIT_TERMINAL_PROMPT": "0"}

    def get_default_branch(self, repo_full_name: str) -> str:
        return ""


class StubWorkspaceManager(WorkspaceManager):
    def __init__(self, root: str) -> None:
        settings = SimpleNamespace(workspace_root=root)
        super().__init__(settings, github_client=FakeGitHubClient())
        self.commands: list[list[str]] = []
        self.symbolic_ref_error = False
        self.remote_show_error = False
        self.local_heads = "main\n"
        self.remote_heads = "origin/main\n"

    def _run(self, cmd: list[str]) -> None:
        self.commands.append(cmd)
        if cmd[:3] == ["git", "clone", "--mirror"]:
            Path(cmd[-1]).mkdir(parents=True, exist_ok=True)
        elif len(cmd) >= 6 and cmd[0] == "git" and cmd[1] == "--git-dir" and cmd[3] == "worktree" and cmd[4] == "add":
            Path(cmd[5]).mkdir(parents=True, exist_ok=True)

    def _capture(self, cmd: list[str]) -> str:
        self.commands.append(cmd)
        if "symbolic-ref" in cmd:
            if self.symbolic_ref_error:
                raise subprocess.CalledProcessError(128, cmd)
            return "refs/remotes/origin/main\n"
        if cmd[-2:] == ["show", "origin"]:
            if self.remote_show_error:
                raise subprocess.CalledProcessError(128, cmd)
            return "  HEAD branch: main\n"
        if "for-each-ref" in cmd and "refs/heads" in cmd:
            return self.local_heads
        if "for-each-ref" in cmd and "refs/remotes/origin" in cmd:
            return self.remote_heads
        if "show-ref" in cmd:
            ref = cmd[-1]
            if ref.endswith("/main"):
                return "sha refs/heads/main\n"
            raise subprocess.CalledProcessError(1, cmd)
        if cmd[-3:] == ["rev-parse", "--abbrev-ref", "HEAD"]:
            return "main\n"
        if cmd[-2:] == ["--list", "agent/gh-123-add-login-timeout"]:
            return ""
        return ""


class WorkspaceManagerTests(unittest.TestCase):
    def test_prepare_uses_issue_based_workspace_layout(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = StubWorkspaceManager(tmpdir)

            result = manager.prepare("acme/api", 123, issue_title="Add login timeout")

            self.assertEqual("acme/api#123", result["workspace_key"])
            self.assertIn("/_mirrors/acme-api.git", result["mirror"])
            self.assertIn("/acme/api/issue-123-add-login-timeout/repo", result["workspace"])
            self.assertEqual("agent/gh-123-add-login-timeout", result["branch_name"])
            self.assertTrue(
                any(
                    len(cmd) >= 7
                    and cmd[:5] == ["git", "--git-dir", result["mirror"], "worktree", "add"]
                    and cmd[-1] == "main"
                    for cmd in manager.commands
                )
            )

    def test_prepare_falls_back_to_main_when_origin_head_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = StubWorkspaceManager(tmpdir)
            manager.symbolic_ref_error = True

            result = manager.prepare("acme/api", 123, issue_title="Add login timeout")

            self.assertEqual("main", result["base_branch"])
            self.assertTrue(
                any(cmd[:6] == ["git", "--git-dir", result["mirror"], "remote", "set-head", "origin"] for cmd in manager.commands)
            )

    def test_prepare_plan_workspace_falls_back_to_remote_heads_when_remote_show_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = StubWorkspaceManager(tmpdir)
            manager.remote_show_error = True

            result = manager.prepare_plan_workspace("acme/api", thread_id=99)

            self.assertEqual("main", result["base_branch"])

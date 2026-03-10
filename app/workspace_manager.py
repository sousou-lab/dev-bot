from __future__ import annotations

import subprocess
from pathlib import Path

from app.config import Settings


class WorkspaceManager:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.root = Path(settings.workspace_root).expanduser()
        self.root.mkdir(parents=True, exist_ok=True)

    def prepare(self, repo_full_name: str, issue_number: int, thread_id: int, run_root: str | None = None) -> dict:
        owner, repo = repo_full_name.split("/", 1)
        workspace_root = self.root / f"{owner}-{repo}" / f"thread-{thread_id}"
        workspace = workspace_root / "workspace"
        if not workspace.exists():
            workspace.parent.mkdir(parents=True, exist_ok=True)
            self._run(["git", "clone", self._clone_url(repo_full_name), str(workspace)])
        else:
            self._run(["git", "-C", str(workspace), "remote", "set-url", "origin", self._clone_url(repo_full_name)])
            self._run(["git", "-C", str(workspace), "fetch", "origin"])
        default_branch = self._capture(["git", "-C", str(workspace), "remote", "show", "origin"])
        head_branch = "main"
        for line in default_branch.splitlines():
            if "HEAD branch:" in line:
                head_branch = line.split(":", 1)[1].strip()
                break
        branch_name = f"agent/issue-{issue_number}-thread-{thread_id}"
        branches = self._capture(["git", "-C", str(workspace), "branch", "--list", branch_name]).strip()
        if branches:
            self._run(["git", "-C", str(workspace), "checkout", branch_name])
        else:
            self._run(["git", "-C", str(workspace), "checkout", "-B", branch_name, f"origin/{head_branch}"])
        self._run(["git", "-C", str(workspace), "config", "user.name", "dev-bot"])
        self._run(["git", "-C", str(workspace), "config", "user.email", "dev-bot@example.local"])
        resolved_run_root = Path(run_root) if run_root else workspace_root / "latest-run"
        resolved_run_root.mkdir(parents=True, exist_ok=True)
        return {
            "workspace": str(workspace),
            "branch_name": branch_name,
            "base_branch": head_branch,
            "run_root": str(resolved_run_root),
            "artifacts_dir": str(resolved_run_root / "artifacts"),
        }

    def prepare_plan_workspace(self, repo_full_name: str, thread_id: int) -> dict:
        run_root, workspace = self._prepare_root(repo_full_name, thread_id, mode="plan")
        if not workspace.exists():
            self._run(["git", "clone", "--depth", "1", self._clone_url(repo_full_name), str(workspace)])
        else:
            self._run(["git", "-C", str(workspace), "fetch", "--depth", "1", "origin"])
        default_branch = self._capture(["git", "-C", str(workspace), "remote", "show", "origin"])
        head_branch = "main"
        for line in default_branch.splitlines():
            if "HEAD branch:" in line:
                head_branch = line.split(":", 1)[1].strip()
                break
        return {
            "workspace": str(workspace),
            "base_branch": head_branch,
            "run_root": str(run_root),
        }

    def push_branch(self, workspace: str, branch_name: str) -> None:
        self._run(["git", "-C", workspace, "push", "-u", "origin", branch_name])

    def _prepare_root(self, repo_full_name: str, thread_id: int, *, mode: str) -> tuple[Path, Path]:
        owner, repo = repo_full_name.split("/", 1)
        run_root = self.root / f"{owner}-{repo}" / f"thread-{thread_id}" / mode
        run_root.mkdir(parents=True, exist_ok=True)
        workspace = run_root / "workspace"
        return run_root, workspace

    def _clone_url(self, repo_full_name: str) -> str:
        return f"https://x-access-token:{self.settings.github_token}@github.com/{repo_full_name}.git"

    def _run(self, cmd: list[str]) -> None:
        try:
            subprocess.run(cmd, check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or "").strip()
            stdout = (exc.stdout or "").strip()
            detail = stderr or stdout or str(exc)
            raise RuntimeError(f"Command {cmd!r} failed: {detail}") from exc

    def _capture(self, cmd: list[str]) -> str:
        completed = subprocess.run(cmd, check=True, capture_output=True, text=True)
        return completed.stdout

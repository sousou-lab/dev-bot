from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from app.config import Settings


class WorkspaceManager:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.root = Path(settings.workspace_root).expanduser()
        self.root.mkdir(parents=True, exist_ok=True)

    def prepare(self, repo_full_name: str, issue_number: int, thread_id: int) -> dict:
        owner, repo = repo_full_name.split("/", 1)
        run_root = self.root / f"{owner}-{repo}" / f"thread-{thread_id}"
        if run_root.exists():
            shutil.rmtree(run_root)
        run_root.mkdir(parents=True, exist_ok=True)

        clone_url = f"https://x-access-token:{self.settings.github_token}@github.com/{repo_full_name}.git"
        workspace = run_root / "workspace"
        branch_name = f"agent/issue-{issue_number}-thread-{thread_id}"
        self._run(["git", "clone", clone_url, str(workspace)])
        default_branch = self._capture(["git", "-C", str(workspace), "remote", "show", "origin"])
        head_branch = "main"
        for line in default_branch.splitlines():
            if "HEAD branch:" in line:
                head_branch = line.split(":", 1)[1].strip()
                break
        self._run(["git", "-C", str(workspace), "checkout", "-b", branch_name, f"origin/{head_branch}"])
        self._run(["git", "-C", str(workspace), "config", "user.name", "dev-bot"])
        self._run(["git", "-C", str(workspace), "config", "user.email", "dev-bot@example.local"])
        return {
            "workspace": str(workspace),
            "branch_name": branch_name,
            "base_branch": head_branch,
            "run_root": str(run_root),
            "artifacts_dir": str(run_root / "artifacts"),
        }

    def push_branch(self, workspace: str, branch_name: str) -> None:
        self._run(["git", "-C", workspace, "push", "-u", "origin", branch_name])

    def _run(self, cmd: list[str]) -> None:
        subprocess.run(cmd, check=True)

    def _capture(self, cmd: list[str]) -> str:
        completed = subprocess.run(cmd, check=True, capture_output=True, text=True)
        return completed.stdout

from __future__ import annotations

import subprocess
from pathlib import Path

from app.config import Settings
from app.github_client import GitHubIssueClient


class WorkspaceManager:
    def __init__(self, settings: Settings, github_client: GitHubIssueClient | None = None) -> None:
        self.settings = settings
        self.root = Path(settings.workspace_root).expanduser()
        self.root.mkdir(parents=True, exist_ok=True)
        self.github_client = github_client

    def prepare(
        self,
        repo_full_name: str,
        issue_number: int,
        thread_id: int | None = None,
        run_root: str | None = None,
        issue_title: str | None = None,
    ) -> dict:
        owner, repo = repo_full_name.split("/", 1)
        issue_slug = _slugify_issue_title(issue_title or f"issue-{issue_number}")
        workspace_key = f"{owner}/{repo}#{issue_number}"
        repo_root = self.root / owner / repo
        mirror = self.root / "_mirrors" / f"{owner}-{repo}.git"
        workspace_root = repo_root / f"issue-{issue_number}-{issue_slug}"
        workspace = workspace_root / "repo"
        mirror.parent.mkdir(parents=True, exist_ok=True)
        workspace_root.mkdir(parents=True, exist_ok=True)

        if not mirror.exists():
            self._run(["git", "clone", "--mirror", self._clone_url(repo_full_name), str(mirror)])
        else:
            self._run(["git", "--git-dir", str(mirror), "remote", "set-url", "origin", self._clone_url(repo_full_name)])
            self._run(["git", "--git-dir", str(mirror), "fetch", "origin", "--prune"])

        head_branch = self._resolve_default_branch(repo_full_name, git_dir=str(mirror))
        mirror_base_ref = self._resolve_mirror_base_ref(str(mirror), head_branch)
        branch_name = f"agent/gh-{issue_number}-{issue_slug}"

        if not workspace.exists():
            self._run(["git", "--git-dir", str(mirror), "worktree", "add", str(workspace), mirror_base_ref])
        else:
            self._run(["git", "-C", str(workspace), "fetch", "origin"])

        current_branch = self._capture(["git", "-C", str(workspace), "rev-parse", "--abbrev-ref", "HEAD"]).strip()
        if current_branch != branch_name:
            branches = self._capture(["git", "-C", str(workspace), "branch", "--list", branch_name]).strip()
            if branches:
                self._run(["git", "-C", str(workspace), "checkout", branch_name])
            else:
                self._run(["git", "-C", str(workspace), "checkout", "-B", branch_name, mirror_base_ref])

        self._run(["git", "-C", str(workspace), "remote", "set-url", "origin", self._clone_url(repo_full_name)])
        self._run(["git", "-C", str(workspace), "config", "user.name", "dev-bot"])
        self._run(["git", "-C", str(workspace), "config", "user.email", "dev-bot@example.local"])
        resolved_run_root = Path(run_root) if run_root else workspace_root / "latest-run"
        resolved_run_root.mkdir(parents=True, exist_ok=True)
        return {
            "workspace_key": workspace_key,
            "workspace": str(workspace),
            "workspace_root": str(workspace_root),
            "mirror": str(mirror),
            "branch_name": branch_name,
            "base_branch": head_branch,
            "run_root": str(resolved_run_root),
            "artifacts_dir": str(resolved_run_root / "artifacts"),
        }

    def prepare_plan_workspace(self, repo_full_name: str, thread_id: int | None = None, issue_number: int | None = None) -> dict:
        run_root, workspace = self._prepare_root(repo_full_name, thread_id=thread_id, issue_number=issue_number, mode="plan")
        if not workspace.exists():
            self._run(["git", "clone", "--depth", "1", self._clone_url(repo_full_name), str(workspace)])
        else:
            self._run(["git", "-C", str(workspace), "fetch", "--depth", "1", "origin"])
        head_branch = self._resolve_default_branch(repo_full_name, workspace=str(workspace))
        return {
            "workspace": str(workspace),
            "base_branch": head_branch,
            "run_root": str(run_root),
        }

    def push_branch(self, workspace: str, branch_name: str) -> None:
        self._run(["git", "-C", workspace, "push", "-u", "origin", branch_name])

    def _prepare_root(
        self,
        repo_full_name: str,
        thread_id: int | None,
        *,
        mode: str,
        issue_number: int | None = None,
    ) -> tuple[Path, Path]:
        owner, repo = repo_full_name.split("/", 1)
        identity = f"issue-{issue_number}" if issue_number is not None else f"thread-{thread_id}"
        run_root = self.root / owner / repo / identity / mode
        run_root.mkdir(parents=True, exist_ok=True)
        workspace = run_root / "workspace"
        return run_root, workspace

    def _clone_url(self, repo_full_name: str) -> str:
        return f"https://github.com/{repo_full_name}.git"

    def _resolve_default_branch(
        self,
        repo_full_name: str,
        *,
        git_dir: str | None = None,
        workspace: str | None = None,
    ) -> str:
        branch = self._default_branch_from_github(repo_full_name)
        if branch:
            return branch

        if git_dir:
            branch = self._default_branch_from_remote_head(git_dir)
            if branch:
                return branch
            branch = self._default_branch_from_local_heads(git_dir)
            if branch:
                return branch

        if workspace:
            branch = self._default_branch_from_remote_show(workspace)
            if branch:
                return branch
            branch = self._default_branch_from_workspace_heads(workspace)
            if branch:
                return branch

        raise RuntimeError(f"Could not resolve default branch for {repo_full_name}")

    def _default_branch_from_github(self, repo_full_name: str) -> str:
        if self.github_client is None:
            return ""
        try:
            return self.github_client.get_default_branch(repo_full_name)
        except RuntimeError:
            return ""

    def _default_branch_from_remote_head(self, git_dir: str) -> str:
        try:
            self._run(["git", "--git-dir", git_dir, "remote", "set-head", "origin", "--auto"])
        except RuntimeError:
            pass
        try:
            default_branch = self._capture(["git", "--git-dir", git_dir, "symbolic-ref", "refs/remotes/origin/HEAD"])
        except subprocess.CalledProcessError:
            return ""
        return default_branch.rsplit("/", 1)[-1].strip()

    def _default_branch_from_local_heads(self, git_dir: str) -> str:
        for candidate in ("main", "master"):
            if self._has_ref(git_dir=git_dir, ref=f"refs/heads/{candidate}"):
                return candidate
        branches = self._list_heads(git_dir=git_dir)
        return branches[0] if branches else ""

    def _resolve_mirror_base_ref(self, git_dir: str, branch: str) -> str:
        if self._has_ref(git_dir=git_dir, ref=f"refs/heads/{branch}"):
            return branch
        if self._has_ref(git_dir=git_dir, ref=f"refs/remotes/origin/{branch}"):
            return f"origin/{branch}"
        return branch

    def _default_branch_from_remote_show(self, workspace: str) -> str:
        try:
            default_branch = self._capture(["git", "-C", workspace, "remote", "show", "origin"])
        except subprocess.CalledProcessError:
            return ""
        for line in default_branch.splitlines():
            if "HEAD branch:" in line:
                return line.split(":", 1)[1].strip()
        return ""

    def _default_branch_from_workspace_heads(self, workspace: str) -> str:
        for candidate in ("main", "master"):
            if self._has_ref(workspace=workspace, ref=f"refs/remotes/origin/{candidate}"):
                return candidate
            if self._has_ref(workspace=workspace, ref=f"refs/heads/{candidate}"):
                return candidate
        branches = self._list_heads(workspace=workspace)
        return branches[0] if branches else ""

    def _has_ref(self, *, git_dir: str | None = None, workspace: str | None = None, ref: str) -> bool:
        cmd = ["git"]
        if git_dir:
            cmd.extend(["--git-dir", git_dir])
        elif workspace:
            cmd.extend(["-C", workspace])
        cmd.extend(["show-ref", "--verify", ref])
        try:
            self._capture(cmd)
        except subprocess.CalledProcessError:
            return False
        return True

    def _list_heads(self, *, git_dir: str | None = None, workspace: str | None = None) -> list[str]:
        cmd = ["git"]
        if git_dir:
            cmd.extend(["--git-dir", git_dir, "for-each-ref", "--format=%(refname:short)", "refs/heads"])
        elif workspace:
            cmd.extend(["-C", workspace, "for-each-ref", "--format=%(refname:short)", "refs/remotes/origin"])
        else:
            return []
        try:
            output = self._capture(cmd)
        except subprocess.CalledProcessError:
            return []
        branches = [line.rsplit("/", 1)[-1].strip() for line in output.splitlines() if line.strip()]
        return [branch for branch in branches if branch != "HEAD"]

    def _run(self, cmd: list[str]) -> None:
        try:
            subprocess.run(cmd, check=True, capture_output=True, text=True, env=self._git_env())
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or "").strip()
            stdout = (exc.stdout or "").strip()
            detail = stderr or stdout or str(exc)
            raise RuntimeError(f"Command {cmd!r} failed: {detail}") from exc

    def _capture(self, cmd: list[str]) -> str:
        completed = subprocess.run(cmd, check=True, capture_output=True, text=True, env=self._git_env())
        return completed.stdout

    def _git_env(self) -> dict[str, str] | None:
        if self.github_client is None:
            return None
        try:
            return self.github_client.build_git_env()
        except RuntimeError:
            return None


def _slugify_issue_title(title: str) -> str:
    normalized = "".join(ch.lower() if ch.isalnum() else "-" for ch in title).strip("-")
    compact = "-".join(part for part in normalized.split("-") if part)
    return compact[:48] or "work-item"

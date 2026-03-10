from __future__ import annotations

from dataclasses import dataclass
from time import monotonic

from github import Github
from github.GithubException import GithubException


@dataclass(frozen=True)
class CreatedIssue:
    number: int
    title: str
    body: str
    url: str
    repo_full_name: str


class GitHubIssueClient:
    def __init__(self, token: str) -> None:
        self.client = Github(token)
        self._repo_cache: list[str] = []
        self._repo_cache_expires_at = 0.0

    def create_issue(self, repo_full_name: str, title: str, body: str) -> CreatedIssue:
        try:
            repo = self.client.get_repo(repo_full_name)
            issue = repo.create_issue(title=title, body=body)
        except GithubException as exc:
            raise RuntimeError(f"GitHub issue creation failed: {exc.data}") from exc
        return CreatedIssue(
            number=issue.number,
            title=issue.title,
            body=body,
            url=issue.html_url,
            repo_full_name=repo_full_name,
        )

    def create_pull_request(self, repo_full_name: str, title: str, body: str, head: str, base: str, draft: bool) -> dict:
        try:
            repo = self.client.get_repo(repo_full_name)
            pr = repo.create_pull(title=title, body=body, head=head, base=base, draft=draft)
        except GithubException as exc:
            raise RuntimeError(f"GitHub PR creation failed: {exc.data}") from exc
        return {
            "number": pr.number,
            "title": pr.title,
            "url": pr.html_url,
            "repo_full_name": repo_full_name,
        }

    def create_issue_comment(self, repo_full_name: str, issue_number: int, body: str) -> dict:
        try:
            repo = self.client.get_repo(repo_full_name)
            issue = repo.get_issue(number=issue_number)
            comment = issue.create_comment(body)
        except GithubException as exc:
            raise RuntimeError(f"GitHub comment creation failed: {exc.data}") from exc
        return {"id": comment.id, "url": comment.html_url}

    def suggest_repositories(self, query: str, limit: int = 25) -> list[str]:
        repos = self._list_accessible_repositories()
        return self._filter_repositories(repos, query, limit)

    def suggest_cached_repositories(self, query: str, limit: int = 25) -> list[str]:
        return self._filter_repositories(self.cached_repositories(), query, limit)

    def cached_repositories(self) -> list[str]:
        return list(self._repo_cache)

    def warm_repository_cache(self) -> list[str]:
        return self._list_accessible_repositories()

    def _filter_repositories(self, repos: list[str], query: str, limit: int) -> list[str]:
        needle = query.strip().lower()
        if needle:
            starts = [repo for repo in repos if repo.lower().startswith(needle)]
            contains = [repo for repo in repos if needle in repo.lower() and repo not in starts]
            return (starts + contains)[:limit]
        return repos[:limit]

    def _list_accessible_repositories(self) -> list[str]:
        now = monotonic()
        if self._repo_cache and now < self._repo_cache_expires_at:
            return self._repo_cache

        try:
            user = self.client.get_user()
            repos = user.get_repos(visibility="all", affiliation="owner,collaborator,organization_member", sort="full_name")
            self._repo_cache = sorted(repo.full_name for repo in repos)
            self._repo_cache_expires_at = now + 300
            return self._repo_cache
        except GithubException as exc:
            raise RuntimeError(f"GitHub repository listing failed: {exc.data}") from exc

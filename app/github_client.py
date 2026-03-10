from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from time import monotonic
from typing import Any

try:  # pragma: no cover - depends on optional third-party package
    from github import Auth, Github, GithubIntegration
    from github.GithubException import GithubException

    GITHUB_SDK_AVAILABLE = True
except ModuleNotFoundError:  # pragma: no cover - bare test env fallback
    Auth = Github = GithubIntegration = None  # type: ignore[assignment]

    class GithubException(Exception):
        def __init__(self, data: Any | None = None) -> None:
            super().__init__(str(data))
            self.data = data

    GITHUB_SDK_AVAILABLE = False


@dataclass(frozen=True)
class CreatedIssue:
    number: int
    title: str
    body: str
    url: str
    repo_full_name: str


WORKPAD_MARKER_TEMPLATE = "<!-- dev-bot-workpad repo={repo_full_name} issue={issue_number} -->"
STATE_LABEL_PREFIX = "state:"


class GitHubIssueClient:
    def __init__(
        self,
        token: str | None = None,
        *,
        app_id: str | None = None,
        private_key_path: str | None = None,
        installation_id: str | None = None,
        project_id: str | None = None,
        project_state_field_id: str | None = None,
        project_state_option_ids: str | None = None,
    ) -> None:
        self._repo_cache: list[str] = []
        self._repo_cache_expires_at = 0.0
        self._token = (token or "").strip()
        self._app_id = (app_id or "").strip()
        self._private_key_path = (private_key_path or "").strip()
        self._installation_id = (installation_id or "").strip()
        self._project_id = (project_id or "").strip()
        self._project_state_field_id = (project_state_field_id or "").strip()
        self._project_state_option_ids = _parse_option_ids(project_state_option_ids or "")
        self.client = None

    def create_issue(self, repo_full_name: str, title: str, body: str) -> CreatedIssue:
        repo = self._get_repo(repo_full_name)
        try:
            issue = repo.create_issue(title=title, body=body)
        except GithubException as exc:
            raise RuntimeError(f"GitHub issue creation failed: {getattr(exc, 'data', exc)}") from exc
        return CreatedIssue(
            number=issue.number,
            title=issue.title,
            body=body,
            url=issue.html_url,
            repo_full_name=repo_full_name,
        )

    def create_pull_request(self, repo_full_name: str, title: str, body: str, head: str, base: str, draft: bool) -> dict:
        repo = self._get_repo(repo_full_name)
        try:
            pr = repo.create_pull(title=title, body=body, head=head, base=base, draft=draft)
        except GithubException as exc:
            raise RuntimeError(f"GitHub PR creation failed: {getattr(exc, 'data', exc)}") from exc
        return {
            "number": pr.number,
            "title": pr.title,
            "url": pr.html_url,
            "repo_full_name": repo_full_name,
        }

    def create_issue_comment(self, repo_full_name: str, issue_number: int, body: str) -> dict:
        repo = self._get_repo(repo_full_name)
        try:
            issue = repo.get_issue(number=issue_number)
            comment = issue.create_comment(body)
        except GithubException as exc:
            raise RuntimeError(f"GitHub comment creation failed: {getattr(exc, 'data', exc)}") from exc
        return {"id": comment.id, "url": comment.html_url}

    def get_issue_snapshot(self, repo_full_name: str, issue_number: int) -> dict[str, Any]:
        repo = self._get_repo(repo_full_name)
        try:
            issue = repo.get_issue(number=issue_number)
        except GithubException as exc:
            raise RuntimeError(f"GitHub issue lookup failed: {getattr(exc, 'data', exc)}") from exc
        return {
            "repo_full_name": repo_full_name,
            "number": issue.number,
            "node_id": getattr(issue, "node_id", ""),
            "title": issue.title,
            "body": issue.body or "",
            "url": issue.html_url,
            "state": getattr(issue, "state", ""),
            "labels": [label.name for label in getattr(issue, "labels", [])],
        }

    def update_issue_state(self, repo_full_name: str, issue_number: int, state: str) -> dict[str, Any]:
        repo = self._get_repo(repo_full_name)
        try:
            issue = repo.get_issue(number=issue_number)
            project_updated = self._update_project_state(getattr(issue, "node_id", ""), state)
            existing = [label.name for label in getattr(issue, "labels", [])]
            keep = [name for name in existing if not name.startswith(STATE_LABEL_PREFIX)]
            if not project_updated:
                keep.append(_state_label(state))
                issue.set_labels(*keep)
        except GithubException as exc:
            raise RuntimeError(f"GitHub issue state update failed: {getattr(exc, 'data', exc)}") from exc
        return {"state": state, "labels": keep if not project_updated else [], "project_updated": project_updated}

    def upsert_workpad_comment(
        self,
        repo_full_name: str,
        issue_number: int,
        sections: dict[str, Any],
    ) -> dict[str, Any]:
        repo = self._get_repo(repo_full_name)
        marker = WORKPAD_MARKER_TEMPLATE.format(repo_full_name=repo_full_name, issue_number=issue_number)
        body = render_workpad(repo_full_name, issue_number, sections)
        try:
            issue = repo.get_issue(number=issue_number)
            for comment in issue.get_comments():
                if marker in (comment.body or ""):
                    comment.edit(body)
                    return {"id": comment.id, "url": comment.html_url, "updated": True}
            comment = issue.create_comment(body)
        except GithubException as exc:
            raise RuntimeError(f"GitHub workpad update failed: {getattr(exc, 'data', exc)}") from exc
        return {"id": comment.id, "url": comment.html_url, "updated": False}

    def suggest_repositories(self, query: str, limit: int = 25) -> list[str]:
        repos = self._list_accessible_repositories()
        return self._filter_repositories(repos, query, limit)

    def suggest_cached_repositories(self, query: str, limit: int = 25) -> list[str]:
        repos = self.cached_repositories()
        if not repos:
            fallback = self.fallback_repositories()
            if fallback:
                repos = fallback
        return self._filter_repositories(repos, query, limit)

    def cached_repositories(self) -> list[str]:
        return list(self._repo_cache)

    def warm_repository_cache(self) -> list[str]:
        return self._list_accessible_repositories()

    def fallback_repositories(self) -> list[str]:
        owner = os.environ.get("GITHUB_OWNER", "").strip()
        repo = os.environ.get("GITHUB_REPO", "").strip()
        configured = _join_repo_name(owner=owner, repo=repo)
        return [configured] if configured else []

    def preflight(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"auth_mode": "token" if self._token else "app", "ok": False}
        try:
            repos = self._list_accessible_repositories()
            payload["ok"] = True
            payload["repo_count"] = len(repos)
            payload["sample_repos"] = repos[:5]
            return payload
        except Exception as exc:
            fallback = self.fallback_repositories()
            payload["error"] = str(exc)
            payload["fallback_repos"] = fallback
            return payload

    def get_default_branch(self, repo_full_name: str) -> str:
        repo = self._get_repo(repo_full_name)
        default_branch = str(getattr(repo, "default_branch", "")).strip()
        return default_branch

    def installation_token(self) -> str:
        if self._token:
            return self._token
        if not GITHUB_SDK_AVAILABLE:
            raise RuntimeError("PyGithub is required for GitHub App authentication")
        if not self._app_id or not self._private_key_path or not self._installation_id:
            raise RuntimeError("GitHub App credentials are not configured")

        try:
            private_key = open(self._private_key_path, encoding="utf-8").read()
            app_auth = Auth.AppAuth(int(self._app_id), private_key)
            integration = GithubIntegration(auth=app_auth)
            access_token = integration.get_access_token(int(self._installation_id))
        except (OSError, GithubException, ValueError) as exc:
            raise RuntimeError(
                "GitHub App token generation failed. "
                f"app_id={self._app_id!r} installation_id={self._installation_id!r} "
                "Check that the private key matches the App ID, the installation ID belongs to that app, "
                "and the app is installed on the target repository. "
                f"Original error: {exc}"
            ) from exc
        return access_token.token

    def build_git_env(self) -> dict[str, str]:
        token = self.installation_token()
        basic_auth = base64.b64encode(f"x-access-token:{token}".encode("utf-8")).decode("ascii")
        env = os.environ.copy()
        env.update(
            {
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
                "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {basic_auth}",
            }
        )
        return env

    def _filter_repositories(self, repos: list[str], query: str, limit: int) -> list[str]:
        needle = query.strip().lower()
        if needle:
            ranked: list[tuple[tuple[int, int, int, str], str]] = []
            for index, repo in enumerate(repos):
                score = _repository_match_score(repo, needle)
                if score is None:
                    continue
                ranked.append((score + (index, repo), repo))
            ranked.sort(key=lambda item: item[0])
            return [repo for _, repo in ranked[:limit]]
        return repos[:limit]

    def _list_accessible_repositories(self) -> list[str]:
        now = monotonic()
        if self._repo_cache and now < self._repo_cache_expires_at:
            return self._repo_cache

        try:
            if self._token and not (self._app_id and self._private_key_path and self._installation_id):
                client = self._require_client()
                user = client.get_user()
                repos = user.get_repos(
                    visibility="all",
                    affiliation="owner,collaborator,organization_member",
                    sort="full_name",
                )
                self._repo_cache = sorted(repo.full_name for repo in repos)
            else:
                self._repo_cache = self._list_installation_repositories()
            self._repo_cache_expires_at = now + 300
            return self._repo_cache
        except GithubException as exc:
            raise RuntimeError(f"GitHub repository listing failed: {getattr(exc, 'data', exc)}") from exc

    def _build_token_client(self, token: str):
        if not GITHUB_SDK_AVAILABLE:
            return None
        return Github(auth=Auth.Token(token))

    def _build_installation_client(self):
        if not GITHUB_SDK_AVAILABLE:
            return None
        return Github(auth=Auth.Token(self.installation_token()))

    def _require_client(self):
        if self.client is None:
            if self._token:
                self.client = self._build_token_client(self._token)
            elif self._app_id and self._private_key_path and self._installation_id:
                self.client = self._build_installation_client()
            else:
                raise RuntimeError("GitHub client is not configured")
        if self.client is None:
            raise RuntimeError("GitHub client could not be initialized")
        return self.client

    def _get_repo(self, repo_full_name: str):
        try:
            return self._require_client().get_repo(repo_full_name)
        except GithubException as exc:
            raise RuntimeError(f"GitHub repository access failed: {getattr(exc, 'data', exc)}") from exc

    def _update_project_state(self, issue_node_id: str, state: str) -> bool:
        if not self._project_id or not self._project_state_field_id or not self._project_state_option_ids:
            return False
        option_id = self._project_state_option_ids.get(state)
        if not option_id or not issue_node_id:
            return False
        item_id = self._find_project_item_id(issue_node_id)
        if not item_id:
            return False
        mutation = """
        mutation($projectId:ID!, $itemId:ID!, $fieldId:ID!, $optionId:String!) {
          updateProjectV2ItemFieldValue(
            input:{
              projectId:$projectId,
              itemId:$itemId,
              fieldId:$fieldId,
              value:{singleSelectOptionId:$optionId}
            }
          ) {
            projectV2Item { id }
          }
        }
        """
        self._graphql(
            mutation,
            {
                "projectId": self._project_id,
                "itemId": item_id,
                "fieldId": self._project_state_field_id,
                "optionId": option_id,
            },
        )
        return True

    def _find_project_item_id(self, issue_node_id: str) -> str:
        query = """
        query($issueId:ID!) {
          node(id:$issueId) {
            ... on Issue {
              projectItems(first:20) {
                nodes {
                  id
                  project { id }
                }
              }
            }
          }
        }
        """
        payload = self._graphql(query, {"issueId": issue_node_id})
        nodes = (
            payload.get("node", {})
            .get("projectItems", {})
            .get("nodes", [])
        )
        if not isinstance(nodes, list):
            return ""
        for item in nodes:
            if not isinstance(item, dict):
                continue
            project = item.get("project", {})
            if isinstance(project, dict) and project.get("id") == self._project_id:
                return str(item.get("id", ""))
        return ""

    def _graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        payload = json.dumps({"query": query, "variables": variables}).encode("utf-8")
        request = urllib.request.Request(
            "https://api.github.com/graphql",
            data=payload,
            headers={
                "Authorization": f"Bearer {self.installation_token()}",
                "Content-Type": "application/json",
                "Accept": "application/vnd.github+json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                body = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"GitHub GraphQL request failed: {exc}") from exc
        if body.get("errors"):
            raise RuntimeError(f"GitHub GraphQL returned errors: {body['errors']}")
        data = body.get("data", {})
        return data if isinstance(data, dict) else {}

    def _list_installation_repositories(self) -> list[str]:
        request = urllib.request.Request(
            "https://api.github.com/installation/repositories",
            headers={
                "Authorization": f"Bearer {self.installation_token()}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                body = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"GitHub installation repository listing failed: {exc}") from exc
        repositories = body.get("repositories", [])
        if not isinstance(repositories, list):
            return []
        names = [str(repo.get("full_name", "")).strip() for repo in repositories if isinstance(repo, dict)]
        return sorted(name for name in names if name)


def render_workpad(repo_full_name: str, issue_number: int, sections: dict[str, Any]) -> str:
    marker = WORKPAD_MARKER_TEMPLATE.format(repo_full_name=repo_full_name, issue_number=issue_number)
    ordered_sections = [
        "Current State",
        "Plan Approved",
        "Goal",
        "Acceptance Criteria",
        "Constraints",
        "Plan Summary",
        "Test Plan",
        "Latest Attempt",
        "Verification",
        "Branch",
        "PR",
        "Blockers",
        "Artifacts",
        "Audit Trail",
    ]
    lines = [marker, ""]
    for title in ordered_sections:
        lines.append(f"## {title}")
        lines.append(_format_workpad_value(sections.get(title, "なし")))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _format_workpad_value(value: Any) -> str:
    if isinstance(value, list):
        items = [str(item).strip() for item in value if str(item).strip()]
        return "\n".join(f"- {item}" for item in items) if items else "なし"
    text = str(value).strip()
    return text or "なし"


def _state_label(state: str) -> str:
    slug = state.strip().lower().replace(" ", "-")
    return f"{STATE_LABEL_PREFIX}{slug}"


def _parse_option_ids(raw: str) -> dict[str, str]:
    text = raw.strip()
    if not text:
        return {}
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return {}
    if not isinstance(payload, dict):
        return {}
    result: dict[str, str] = {}
    for key, value in payload.items():
        if str(key).strip() and str(value).strip():
            result[str(key).strip()] = str(value).strip()
    return result


def _join_repo_name(*, owner: str | None, repo: str | None) -> str:
    resolved_owner = (owner or "").strip()
    resolved_repo = (repo or "").strip()
    if resolved_owner and resolved_repo:
        return f"{resolved_owner}/{resolved_repo}"
    return ""


def _repository_match_score(repo: str, needle: str) -> tuple[int, int] | None:
    lowered = repo.lower()
    owner, _, repo_name = lowered.partition("/")
    if lowered == needle:
        return (0, 0)
    if repo_name == needle:
        return (1, 0)
    if repo_name.startswith(needle):
        return (2, repo_name.find(needle))
    if lowered.startswith(needle):
        return (3, lowered.find(needle))
    if needle in repo_name:
        return (4, repo_name.find(needle))
    if needle in owner:
        return (5, owner.find(needle))
    if needle in lowered:
        return (6, lowered.find(needle))
    return None

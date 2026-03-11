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
        project_plan_field_id: str | None = None,
        project_plan_option_ids: str | None = None,
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
        self._project_plan_field_id = (project_plan_field_id or "").strip()
        self._project_plan_option_ids = _parse_option_ids(project_plan_option_ids or "")
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

    def create_pull_request(
        self, repo_full_name: str, title: str, body: str, head: str, base: str, draft: bool
    ) -> dict:
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

    def merge_pull_request(self, repo_full_name: str, pull_number: int) -> dict[str, Any]:
        repo = self._get_repo(repo_full_name)
        try:
            pull = repo.get_pull(number=pull_number)
            merged = pull.merge()
        except GithubException as exc:
            raise RuntimeError(f"GitHub PR merge failed: {getattr(exc, 'data', exc)}") from exc
        return {
            "merged": bool(getattr(merged, "merged", False)),
            "message": str(getattr(merged, "message", "")),
            "sha": str(getattr(merged, "sha", "")),
        }

    def get_pull_request_status(self, repo_full_name: str, pull_number: int) -> dict[str, Any]:
        repo = self._get_repo(repo_full_name)
        try:
            pull = repo.get_pull(number=pull_number)
        except GithubException as exc:
            raise RuntimeError(f"GitHub PR lookup failed: {getattr(exc, 'data', exc)}") from exc
        return {
            "draft": bool(getattr(pull, "draft", False)),
            "mergeable": getattr(pull, "mergeable", None),
            "mergeable_state": str(getattr(pull, "mergeable_state", "") or ""),
            "head_sha": str(getattr(getattr(pull, "head", None), "sha", "") or ""),
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

    def get_issue_project_fields(self, repo_full_name: str, issue_number: int) -> dict[str, str]:
        repo = self._get_repo(repo_full_name)
        try:
            issue = repo.get_issue(number=issue_number)
        except GithubException as exc:
            raise RuntimeError(f"GitHub issue lookup failed: {getattr(exc, 'data', exc)}") from exc
        return self._load_issue_project_fields(getattr(issue, "node_id", ""))

    def list_project_issues(self) -> list[dict[str, Any]]:
        if not self._project_id:
            return []
        items: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            query = """
            query($projectId:ID!, $cursor:String) {
              node(id:$projectId) {
                ... on ProjectV2 {
                  items(first:100, after:$cursor) {
                    pageInfo {
                      hasNextPage
                      endCursor
                    }
                    nodes {
                      content {
                        ... on Issue {
                          number
                          title
                          body
                          url
                          state
                          repository { nameWithOwner }
                        }
                      }
                      fieldValues(first:100) {
                        nodes {
                          ... on ProjectV2ItemFieldSingleSelectValue {
                            name
                            field {
                              ... on ProjectV2FieldCommon {
                                id
                                name
                              }
                            }
                          }
                        }
                      }
                    }
                  }
                }
              }
            }
            """
            payload = self._graphql(query, {"projectId": self._project_id, "cursor": cursor})
            items_payload = payload.get("node", {}).get("items", {})
            nodes = items_payload.get("nodes", [])
            if isinstance(nodes, list):
                for item in nodes:
                    normalized = self._normalize_project_issue_item(item)
                    if normalized:
                        items.append(normalized)
            page_info = items_payload.get("pageInfo", {}) if isinstance(items_payload, dict) else {}
            if not isinstance(page_info, dict) or not page_info.get("hasNextPage"):
                break
            cursor = str(page_info.get("endCursor", "")).strip() or None
            if not cursor:
                break
        return items

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

    def update_issue_plan(self, repo_full_name: str, issue_number: int, plan_state: str) -> dict[str, Any]:
        repo = self._get_repo(repo_full_name)
        try:
            issue = repo.get_issue(number=issue_number)
            project_updated = self._update_project_single_select(
                issue_node_id=getattr(issue, "node_id", ""),
                field_id=self._project_plan_field_id,
                option_ids=self._project_plan_option_ids,
                value=plan_state,
            )
        except GithubException as exc:
            raise RuntimeError(f"GitHub issue plan update failed: {getattr(exc, 'data', exc)}") from exc
        return {"plan": plan_state, "project_updated": project_updated}

    def add_issue_to_project(self, repo_full_name: str, issue_number: int) -> dict[str, Any]:
        if not self._project_id:
            return {"project_updated": False}
        repo = self._get_repo(repo_full_name)
        try:
            issue = repo.get_issue(number=issue_number)
            item_id = self._find_project_item_id(getattr(issue, "node_id", ""))
            if item_id:
                return {"project_updated": True, "item_id": item_id, "already_present": True}
            mutation = """
            mutation($projectId:ID!, $contentId:ID!) {
              addProjectV2ItemById(input:{projectId:$projectId, contentId:$contentId}) {
                item { id }
              }
            }
            """
            payload = self._graphql(
                mutation,
                {
                    "projectId": self._project_id,
                    "contentId": getattr(issue, "node_id", ""),
                },
            )
        except GithubException as exc:
            raise RuntimeError(f"GitHub project add failed: {getattr(exc, 'data', exc)}") from exc
        item = payload.get("addProjectV2ItemById", {}).get("item", {}) if isinstance(payload, dict) else {}
        return {
            "project_updated": bool(item),
            "item_id": str(item.get("id", "") or ""),
            "already_present": False,
        }

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
            payload["repo_count"] = len(repos)
            payload["sample_repos"] = repos[:5]
            if self._project_id:
                project = self._load_project_configuration()
                payload["project"] = project
            payload["ok"] = True
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
        basic_auth = base64.b64encode(f"x-access-token:{token}".encode()).decode("ascii")
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
        return self._update_project_single_select(
            issue_node_id=issue_node_id,
            field_id=self._project_state_field_id,
            option_ids=self._project_state_option_ids,
            value=state,
        )

    def _load_issue_project_fields(self, issue_node_id: str) -> dict[str, str]:
        if not self._project_id or not issue_node_id:
            return {}
        query = """
        query($issueId:ID!) {
          node(id:$issueId) {
            ... on Issue {
              projectItems(first:50) {
                nodes {
                  project { id }
                  fieldValues(first:100) {
                    nodes {
                      ... on ProjectV2ItemFieldSingleSelectValue {
                        name
                        field {
                          ... on ProjectV2FieldCommon {
                            id
                            name
                          }
                        }
                      }
                    }
                  }
                }
              }
            }
          }
        }
        """
        payload = self._graphql(query, {"issueId": issue_node_id})
        nodes = payload.get("node", {}).get("projectItems", {}).get("nodes", [])
        if not isinstance(nodes, list):
            return {}
        result: dict[str, str] = {}
        for item in nodes:
            if not isinstance(item, dict):
                continue
            project = item.get("project", {})
            if not isinstance(project, dict) or project.get("id") != self._project_id:
                continue
            field_values = item.get("fieldValues", {}).get("nodes", [])
            if not isinstance(field_values, list):
                continue
            for field_value in field_values:
                if not isinstance(field_value, dict):
                    continue
                field = field_value.get("field", {})
                if not isinstance(field, dict):
                    continue
                field_id = str(field.get("id", ""))
                field_name = str(field.get("name", "")).strip()
                value_name = str(field_value.get("name", "")).strip()
                if not value_name:
                    continue
                if field_id == self._project_state_field_id or field_name == "State":
                    result["state"] = value_name
                if field_id == self._project_plan_field_id or field_name == "Plan":
                    result["plan"] = value_name
            break
        return result

    def _load_project_configuration(self) -> dict[str, Any]:
        if not self._project_id:
            return {}
        query = """
        query($projectId:ID!) {
          node(id:$projectId) {
            __typename
            ... on ProjectV2 {
              id
              title
              fields(first:50) {
                nodes {
                  ... on ProjectV2SingleSelectField {
                    id
                    name
                    options {
                      id
                      name
                    }
                  }
                }
              }
            }
          }
        }
        """
        payload = self._graphql(query, {"projectId": self._project_id})
        node = payload.get("node", {})
        if not isinstance(node, dict) or node.get("__typename") != "ProjectV2":
            raise RuntimeError(f"GitHub Project v2 lookup failed: project_id={self._project_id!r} is not accessible")

        fields_payload = node.get("fields", {})
        field_nodes = fields_payload.get("nodes", []) if isinstance(fields_payload, dict) else []
        project_fields = self._extract_project_field_configuration(field_nodes)

        state_field = project_fields.get(self._project_state_field_id)
        plan_field = project_fields.get(self._project_plan_field_id)
        if state_field is None:
            raise RuntimeError(
                "GitHub Project v2 configuration failed: "
                f"state field {self._project_state_field_id!r} was not found on project {self._project_id!r}"
            )
        if plan_field is None:
            raise RuntimeError(
                "GitHub Project v2 configuration failed: "
                f"plan field {self._project_plan_field_id!r} was not found on project {self._project_id!r}"
            )

        self._validate_project_option_ids(
            field_name="State", expected=self._project_state_option_ids, field=state_field
        )
        self._validate_project_option_ids(field_name="Plan", expected=self._project_plan_option_ids, field=plan_field)

        return {
            "id": str(node.get("id", "") or ""),
            "title": str(node.get("title", "") or ""),
            "state_field": {
                "id": state_field["id"],
                "name": state_field["name"],
                "option_count": len(state_field["options"]),
            },
            "plan_field": {
                "id": plan_field["id"],
                "name": plan_field["name"],
                "option_count": len(plan_field["options"]),
            },
        }

    def _normalize_project_issue_item(self, item: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(item, dict):
            return {}
        content = item.get("content", {})
        if not isinstance(content, dict):
            return {}
        repo = content.get("repository", {})
        repo_full_name = str(repo.get("nameWithOwner", "")).strip() if isinstance(repo, dict) else ""
        issue_number = int(content.get("number", 0) or 0)
        if not repo_full_name or issue_number <= 0:
            return {}
        fields = self._extract_project_field_values(item.get("fieldValues", {}).get("nodes", []))
        return {
            "repo_full_name": repo_full_name,
            "number": issue_number,
            "title": str(content.get("title", "") or ""),
            "body": str(content.get("body", "") or ""),
            "url": str(content.get("url", "") or ""),
            "issue_state": str(content.get("state", "") or ""),
            "state": fields.get("state", ""),
            "plan": fields.get("plan", ""),
        }

    def _extract_project_field_values(self, nodes: Any) -> dict[str, str]:
        if not isinstance(nodes, list):
            return {}
        result: dict[str, str] = {}
        for field_value in nodes:
            if not isinstance(field_value, dict):
                continue
            field = field_value.get("field", {})
            if not isinstance(field, dict):
                continue
            field_id = str(field.get("id", ""))
            field_name = str(field.get("name", "")).strip()
            value_name = str(field_value.get("name", "")).strip()
            if not value_name:
                continue
            if field_id == self._project_state_field_id or field_name == "State":
                result["state"] = value_name
            if field_id == self._project_plan_field_id or field_name == "Plan":
                result["plan"] = value_name
        return result

    def _extract_project_field_configuration(self, nodes: Any) -> dict[str, dict[str, Any]]:
        if not isinstance(nodes, list):
            return {}
        result: dict[str, dict[str, Any]] = {}
        for field in nodes:
            if not isinstance(field, dict):
                continue
            field_id = str(field.get("id", "")).strip()
            if not field_id:
                continue
            options_payload = field.get("options", [])
            options: dict[str, str] = {}
            if isinstance(options_payload, list):
                for option in options_payload:
                    if not isinstance(option, dict):
                        continue
                    option_id = str(option.get("id", "")).strip()
                    option_name = str(option.get("name", "")).strip()
                    if option_id and option_name:
                        options[option_name] = option_id
            result[field_id] = {
                "id": field_id,
                "name": str(field.get("name", "")).strip(),
                "options": options,
            }
        return result

    def _validate_project_option_ids(
        self,
        *,
        field_name: str,
        expected: dict[str, str],
        field: dict[str, Any],
    ) -> None:
        actual_options = field.get("options", {})
        if not isinstance(actual_options, dict):
            actual_options = {}
        for option_name, option_id in expected.items():
            actual_id = str(actual_options.get(option_name, "")).strip()
            if not actual_id:
                raise RuntimeError(
                    "GitHub Project v2 configuration failed: "
                    f"{field_name} option {option_name!r} is missing from field {field.get('id', '')!r}"
                )
            if actual_id != option_id:
                raise RuntimeError(
                    "GitHub Project v2 configuration failed: "
                    f"{field_name} option {option_name!r} expected id {option_id!r} but found {actual_id!r}"
                )

    def _update_project_single_select(
        self,
        *,
        issue_node_id: str,
        field_id: str,
        option_ids: dict[str, str],
        value: str,
    ) -> bool:
        if not self._project_id or not field_id or not option_ids:
            return False
        option_id = option_ids.get(value)
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
                "fieldId": field_id,
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
        nodes = payload.get("node", {}).get("projectItems", {}).get("nodes", [])
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
        names: set[str] = set()
        next_url = "https://api.github.com/installation/repositories?per_page=100"
        headers = {
            "Authorization": f"Bearer {self.installation_token()}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        while next_url:
            request = urllib.request.Request(next_url, headers=headers, method="GET")
            try:
                with urllib.request.urlopen(request, timeout=20) as response:
                    body = json.loads(response.read().decode("utf-8"))
                    link_header = response.headers.get("Link", "")
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                raise RuntimeError(f"GitHub installation repository listing failed: {exc}") from exc
            repositories = body.get("repositories", [])
            if isinstance(repositories, list):
                names.update(str(repo.get("full_name", "")).strip() for repo in repositories if isinstance(repo, dict))
            next_url = _parse_next_link(link_header)
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


def _parse_next_link(link_header: str) -> str:
    for part in link_header.split(","):
        section = part.strip()
        if 'rel="next"' not in section:
            continue
        if not section.startswith("<"):
            continue
        url, _, _rest = section.partition(">")
        return url[1:].strip()
    return ""

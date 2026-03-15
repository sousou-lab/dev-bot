from __future__ import annotations

import base64
import json
import unittest
from unittest.mock import Mock, patch

from app.github_client import GitHubIssueClient, _parse_next_link, _parse_option_ids, render_workpad


class GitHubIssueClientTests(unittest.TestCase):
    def test_suggest_cached_repositories_filters_without_network(self) -> None:
        client = GitHubIssueClient("token")
        client._repo_cache = [
            "hayasesou/analytics-stock",
            "hayasesou/dev-bot",
            "other/repo",
        ]

        repos = client.suggest_cached_repositories("haya", limit=25)

        self.assertEqual(["hayasesou/analytics-stock", "hayasesou/dev-bot"], repos)

    def test_suggest_cached_repositories_returns_all_cached_when_query_empty(self) -> None:
        client = GitHubIssueClient("token")
        client._repo_cache = ["b/repo", "a/repo"]

        repos = client.suggest_cached_repositories("", limit=25)

        self.assertEqual(["b/repo", "a/repo"], repos)

    def test_suggest_cached_repositories_prioritizes_repo_name_prefix(self) -> None:
        client = GitHubIssueClient("token")
        client._repo_cache = [
            "hayasesou/dev-bot",
            "devtools/platform",
            "other/bot-dev",
        ]

        repos = client.suggest_cached_repositories("dev", limit=25)

        self.assertEqual(["hayasesou/dev-bot", "devtools/platform", "other/bot-dev"], repos)

    def test_suggest_cached_repositories_matches_mid_string_repo_names(self) -> None:
        client = GitHubIssueClient("token")
        client._repo_cache = [
            "hayasesou/GO_piscine",
            "hayasesou/dev-bot",
            "other/repo",
        ]

        repos = client.suggest_cached_repositories("pisc", limit=25)

        self.assertEqual(["hayasesou/GO_piscine"], repos)

    def test_build_git_env_uses_header_not_tokenized_url(self) -> None:
        client = GitHubIssueClient("token-123")

        env = client.build_git_env()

        self.assertEqual("0", env["GIT_TERMINAL_PROMPT"])
        self.assertEqual("http.https://github.com/.extraheader", env["GIT_CONFIG_KEY_0"])
        expected = base64.b64encode(b"x-access-token:token-123").decode("ascii")
        self.assertEqual(f"AUTHORIZATION: basic {expected}", env["GIT_CONFIG_VALUE_0"])

    def test_require_client_caches_pat_client(self) -> None:
        client = GitHubIssueClient("token-123")
        token_client = Mock()

        with patch.object(client, "_build_token_client", return_value=token_client) as build_mock:
            first = client._require_client()
            second = client._require_client()

        self.assertIs(first, token_client)
        self.assertIs(second, token_client)
        build_mock.assert_called_once_with("token-123")

    def test_require_client_rebuilds_github_app_client_each_call(self) -> None:
        client = GitHubIssueClient(app_id="1", private_key_path="/tmp/key.pem", installation_id="2")
        first_client = Mock()
        second_client = Mock()

        with patch.object(
            client, "_build_installation_client", side_effect=[first_client, second_client]
        ) as build_mock:
            first = client._require_client()
            second = client._require_client()

        self.assertIs(first, first_client)
        self.assertIs(second, second_client)
        self.assertEqual(2, build_mock.call_count)

    def test_render_workpad_includes_marker_and_sections(self) -> None:
        body = render_workpad(
            "owner/repo",
            42,
            {"Current State": "In Progress", "Goal": "Ship fix", "Artifacts": ["plan.json", "verification.json"]},
        )

        self.assertIn("<!-- dev-bot-workpad repo=owner/repo issue=42 -->", body)
        self.assertIn("## Current State", body)
        self.assertIn("In Progress", body)
        self.assertIn("- plan.json", body)

    def test_parse_option_ids_accepts_json_mapping(self) -> None:
        parsed = _parse_option_ids('{"Ready":"opt_ready","In Progress":"opt_progress"}')

        self.assertEqual({"Ready": "opt_ready", "In Progress": "opt_progress"}, parsed)

    def test_suggest_cached_repositories_uses_installation_fallback_when_cache_empty(self) -> None:
        client = GitHubIssueClient(app_id="1", private_key_path="/tmp/key.pem", installation_id="2")
        with patch.object(
            client, "_list_installation_repositories", return_value=["hayasesou/dev-bot", "hayasesou/other"]
        ):
            repos = client.suggest_repositories("hayase", limit=25)

        self.assertEqual(["hayasesou/dev-bot", "hayasesou/other"], repos)

    def test_list_installation_repositories_follows_pagination_links(self) -> None:
        client = GitHubIssueClient(app_id="1", private_key_path="/tmp/key.pem", installation_id="2")

        class _Response:
            def __init__(self, payload: dict[str, object], link: str = "") -> None:
                self._payload = payload
                self.headers = {"Link": link}

            def read(self) -> bytes:
                return json.dumps(self._payload).encode("utf-8")

            def __enter__(self) -> _Response:
                return self

            def __exit__(self, exc_type, exc, tb) -> None:
                del exc_type, exc, tb
                return None

        responses = [
            _Response(
                {"repositories": [{"full_name": "hayasesou/GO_piscine"}]},
                '<https://api.github.com/installation/repositories?page=2>; rel="next"',
            ),
            _Response({"repositories": [{"full_name": "hayasesou/slide-system"}]}),
        ]

        with (
            patch.object(client, "installation_token", return_value="token"),
            patch("urllib.request.urlopen", side_effect=responses),
        ):
            repos = client._list_installation_repositories()

        self.assertEqual(["hayasesou/GO_piscine", "hayasesou/slide-system"], repos)

    def test_get_issue_project_fields_extracts_state_and_plan(self) -> None:
        client = GitHubIssueClient(
            "token",
            project_id="project-1",
            project_state_field_id="field-state",
            project_plan_field_id="field-plan",
        )
        repo = Mock()
        issue = Mock(node_id="issue-node")
        repo.get_issue.return_value = issue

        with (
            patch.object(client, "_get_repo", return_value=repo),
            patch.object(
                client,
                "_graphql",
                return_value={
                    "node": {
                        "projectItems": {
                            "nodes": [
                                {
                                    "project": {"id": "project-1"},
                                    "fieldValues": {
                                        "nodes": [
                                            {"name": "Ready", "field": {"id": "field-state", "name": "State"}},
                                            {"name": "Approved", "field": {"id": "field-plan", "name": "Plan"}},
                                        ]
                                    },
                                }
                            ]
                        }
                    }
                },
            ),
        ):
            fields = client.get_issue_project_fields("owner/repo", 42)

        self.assertEqual({"state": "Ready", "plan": "Approved"}, fields)

    def test_get_issue_project_fields_queries_with_expanded_field_limits(self) -> None:
        client = GitHubIssueClient(
            "token",
            project_id="project-1",
            project_state_field_id="field-state",
            project_plan_field_id="field-plan",
        )
        repo = Mock()
        issue = Mock(node_id="issue-node")
        repo.get_issue.return_value = issue

        with (
            patch.object(client, "_get_repo", return_value=repo),
            patch.object(
                client,
                "_graphql",
                return_value={"node": {"projectItems": {"nodes": []}}},
            ) as graphql_mock,
        ):
            client.get_issue_project_fields("owner/repo", 42)

        query_text = graphql_mock.call_args.args[0]
        self.assertIn("projectItems(first:50)", query_text)
        self.assertIn("fieldValues(first:100)", query_text)

    def test_preflight_loads_project_configuration_when_project_is_configured(self) -> None:
        client = GitHubIssueClient(
            "token",
            project_id="project-1",
            project_state_field_id="field-state",
            project_state_option_ids='{"Ready":"opt-ready"}',
            project_plan_field_id="field-plan",
            project_plan_option_ids='{"Approved":"opt-approved"}',
        )
        with (
            patch.object(client, "_list_accessible_repositories", return_value=["owner/repo"]),
            patch.object(
                client,
                "_graphql",
                return_value={
                    "node": {
                        "__typename": "ProjectV2",
                        "id": "project-1",
                        "title": "Dev Bot",
                        "fields": {
                            "nodes": [
                                {
                                    "id": "field-state",
                                    "name": "State",
                                    "options": [{"id": "opt-ready", "name": "Ready"}],
                                },
                                {
                                    "id": "field-plan",
                                    "name": "Plan",
                                    "options": [{"id": "opt-approved", "name": "Approved"}],
                                },
                            ]
                        },
                    }
                },
            ),
        ):
            payload = client.preflight()

        self.assertTrue(payload["ok"])
        self.assertEqual(1, payload["repo_count"])
        self.assertEqual("Dev Bot", payload["project"]["title"])
        self.assertEqual("field-state", payload["project"]["state_field"]["id"])
        self.assertEqual("field-plan", payload["project"]["plan_field"]["id"])

    def test_preflight_fails_when_project_state_option_id_does_not_match(self) -> None:
        client = GitHubIssueClient(
            "token",
            project_id="project-1",
            project_state_field_id="field-state",
            project_state_option_ids='{"Ready":"opt-expected"}',
            project_plan_field_id="field-plan",
            project_plan_option_ids='{"Approved":"opt-approved"}',
        )
        with (
            patch.object(client, "_list_accessible_repositories", return_value=["owner/repo"]),
            patch.object(
                client,
                "_graphql",
                return_value={
                    "node": {
                        "__typename": "ProjectV2",
                        "id": "project-1",
                        "title": "Dev Bot",
                        "fields": {
                            "nodes": [
                                {
                                    "id": "field-state",
                                    "name": "State",
                                    "options": [{"id": "opt-actual", "name": "Ready"}],
                                },
                                {
                                    "id": "field-plan",
                                    "name": "Plan",
                                    "options": [{"id": "opt-approved", "name": "Approved"}],
                                },
                            ]
                        },
                    }
                },
            ),
        ):
            payload = client.preflight()

        self.assertFalse(payload["ok"])
        self.assertIn("expected id 'opt-expected' but found 'opt-actual'", payload["error"])

    def test_list_project_issues_normalizes_issue_rows(self) -> None:
        client = GitHubIssueClient(
            "token",
            project_id="project-1",
            project_state_field_id="field-state",
            project_plan_field_id="field-plan",
        )
        with patch.object(
            client,
            "_graphql",
            return_value={
                "node": {
                    "items": {
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                        "nodes": [
                            {
                                "content": {
                                    "number": 42,
                                    "title": "Ship scheduler",
                                    "body": "body",
                                    "url": "https://github.com/owner/repo/issues/42",
                                    "state": "OPEN",
                                    "repository": {"nameWithOwner": "owner/repo"},
                                },
                                "fieldValues": {
                                    "nodes": [
                                        {"name": "Ready", "field": {"id": "field-state", "name": "State"}},
                                        {"name": "Approved", "field": {"id": "field-plan", "name": "Plan"}},
                                    ]
                                },
                            }
                        ],
                    }
                }
            },
        ):
            items = client.list_project_issues()

        self.assertEqual(
            [
                {
                    "repo_full_name": "owner/repo",
                    "number": 42,
                    "title": "Ship scheduler",
                    "body": "body",
                    "url": "https://github.com/owner/repo/issues/42",
                    "issue_state": "OPEN",
                    "state": "Ready",
                    "plan": "Approved",
                }
            ],
            items,
        )

    def test_list_project_issues_queries_with_expanded_field_limits(self) -> None:
        client = GitHubIssueClient(
            "token",
            project_id="project-1",
            project_state_field_id="field-state",
            project_plan_field_id="field-plan",
        )
        with patch.object(
            client,
            "_graphql",
            return_value={"node": {"items": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": []}}},
        ) as graphql_mock:
            client.list_project_issues()

        query_text = graphql_mock.call_args.args[0]
        self.assertIn("fieldValues(first:100)", query_text)

    def test_add_issue_to_project_creates_project_item_when_missing(self) -> None:
        client = GitHubIssueClient("token", project_id="project-1")
        repo = Mock()
        issue = Mock(node_id="issue-node")
        repo.get_issue.return_value = issue

        with (
            patch.object(client, "_get_repo", return_value=repo),
            patch.object(client, "_find_project_item_id", return_value=""),
            patch.object(
                client,
                "_graphql",
                return_value={"addProjectV2ItemById": {"item": {"id": "item-1"}}},
            ) as graphql_mock,
        ):
            result = client.add_issue_to_project("owner/repo", 42)

        self.assertEqual({"project_updated": True, "item_id": "item-1", "already_present": False}, result)
        query_text = graphql_mock.call_args.args[0]
        self.assertIn("addProjectV2ItemById", query_text)

    def test_merge_pull_request_returns_merge_result(self) -> None:
        client = GitHubIssueClient("token")
        repo = Mock()
        pull = Mock()
        pull.merge.return_value = Mock(merged=True, message="merged", sha="abc123")
        repo.get_pull.return_value = pull

        with patch.object(client, "_get_repo", return_value=repo):
            result = client.merge_pull_request("owner/repo", 99)

        self.assertEqual({"merged": True, "message": "merged", "sha": "abc123"}, result)

    def test_get_pull_request_status_returns_mergeability_fields(self) -> None:
        client = GitHubIssueClient("token")
        repo = Mock()
        pull = Mock(draft=False, mergeable=True, mergeable_state="clean")
        pull.head.sha = "headsha123"
        repo.get_pull.return_value = pull

        with patch.object(client, "_get_repo", return_value=repo):
            result = client.get_pull_request_status("owner/repo", 99)

        self.assertEqual(
            {"draft": False, "mergeable": True, "mergeable_state": "clean", "head_sha": "headsha123"},
            result,
        )

    def test_create_inline_review_comment_uses_pull_head_sha(self) -> None:
        client = GitHubIssueClient("token")
        repo = Mock()
        pull = Mock()
        pull.head.sha = "headsha123"
        pull.create_review_comment.return_value = Mock(id=55, html_url="https://example.invalid/comment/55")
        repo.get_pull.return_value = pull

        with patch.object(client, "_get_repo", return_value=repo):
            result = client.create_inline_review_comment(
                "owner/repo",
                pr_number=99,
                path="app/x.py",
                line=12,
                body="review body",
            )

        pull.create_review_comment.assert_called_once_with(
            body="review body",
            commit="headsha123",
            path="app/x.py",
            line=12,
        )
        self.assertEqual({"id": 55, "url": "https://example.invalid/comment/55"}, result)

    def test_parse_next_link_returns_next_url(self) -> None:
        link = (
            '<https://api.github.com/installation/repositories?page=2>; rel="next", '
            '<https://api.github.com/installation/repositories?page=3>; rel="last"'
        )

        self.assertEqual("https://api.github.com/installation/repositories?page=2", _parse_next_link(link))

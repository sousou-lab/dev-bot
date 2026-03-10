from __future__ import annotations

import base64
import unittest
from unittest.mock import patch

from app.github_client import GitHubIssueClient, _parse_option_ids, render_workpad


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
        with patch.object(client, "_list_installation_repositories", return_value=["hayasesou/dev-bot", "hayasesou/other"]):
            repos = client.suggest_repositories("hayase", limit=25)

        self.assertEqual(["hayasesou/dev-bot", "hayasesou/other"], repos)

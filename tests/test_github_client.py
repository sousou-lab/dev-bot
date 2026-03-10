from __future__ import annotations

import unittest

from app.github_client import GitHubIssueClient


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

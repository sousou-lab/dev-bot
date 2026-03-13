from __future__ import annotations

import unittest

from app.contracts.artifact_models import ReviewFinding, ReviewFindingsV1
from app.review.github_poster import GitHubReviewPoster


class _GitHubClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def create_inline_review_comment(self, *, pr_number: int, path: str, line: int, body: str) -> None:
        self.calls.append({"pr_number": pr_number, "path": path, "line": line, "body": body})


class _SyncGitHubClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def create_inline_review_comment(self, *, pr_number: int, path: str, line: int, body: str) -> None:
        self.calls.append({"pr_number": pr_number, "path": path, "line": line, "body": body})


class _RepoAwareGitHubClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def create_inline_review_comment(
        self,
        *,
        repo_full_name: str,
        pr_number: int,
        path: str,
        line: int,
        body: str,
    ) -> None:
        self.calls.append(
            {
                "repo_full_name": repo_full_name,
                "pr_number": pr_number,
                "path": path,
                "line": line,
                "body": body,
            }
        )


class GitHubReviewPosterTests(unittest.IsolatedAsyncioTestCase):
    async def test_post_inline_renders_each_finding(self) -> None:
        client = _GitHubClient()
        poster = GitHubReviewPoster(client)
        findings = ReviewFindingsV1(
            findings=[
                ReviewFinding(
                    id="R1",
                    severity="high",
                    origin="introduced",
                    confidence=0.9,
                    file="app/x.py",
                    line_start=10,
                    line_end=12,
                    claim="bug",
                    evidence=["failing test", "bad branch"],
                    verifier_status="confirmed",
                )
            ]
        )

        await poster.post_inline(pr_number=42, findings=findings)

        self.assertEqual(1, len(client.calls))
        self.assertEqual(42, client.calls[0]["pr_number"])
        self.assertEqual("app/x.py", client.calls[0]["path"])
        self.assertEqual(12, client.calls[0]["line"])
        self.assertIn("[HIGH | introduced] bug", str(client.calls[0]["body"]))
        self.assertIn("- failing test", str(client.calls[0]["body"]))

    async def test_post_inline_accepts_sync_clients(self) -> None:
        client = _SyncGitHubClient()
        poster = GitHubReviewPoster(client)

        await poster.post_inline(
            pr_number=7,
            findings=ReviewFindingsV1(
                findings=[
                    ReviewFinding(
                        id="R2",
                        severity="medium",
                        origin="pre_existing",
                        confidence=0.85,
                        file="app/y.py",
                        line_start=4,
                        line_end=4,
                        claim="watch this",
                    )
                ]
            ),
        )

        self.assertEqual(1, len(client.calls))
        self.assertEqual(7, client.calls[0]["pr_number"])

    async def test_post_inline_passes_repo_name_when_client_supports_it(self) -> None:
        client = _RepoAwareGitHubClient()
        poster = GitHubReviewPoster(client)

        await poster.post_inline(
            pr_number=11,
            repo_full_name="owner/repo",
            findings=ReviewFindingsV1(
                findings=[
                    ReviewFinding(
                        id="R3",
                        severity="high",
                        origin="introduced",
                        confidence=0.9,
                        file="app/z.py",
                        line_start=8,
                        line_end=9,
                        claim="breakage",
                    )
                ]
            ),
        )

        self.assertEqual("owner/repo", client.calls[0]["repo_full_name"])

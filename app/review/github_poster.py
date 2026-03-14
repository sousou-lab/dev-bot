from __future__ import annotations

import inspect
from typing import Any, Protocol

from app.contracts.artifact_models import ReviewFinding, ReviewFindingsV1


class InlineCommentClient(Protocol):
    async def create_inline_review_comment(self, *, pr_number: int, path: str, line: int, body: str) -> Any: ...


class GitHubReviewPoster:
    def __init__(self, github_client: InlineCommentClient) -> None:
        self.github_client = github_client

    async def post_inline(self, pr_number: int, findings: ReviewFindingsV1, *, repo_full_name: str = "") -> None:
        for finding in findings.findings:
            kwargs: dict[str, Any] = {
                "pr_number": pr_number,
                "path": finding.file,
                "line": finding.line_end,
                "body": self._render_comment(finding),
            }
            if repo_full_name:
                try:
                    signature = inspect.signature(self.github_client.create_inline_review_comment)
                except TypeError:
                    signature = None
                except ValueError:
                    signature = None
                if signature is not None and "repo_full_name" in signature.parameters:
                    kwargs["repo_full_name"] = repo_full_name
            result = self.github_client.create_inline_review_comment(**kwargs)
            if inspect.isawaitable(result):
                await result

    def _render_comment(self, finding: ReviewFinding) -> str:
        lines = [f"[{finding.severity.upper()} | {finding.origin}] {finding.claim}"]
        if finding.evidence:
            lines.append("")
            lines.append("Evidence:")
            lines.extend(f"- {item}" for item in finding.evidence)
        return "\n".join(lines)

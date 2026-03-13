from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from app.contracts.artifact_models import ReviewFinding, ReviewFindingsV1


@dataclass(frozen=True, slots=True)
class ReviewBundle:
    all_findings: ReviewFindingsV1
    postable_findings: ReviewFindingsV1


class ReviewRole(Protocol):
    async def run(self, ctx: Any) -> ReviewFindingsV1: ...


class EvidenceVerifier(Protocol):
    async def run(self, ctx: Any, findings: ReviewFindingsV1) -> ReviewFindingsV1: ...


class FindingRanker(Protocol):
    async def run(self, ctx: Any, findings: ReviewFindingsV1) -> ReviewFindingsV1: ...


class ReviewOrchestrator:
    def __init__(
        self,
        diff_reviewer: ReviewRole,
        history_reviewer: ReviewRole,
        contract_reviewer: ReviewRole,
        test_reviewer: ReviewRole,
        evidence_verifier: EvidenceVerifier,
        ranker: FindingRanker,
    ) -> None:
        self.diff_reviewer = diff_reviewer
        self.history_reviewer = history_reviewer
        self.contract_reviewer = contract_reviewer
        self.test_reviewer = test_reviewer
        self.evidence_verifier = evidence_verifier
        self.ranker = ranker

    async def run(self, ctx: Any) -> ReviewBundle:
        batches = await asyncio.gather(
            self.diff_reviewer.run(ctx),
            self.history_reviewer.run(ctx),
            self.contract_reviewer.run(ctx),
            self.test_reviewer.run(ctx),
        )
        merged = self._dedupe(batches)
        verified = await self.evidence_verifier.run(ctx, merged)
        ranked = await self.ranker.run(ctx, verified)
        threshold = float(getattr(getattr(ctx, "thresholds", None), "min_confidence_to_report", 0.80))
        postable = ReviewFindingsV1(
            findings=[
                finding
                for finding in ranked.findings
                if finding.confidence >= threshold and finding.verifier_status == "confirmed"
            ]
        )
        return ReviewBundle(all_findings=ranked, postable_findings=postable)

    def _dedupe(self, batches: Sequence[ReviewFindingsV1]) -> ReviewFindingsV1:
        seen: set[tuple[str, int, int, str]] = set()
        findings: list[ReviewFinding] = []
        for batch in batches:
            for finding in batch.findings:
                key = (finding.file, finding.line_start, finding.line_end, finding.claim)
                if key in seen:
                    continue
                seen.add(key)
                findings.append(finding)
        return ReviewFindingsV1(findings=findings)

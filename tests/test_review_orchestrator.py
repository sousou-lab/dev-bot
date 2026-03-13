from __future__ import annotations

import unittest
from dataclasses import dataclass

from app.contracts.artifact_models import ReviewFinding, ReviewFindingsV1
from app.review.orchestrator import ReviewOrchestrator


class _StaticReviewer:
    def __init__(self, findings: ReviewFindingsV1) -> None:
        self.findings = findings

    async def run(self, _ctx) -> ReviewFindingsV1:
        return self.findings


class _PassThroughVerifier:
    async def run(self, _ctx, findings: ReviewFindingsV1) -> ReviewFindingsV1:
        return findings


class _PassThroughRanker:
    async def run(self, _ctx, findings: ReviewFindingsV1) -> ReviewFindingsV1:
        return findings


@dataclass
class _Thresholds:
    min_confidence_to_report: float = 0.8


@dataclass
class _Ctx:
    thresholds: _Thresholds


class ReviewOrchestratorTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_dedupes_and_filters_postable_findings(self) -> None:
        duplicate = ReviewFinding(
            id="R1",
            severity="high",
            origin="introduced",
            confidence=0.9,
            file="app/x.py",
            line_start=10,
            line_end=12,
            claim="bug",
            verifier_status="confirmed",
        )
        low_confidence = ReviewFinding(
            id="R2",
            severity="low",
            origin="introduced",
            confidence=0.2,
            file="app/y.py",
            line_start=1,
            line_end=1,
            claim="nit",
            verifier_status="confirmed",
        )
        orchestrator = ReviewOrchestrator(
            diff_reviewer=_StaticReviewer(ReviewFindingsV1(findings=[duplicate])),
            history_reviewer=_StaticReviewer(ReviewFindingsV1(findings=[duplicate])),
            contract_reviewer=_StaticReviewer(ReviewFindingsV1(findings=[low_confidence])),
            test_reviewer=_StaticReviewer(ReviewFindingsV1(findings=[])),
            evidence_verifier=_PassThroughVerifier(),
            ranker=_PassThroughRanker(),
        )

        bundle = await orchestrator.run(_Ctx(thresholds=_Thresholds()))

        self.assertEqual(2, len(bundle.all_findings.findings))
        self.assertEqual(["R1"], [finding.id for finding in bundle.postable_findings.findings])

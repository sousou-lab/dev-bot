from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from app.contracts.artifact_models import ConstraintReportV1, PlanV2, RepoExplorerV1, RiskTestPlanV1
from app.planning.context_builder import CommitteeContextBuilder
from app.planning.plan_quality_gate import PlanQualityGate
from app.planning.roles.constraint_checker import ConstraintChecker
from app.planning.roles.plan_merger import PlanMerger
from app.planning.roles.repo_explorer import RepoExplorer
from app.planning.roles.risk_test_planner import RiskTestPlanner


@dataclass(frozen=True, slots=True)
class PlanBundle:
    repo: RepoExplorerV1
    risk: RiskTestPlanV1
    constraint: ConstraintReportV1
    merged: PlanV2


class PlannerCommittee:
    def __init__(
        self,
        repo_explorer: RepoExplorer,
        risk_test_planner: RiskTestPlanner,
        constraint_checker: ConstraintChecker,
        merger: PlanMerger,
    ) -> None:
        self.repo_explorer = repo_explorer
        self.risk_test_planner = risk_test_planner
        self.constraint_checker = constraint_checker
        self.merger = merger

    async def build_plan(self, issue_ctx: Any) -> PlanBundle:
        ctx = CommitteeContextBuilder.from_issue(issue_ctx)
        repo_out, risk_out, constraint_out = await asyncio.gather(
            self.repo_explorer.run(ctx.repo_pack),
            self.risk_test_planner.run(ctx.risk_pack),
            self.constraint_checker.run(ctx.constraint_pack),
        )
        merged = await self.merger.run(
            ctx.merge_pack(repo_out=repo_out, risk_out=risk_out, constraint_out=constraint_out)
        )
        PlanQualityGate.validate_or_raise(merged)
        return PlanBundle(repo=repo_out, risk=risk_out, constraint=constraint_out, merged=merged)

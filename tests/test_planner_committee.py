from __future__ import annotations

import unittest
from dataclasses import dataclass

from app.contracts.artifact_models import (
    ConstraintReportV1,
    DesignBranch,
    PlanTask,
    PlanV2,
    RepoExplorerV1,
    RiskTestPlanV1,
)
from app.contracts.artifact_models import (
    TestMappingItem as CriterionTestMappingItem,
)
from app.planning.committee import PlannerCommittee
from app.planning.context_builder import CommitteeContextBuilder


class _Role:
    def __init__(self, result) -> None:
        self.result = result

    async def run(self, _ctx):
        return self.result


@dataclass
class _IssueCtx:
    issue_key: str = "owner/repo#1"
    repo_root: str = "/tmp/repo"
    workpad_text: str = "workpad"
    issue_body: str = "issue body"
    acceptance_hints: list[str] | None = None
    extra_docs: list[str] | None = None


class PlannerCommitteeTests(unittest.IsolatedAsyncioTestCase):
    async def test_build_plan_runs_all_roles_and_validates_output(self) -> None:
        merged = PlanV2(
            goal="goal",
            acceptance_criteria=["ac1"],
            out_of_scope=[],
            candidate_files=["app/x.py"],
            tasks=[PlanTask(id="T1", summary="Do it", files=["app/x.py"], done_when="done")],
            design_branches=[DesignBranch(id="primary", summary="main")],
            test_mapping=[CriterionTestMappingItem(criterion="ac1", tests=["tests/test_x.py"])],
            verification_profile="python-basic",
        )
        committee = PlannerCommittee(
            repo_explorer=_Role(RepoExplorerV1(candidate_files=["app/x.py"])),
            risk_test_planner=_Role(
                RiskTestPlanV1(test_mapping=[CriterionTestMappingItem(criterion="ac1", tests=["tests/test_x.py"])])
            ),
            constraint_checker=_Role(ConstraintReportV1(out_of_scope=[])),
            merger=_Role(merged),
        )

        result = await committee.build_plan(_IssueCtx())

        self.assertEqual(["app/x.py"], result.repo.candidate_files)
        self.assertEqual("goal", result.merged.goal)

    def test_context_builder_specializes_packs_by_role(self) -> None:
        ctx = CommitteeContextBuilder.from_issue(
            _IssueCtx(
                workpad_text="Need to preserve scheduler invariants",
                issue_body="Fix planner quality issues",
                acceptance_hints=["Preserve existing workflows"],
                extra_docs=["docs/ARCHITECTURE.md", "WORKFLOW.md"],
            )
        )

        self.assertIn("Repository-facing workpad context", ctx.repo_pack.workpad_text)
        self.assertIn("Acceptance criteria", ctx.risk_pack.workpad_text)
        self.assertIn("Constraint notes", ctx.constraint_pack.workpad_text)
        self.assertNotEqual(ctx.repo_pack.workpad_text, ctx.risk_pack.workpad_text)

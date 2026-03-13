from __future__ import annotations

import unittest

from app.contracts.artifact_models import DesignBranch, PlanTask, PlanV2
from app.contracts.artifact_models import TestMappingItem as CriterionTestMappingItem
from app.implementation.candidate_policy import decide_candidates, should_enable_candidate_mode


class CandidatePolicyTests(unittest.TestCase):
    def test_candidate_mode_turns_on_for_low_confidence_multi_branch_plan(self) -> None:
        plan = PlanV2(
            goal="goal",
            acceptance_criteria=["ac1"],
            out_of_scope=[],
            candidate_files=["app/x.py"],
            tasks=[PlanTask(id="T1", summary="Do it", files=["app/x.py"], done_when="done")],
            design_branches=[
                DesignBranch(id="primary", summary="main"),
                DesignBranch(id="alt1", summary="alt"),
            ],
            test_mapping=[CriterionTestMappingItem(criterion="ac1", tests=["tests/test_x.py"])],
            verification_profile="python-basic",
            planner_confidence=0.70,
        )

        self.assertTrue(should_enable_candidate_mode(plan, rework_count=0))
        self.assertEqual(["primary", "alt1"], decide_candidates(plan, rework_count=0).candidate_ids)

    def test_candidate_mode_stays_single_for_high_confidence_plan(self) -> None:
        plan = PlanV2(
            goal="goal",
            acceptance_criteria=["ac1"],
            out_of_scope=[],
            candidate_files=["app/x.py"],
            tasks=[PlanTask(id="T1", summary="Do it", files=["app/x.py"], done_when="done")],
            design_branches=[DesignBranch(id="primary", summary="main")],
            test_mapping=[CriterionTestMappingItem(criterion="ac1", tests=["tests/test_x.py"])],
            verification_profile="python-basic",
            planner_confidence=0.95,
        )

        self.assertFalse(should_enable_candidate_mode(plan, rework_count=0))
        self.assertEqual(["primary"], decide_candidates(plan, rework_count=0).candidate_ids)

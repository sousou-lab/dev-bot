from __future__ import annotations

import unittest

from app.contracts.artifact_models import DesignBranch, PlanTask, PlanV2
from app.contracts.artifact_models import TestMappingItem as CriterionTestMappingItem
from app.planning.plan_quality_gate import PlanQualityGate


class PlanQualityGateTests(unittest.TestCase):
    def test_validate_accepts_well_formed_plan(self) -> None:
        plan = PlanV2(
            goal="goal",
            acceptance_criteria=["ac1"],
            out_of_scope=[],
            candidate_files=["app/x.py"],
            tasks=[PlanTask(id="T1", summary="Do it", files=["app/x.py"], done_when="done")],
            design_branches=[DesignBranch(id="primary", summary="main")],
            test_mapping=[CriterionTestMappingItem(criterion="ac1", tests=["tests/test_x.py"])],
            verification_profile="python-basic",
        )

        PlanQualityGate.validate_or_raise(plan)

    def test_validate_rejects_unmapped_acceptance_criteria(self) -> None:
        plan = PlanV2(
            goal="goal",
            acceptance_criteria=["ac1", "ac2"],
            out_of_scope=[],
            candidate_files=["app/x.py"],
            tasks=[PlanTask(id="T1", summary="Do it", files=["app/x.py"], done_when="done")],
            design_branches=[DesignBranch(id="primary", summary="main")],
            test_mapping=[CriterionTestMappingItem(criterion="ac1", tests=["tests/test_x.py"])],
            verification_profile="python-basic",
        )

        with self.assertRaisesRegex(ValueError, "unmapped acceptance criteria"):
            PlanQualityGate.validate_or_raise(plan)

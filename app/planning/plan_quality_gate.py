from __future__ import annotations

from app.contracts.artifact_models import PlanV2


class PlanQualityGate:
    @staticmethod
    def validate_or_raise(plan: PlanV2) -> None:
        if not plan.acceptance_criteria:
            raise ValueError("acceptance_criteria required")
        if plan.out_of_scope is None:
            raise ValueError("out_of_scope field required")
        if not plan.candidate_files:
            raise ValueError("candidate_files required")
        if not plan.verification_profile:
            raise ValueError("verification_profile required")
        if not plan.test_mapping:
            raise ValueError("test_mapping required")
        if not plan.design_branches:
            raise ValueError("design_branches required")

        mapped = {item.criterion for item in plan.test_mapping}
        missing = [criterion for criterion in plan.acceptance_criteria if criterion not in mapped]
        if missing:
            raise ValueError(f"unmapped acceptance criteria: {missing}")

    @staticmethod
    def should_enable_candidate_mode(plan: PlanV2, rework_count: int) -> bool:
        return (rework_count >= 1 or plan.planner_confidence < 0.75) and len(plan.design_branches) >= 2

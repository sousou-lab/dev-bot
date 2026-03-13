from __future__ import annotations

from dataclasses import dataclass

from app.contracts.artifact_models import PlanV2


@dataclass(frozen=True, slots=True)
class CandidateDecision:
    enabled: bool
    candidate_ids: list[str]


def should_enable_candidate_mode(plan: PlanV2, rework_count: int) -> bool:
    return (rework_count >= 1 or plan.planner_confidence < 0.75) and len(plan.design_branches) >= 2


def decide_candidates(plan: PlanV2, rework_count: int) -> CandidateDecision:
    if should_enable_candidate_mode(plan, rework_count):
        return CandidateDecision(enabled=True, candidate_ids=["primary", "alt1"])
    return CandidateDecision(enabled=False, candidate_ids=["primary"])


def select_winner(primary, alt1) -> str:
    def score(candidate) -> int:
        total = 0
        total += 100 if candidate.verification.hard_checks_pass else -1000
        total -= 50 * candidate.review.high_count
        total -= 20 * candidate.review.medium_count
        total -= 10 * candidate.scope.unexpected_file_count
        total += 10 * candidate.summary.plan_alignment_score
        return total

    return max((score(primary), "primary"), (score(alt1), "alt1"))[1]

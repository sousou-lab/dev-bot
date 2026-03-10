from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ProofCheckResult:
    complete: bool
    missing_artifacts: list[str]


def required_artifacts(workflow: dict[str, Any]) -> list[str]:
    proof = workflow.get("proof_of_work", {})
    if not isinstance(proof, dict):
        return []
    items = proof.get("required_artifacts", [])
    if not isinstance(items, list):
        return []
    return [str(item) for item in items if str(item).strip()]


def evaluate_proof_of_work(workflow: dict[str, Any], available_artifacts: set[str]) -> ProofCheckResult:
    required = required_artifacts(workflow)
    missing = [item for item in required if item not in available_artifacts]
    return ProofCheckResult(complete=not missing, missing_artifacts=missing)

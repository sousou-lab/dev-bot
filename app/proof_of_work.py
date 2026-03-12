from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ProofCheckResult:
    complete: bool
    missing_artifacts: list[str]


def required_artifacts(workflow: dict[str, Any]) -> list[str]:
    verification = workflow.get("verification", {})
    if isinstance(verification, dict):
        items = verification.get("required_artifacts", [])
        if isinstance(items, list):
            normalized = [str(item) for item in items if str(item).strip()]
            if normalized:
                return normalized
    proof = workflow.get("proof_of_work", {})
    if isinstance(proof, dict):
        items = proof.get("required_artifacts", [])
        if isinstance(items, list):
            return [str(item) for item in items if str(item).strip()]
    return []


def evaluate_proof_of_work(workflow: dict[str, Any], available_artifacts: set[str]) -> ProofCheckResult:
    required = required_artifacts(workflow)
    missing = [item for item in required if item not in available_artifacts]
    return ProofCheckResult(complete=not missing, missing_artifacts=missing)

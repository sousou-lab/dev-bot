from __future__ import annotations

from typing import Any


def grade_plan_submission(task: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    golden = task.get("golden", {})
    required_files = {str(item) for item in golden.get("candidate_files", [])}
    expected_criteria = {str(item) for item in golden.get("acceptance_criteria", [])}

    actual_files = {str(item) for item in plan.get("candidate_files", [])}
    actual_criteria = {str(item) for item in plan.get("acceptance_criteria", [])}

    missing_files = sorted(required_files - actual_files)
    missing_criteria = sorted(expected_criteria - actual_criteria)

    return {
        "task_id": task.get("task_id", ""),
        "category": "planning",
        "passed": not missing_files and not missing_criteria,
        "missing_candidate_files": missing_files,
        "missing_acceptance_criteria": missing_criteria,
        "score": 1.0 if not missing_files and not missing_criteria else 0.0,
    }

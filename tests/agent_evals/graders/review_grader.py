from __future__ import annotations

from typing import Any


def grade_review_findings(task: dict[str, Any], findings: dict[str, Any]) -> dict[str, Any]:
    golden = task.get("golden", {})
    expected_ids = {str(item) for item in golden.get("expected_finding_ids", [])}
    actual_ids = {str(item.get("id", "")) for item in findings.get("findings", []) if isinstance(item, dict)}

    missing_ids = sorted(expected_ids - actual_ids)

    return {
        "task_id": task.get("task_id", ""),
        "category": "review",
        "passed": not missing_ids,
        "missing_finding_ids": missing_ids,
        "score": 1.0 if not missing_ids else 0.0,
    }

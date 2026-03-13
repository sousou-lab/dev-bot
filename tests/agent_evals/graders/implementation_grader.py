from __future__ import annotations

from typing import Any


def grade_implementation_result(task: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    expected_success = bool(task.get("golden", {}).get("success", True))
    hard_checks_pass = bool(result.get("hard_checks_pass", False))
    actual_success = bool(result.get("success", False))

    passed = actual_success == expected_success and hard_checks_pass
    return {
        "task_id": task.get("task_id", ""),
        "category": "implementation",
        "passed": passed,
        "score": 1.0 if passed else 0.0,
        "expected_success": expected_success,
        "actual_success": actual_success,
        "hard_checks_pass": hard_checks_pass,
    }

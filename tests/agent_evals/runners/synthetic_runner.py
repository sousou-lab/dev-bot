from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tests.agent_evals.graders.implementation_grader import grade_implementation_result
from tests.agent_evals.graders.planning_grader import grade_plan_submission
from tests.agent_evals.graders.review_grader import grade_review_findings

REQUIRED_TASK_KEYS = ("task_id", "language", "fixture_repo", "task_type", "issue_prompt", "golden")


def load_task(task_path: str | Path) -> dict[str, Any]:
    payload = json.loads(Path(task_path).read_text(encoding="utf-8"))
    missing = [key for key in REQUIRED_TASK_KEYS if key not in payload]
    if missing:
        raise ValueError(f"synthetic task missing required keys: {', '.join(missing)}")
    return payload


@dataclass(frozen=True, slots=True)
class SyntheticTask:
    path: Path
    payload: dict[str, Any]


class SyntheticEvalRunner:
    def __init__(self, fixtures_root: str | Path) -> None:
        self.fixtures_root = Path(fixtures_root)

    def list_tasks(self, language: str = "python") -> list[SyntheticTask]:
        root = self.fixtures_root / language
        tasks: list[SyntheticTask] = []
        for task_path in sorted(root.glob("*/task.json")):
            tasks.append(SyntheticTask(path=task_path, payload=load_task(task_path)))
        return tasks

    def grade(self, task: SyntheticTask, submission: dict[str, Any]) -> dict[str, Any]:
        task_type = str(task.payload.get("task_type", "")).strip()
        if task_type == "planning":
            return grade_plan_submission(task.payload, submission)
        if task_type == "review":
            return grade_review_findings(task.payload, submission)
        if task_type == "implementation":
            return grade_implementation_result(task.payload, submission)
        raise ValueError(f"unsupported synthetic task_type: {task_type}")

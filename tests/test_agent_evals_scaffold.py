from __future__ import annotations

import json
import unittest
from pathlib import Path

from tests.agent_evals.graders.implementation_grader import grade_implementation_result
from tests.agent_evals.graders.planning_grader import grade_plan_submission
from tests.agent_evals.graders.review_grader import grade_review_findings
from tests.agent_evals.runners.replay_runner import ReplayEvalRunner
from tests.agent_evals.runners.synthetic_runner import SyntheticEvalRunner, load_task


class AgentEvalScaffoldTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixtures_root = Path("tests/agent_evals/fixtures")

    def test_synthetic_runner_lists_phase1_tasks(self) -> None:
        runner = SyntheticEvalRunner(self.fixtures_root)

        tasks = runner.list_tasks("python")

        self.assertEqual(
            [
                "py-impl-001",
                "py-planning-001",
                "py-planning-002",
                "py-review-001",
            ],
            [task.payload["task_id"] for task in tasks],
        )

    def test_planning_grader_passes_for_matching_submission(self) -> None:
        task = load_task(self.fixtures_root / "python" / "planning-basic" / "task.json")
        result = grade_plan_submission(
            task,
            {
                "goal": "Fix scheduler gate validation",
                "acceptance_criteria": task["golden"]["acceptance_criteria"],
                "candidate_files": task["golden"]["candidate_files"],
            },
        )

        self.assertTrue(result["passed"])
        self.assertEqual(1.0, result["score"])

    def test_review_and_implementation_graders_report_expected_shape(self) -> None:
        review_task = load_task(self.fixtures_root / "python" / "review-seeded-bugs" / "task.json")
        review_result = grade_review_findings(review_task, {"findings": [{"id": "R-approval-gate"}]})
        self.assertTrue(review_result["passed"])

        implementation_task = load_task(self.fixtures_root / "python" / "debug-bundle" / "task.json")
        implementation_result = grade_implementation_result(
            implementation_task,
            {"success": True, "hard_checks_pass": True},
        )
        self.assertTrue(implementation_result["passed"])

    def test_replay_runner_loads_json_capture(self) -> None:
        capture_path = Path("tests/agent_evals/tmp_capture.json")
        capture_path.write_text(json.dumps({"run_id": "run-1", "result": "ok"}), encoding="utf-8")
        self.addCleanup(capture_path.unlink)

        payload = ReplayEvalRunner().load_capture(capture_path)

        self.assertEqual("run-1", payload["run_id"])

    def test_schema_files_exist(self) -> None:
        schema_root = Path("tests/agent_evals/schemas")

        self.assertTrue((schema_root / "synthetic_task_v1.json").exists())
        self.assertTrue((schema_root / "plan_v2.json").exists())
        self.assertTrue((schema_root / "review_findings_v1.json").exists())
        self.assertTrue((schema_root / "implementation_result_v1.json").exists())

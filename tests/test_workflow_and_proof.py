from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.proof_of_work import evaluate_proof_of_work
from app.workflow_loader import load_workflow, workflow_text


class WorkflowAndProofTests(unittest.TestCase):
    def test_load_workflow_parses_front_matter(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "WORKFLOW.md"
            path.write_text(
                "---\ncommands:\n  test:\n    - pytest -q\nproof_of_work:\n  required_artifacts:\n    - plan.json\n---\n\nbody",
                encoding="utf-8",
            )

            payload = load_workflow(workspace=tmpdir)

            self.assertEqual(["pytest -q"], payload["commands"]["test"])
            self.assertIn("proof_of_work", payload)
            self.assertIn("body", payload["contract_body"])
            self.assertIn("required_artifacts", workflow_text(workspace=tmpdir))

    def test_evaluate_proof_of_work_returns_missing_artifacts(self) -> None:
        workflow = {"proof_of_work": {"required_artifacts": ["plan.json", "review_summary.json"]}}

        result = evaluate_proof_of_work(workflow, {"plan.json"})

        self.assertFalse(result.complete)
        self.assertEqual(["review_summary.json"], result.missing_artifacts)

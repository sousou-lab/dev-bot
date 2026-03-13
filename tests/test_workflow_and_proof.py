from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.proof_of_work import evaluate_proof_of_work
from app.workflow_loader import load_workflow, load_workflow_definition, workflow_text


class WorkflowAndProofTests(unittest.TestCase):
    def test_load_workflow_parses_front_matter(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "WORKFLOW.md"
            path.write_text(
                (
                    "---\n"
                    "planning:\n"
                    "  provider: claude-agent-sdk\n"
                    "  committee:\n"
                    "    roles:\n"
                    "      merger:\n"
                    "        mode: query\n"
                    "        allowed_tools: [Read]\n"
                    "        disallowed_tools: [Write]\n"
                    "        output_schema: plan_v2\n"
                    "implementation:\n"
                    "  backend: codex-app-server\n"
                    "review:\n"
                    "  provider: claude-agent-sdk\n"
                    "verification:\n"
                    "  required_artifacts:\n"
                    "    - plan.json\n"
                    "    - verification_plan.json\n"
                    "    - runner_metadata.json\n"
                    "  required_checks:\n"
                    "    - name: tests\n"
                    "      command: pytest -q\n"
                    "telemetry:\n"
                    "  sink: jsonl\n"
                    "  otel_compatible_fields: true\n"
                    "debug:\n"
                    "  incident_bundle:\n"
                    "    enabled: true\n"
                    "---\n\nbody"
                ),
                encoding="utf-8",
            )

            payload = load_workflow(workspace=tmpdir)

            self.assertEqual("pytest -q", payload["verification"]["required_checks"][0]["command"])
            self.assertIn("verification", payload)
            self.assertIn("body", payload["contract_body"])
            self.assertIn("required_artifacts", workflow_text(workspace=tmpdir))
            self.assertEqual("claude-agent-sdk", payload["config"].planning.provider)
            self.assertEqual("jsonl", payload["config"].telemetry.sink)

    def test_load_workflow_definition_returns_typed_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "WORKFLOW.md"
            path.write_text(
                (
                    "---\n"
                    "planning:\n"
                    "  provider: claude-agent-sdk\n"
                    "  committee:\n"
                    "    roles:\n"
                    "      merger:\n"
                    "        mode: query\n"
                    "        allowed_tools: [Read]\n"
                    "        disallowed_tools: [Write]\n"
                    "        output_schema: plan_v2\n"
                    "implementation:\n"
                    "  backend: codex-app-server\n"
                    "  candidate_mode:\n"
                    "    max_parallel_editors: 2\n"
                    "review:\n"
                    "  provider: claude-agent-sdk\n"
                    "---\n"
                ),
                encoding="utf-8",
            )

            definition = load_workflow_definition(workspace=tmpdir)

            assert definition is not None
            self.assertEqual("codex-app-server", definition.config.implementation.backend)
            self.assertEqual(2, definition.config.implementation.candidate_mode.max_parallel_editors)

    def test_load_workflow_definition_records_validation_error_when_not_strict(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "WORKFLOW.md"
            path.write_text("---\nplanning:\n  enabled: true\n---\nbody\n", encoding="utf-8")

            definition = load_workflow_definition(workspace=tmpdir)

            assert definition is not None
            self.assertIsNone(definition.config)
            self.assertIn("planning.provider is required", definition.config_error)

    def test_evaluate_proof_of_work_returns_missing_artifacts(self) -> None:
        workflow = {"verification": {"required_artifacts": ["plan.json", "review_summary.json"]}}

        result = evaluate_proof_of_work(workflow, {"plan.json"})

        self.assertFalse(result.complete)
        self.assertEqual(["review_summary.json"], result.missing_artifacts)

    def test_evaluate_proof_of_work_falls_back_to_legacy_key(self) -> None:
        workflow = {"proof_of_work": {"required_artifacts": ["plan.json", "review_summary.json"]}}

        result = evaluate_proof_of_work(workflow, {"plan.json"})

        self.assertFalse(result.complete)
        self.assertEqual(["review_summary.json"], result.missing_artifacts)

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.workflow_loader import load_workflow, load_workflow_definition


class WorkflowLoaderTests(unittest.TestCase):
    def test_load_workflow_definition_returns_none_when_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            self.assertIsNone(load_workflow_definition(workspace=tmpdir))

    def test_load_workflow_returns_raw_text_when_front_matter_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "WORKFLOW.md"
            path.write_text("planning:\n  provider: claude-agent-sdk\n", encoding="utf-8")

            payload = load_workflow(workspace=tmpdir)

            self.assertEqual("planning:\n  provider: claude-agent-sdk\n", payload["raw_text"])
            self.assertEqual("planning:\n  provider: claude-agent-sdk", payload["contract_body"])
            self.assertIsNone(payload["config"])

    def test_load_workflow_keeps_raw_payload_when_validation_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "WORKFLOW.md"
            path.write_text("---\nplanning:\n  enabled: true\n---\nbody\n", encoding="utf-8")

            payload = load_workflow(workspace=tmpdir)

            self.assertEqual({"enabled": True}, payload["planning"])
            self.assertEqual("body", payload["contract_body"])
            self.assertIsNone(payload["config"])
            self.assertIn("planning.provider is required", payload["config_error"])

    def test_load_workflow_definition_raises_validation_errors_in_strict_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "WORKFLOW.md"
            path.write_text("---\nplanning:\n  enabled: true\n---\nbody\n", encoding="utf-8")

            definition = load_workflow_definition(workspace=tmpdir)

            assert definition is not None
            self.assertIsNone(definition.config)
            self.assertEqual("body", definition.prompt_body)

            with self.assertRaisesRegex(Exception, "planning.provider is required"):
                load_workflow_definition(workspace=tmpdir, strict=True)

    def test_load_workflow_definition_keeps_last_known_good_config_on_invalid_reload(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "WORKFLOW.md"
            path.write_text("---\nplanning:\n  provider: claude-agent-sdk\n---\nbody\n", encoding="utf-8")

            first = load_workflow_definition(workspace=tmpdir)

            assert first is not None
            self.assertIsNotNone(first.config)
            self.assertFalse(first.uses_cached_config)

            path.write_text("---\nplanning:\n  enabled: true\n---\nbody\n", encoding="utf-8")

            reloaded = load_workflow_definition(workspace=tmpdir)

            assert reloaded is not None
            self.assertIsNotNone(reloaded.config)
            self.assertEqual("claude-agent-sdk", reloaded.config.planning.provider)
            self.assertTrue(reloaded.uses_cached_config)
            self.assertIn("using last known good workflow config", reloaded.config_error)

    def test_load_workflow_definition_keeps_last_known_good_config_on_malformed_front_matter(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "WORKFLOW.md"
            path.write_text("---\nplanning:\n  provider: claude-agent-sdk\n---\nbody\n", encoding="utf-8")

            first = load_workflow_definition(workspace=tmpdir)

            assert first is not None
            self.assertIsNotNone(first.config)

            path.write_text("---\nplanning:\n  provider: claude-agent-sdk\nbody\n", encoding="utf-8")

            reloaded = load_workflow_definition(workspace=tmpdir)
            payload = load_workflow(workspace=tmpdir)

            assert reloaded is not None
            self.assertIsNotNone(reloaded.config)
            self.assertTrue(reloaded.uses_cached_config)
            self.assertIn("YAML front matter", reloaded.config_error)
            self.assertTrue(payload["uses_cached_config"])

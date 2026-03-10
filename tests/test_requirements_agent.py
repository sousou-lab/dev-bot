from __future__ import annotations

import tempfile
import unittest
from types import SimpleNamespace

from app.requirements_agent import RequirementsAgent


class RequirementsAgentNormalizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.agent = RequirementsAgent(
            settings=SimpleNamespace(anthropic_api_key="", runs_root=self.tempdir.name),
            runs_root=self.tempdir.name,
        )

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_normalize_payload_accepts_missing_summary_object(self) -> None:
        payload = self.agent._normalize_payload({"status": "questioning", "reply": "ok", "summary": None})

        self.assertEqual("questioning", payload["status"])
        self.assertEqual("ok", payload["reply"])
        self.assertEqual("", payload["summary"]["goal"])
        self.assertEqual([], payload["summary"]["open_questions"])

    def test_normalize_payload_coerces_string_lists(self) -> None:
        payload = self.agent._normalize_payload(
            {
                "status": "ready_for_confirmation",
                "reply": "done",
                "summary": {
                    "goal": "ship it",
                    "in_scope": "feature x",
                    "constraints": ["a", "", "b"],
                },
            }
        )

        self.assertEqual("ready_for_confirmation", payload["status"])
        self.assertEqual(["feature x"], payload["summary"]["in_scope"])
        self.assertEqual(["a", "b"], payload["summary"]["constraints"])

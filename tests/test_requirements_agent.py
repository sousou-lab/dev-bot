from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
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

    def test_normalize_payload_populates_extended_summary_defaults(self) -> None:
        payload = self.agent._normalize_payload({"status": "questioning", "reply": "ok", "summary": {}})

        self.assertEqual("simple", payload["summary"]["complexity"])
        self.assertEqual("", payload["summary"]["recommended_direction"])
        self.assertEqual([], payload["summary"]["solution_options"])
        self.assertEqual([], payload["summary"]["decision_hints"])
        self.assertEqual(
            {
                "priority_preference": "unknown",
                "change_scope_preference": "unknown",
                "compatibility_preference": "unknown",
                "risk_tolerance": "unknown",
                "delivery_expectation": "unknown",
            },
            payload["summary"]["preferences"],
        )

    def test_normalize_payload_filters_invalid_extended_summary_values(self) -> None:
        payload = self.agent._normalize_payload(
            {
                "status": "ready_for_confirmation",
                "reply": "done",
                "summary": {
                    "complexity": "invalid",
                    "preferred_outcomes": "fast rollout",
                    "tradeoffs": ["keep API", ""],
                    "preferences": {
                        "priority_preference": "speed",
                        "change_scope_preference": "bad-value",
                    },
                    "decision_hints": [
                        {
                            "question": "priority",
                            "selected_label": "fast",
                            "normalized_key": "priority_preference",
                            "normalized_value": "speed",
                            "source": "user_selection",
                        },
                        {
                            "question": "bad",
                            "selected_label": "bad",
                            "normalized_key": "missing",
                            "normalized_value": "nope",
                            "source": "other",
                        },
                    ],
                    "solution_options": [
                        {
                            "name": "Option A",
                            "summary": "Smallest change",
                            "pros": "low risk",
                            "cons": ["future debt", ""],
                        },
                        {"name": "", "summary": "", "pros": [], "cons": []},
                    ],
                },
            }
        )

        self.assertEqual("simple", payload["summary"]["complexity"])
        self.assertEqual(["fast rollout"], payload["summary"]["preferred_outcomes"])
        self.assertEqual(["keep API"], payload["summary"]["tradeoffs"])
        self.assertEqual("speed", payload["summary"]["preferences"]["priority_preference"])
        self.assertEqual("unknown", payload["summary"]["preferences"]["change_scope_preference"])
        self.assertEqual(1, len(payload["summary"]["decision_hints"]))
        self.assertEqual(1, len(payload["summary"]["solution_options"]))
        self.assertEqual(["low risk"], payload["summary"]["solution_options"][0]["pros"])

    def test_load_messages_reads_issue_bound_conversation(self) -> None:
        binding_dir = Path(self.tempdir.name) / "bindings" / "discord_threads"
        binding_dir.mkdir(parents=True, exist_ok=True)
        (binding_dir / "123.json").write_text(json.dumps({"issue_key": "owner/repo#42"}), encoding="utf-8")
        issue_dir = Path(self.tempdir.name) / "issues" / "owner__repo__42"
        issue_dir.mkdir(parents=True, exist_ok=True)
        (issue_dir / "conversation.jsonl").write_text(
            json.dumps({"role": "user", "content": "hello"}) + "\n",
            encoding="utf-8",
        )

        rows = self.agent._load_messages(123)

        self.assertEqual([{"role": "user", "content": "hello"}], rows)

    def test_load_messages_prefers_issue_bound_conversation_over_draft(self) -> None:
        draft_dir = Path(self.tempdir.name) / "drafts" / "123"
        draft_dir.mkdir(parents=True, exist_ok=True)
        (draft_dir / "conversation.jsonl").write_text(
            json.dumps({"role": "user", "content": "draft"}) + "\n",
            encoding="utf-8",
        )
        binding_dir = Path(self.tempdir.name) / "bindings" / "discord_threads"
        binding_dir.mkdir(parents=True, exist_ok=True)
        (binding_dir / "123.json").write_text(json.dumps({"issue_key": "owner/repo#42"}), encoding="utf-8")
        issue_dir = Path(self.tempdir.name) / "issues" / "owner__repo__42"
        issue_dir.mkdir(parents=True, exist_ok=True)
        (issue_dir / "conversation.jsonl").write_text(
            json.dumps({"role": "user", "content": "issue"}) + "\n",
            encoding="utf-8",
        )

        rows = self.agent._load_messages(123)

        self.assertEqual([{"role": "user", "content": "issue"}], rows)

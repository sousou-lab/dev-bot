from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.requirements_flow import RequirementsFlow


class RequirementsFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.flow = RequirementsFlow(runs_root=self.tempdir.name)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_build_planning_summary_uses_planning_keys(self) -> None:
        summary = self.flow._build_planning_summary(
            {
                "change_type": "新機能",
                "completion": "Slack 通知を追加する",
                "out_of_scope": "既存 API 変更なし",
                "users": "運用担当",
                "constraints": "DB変更なし",
            }
        )

        self.assertEqual("Slack 通知を追加する", summary["goal"])
        self.assertEqual(["Slack 通知を追加する"], summary["acceptance_criteria"])
        self.assertEqual(["既存 API 変更なし"], summary["out_of_scope"])

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

        rows = self.flow._load_messages(123)

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

        rows = self.flow._load_messages(123)

        self.assertEqual([{"role": "user", "content": "issue"}], rows)

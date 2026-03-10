from __future__ import annotations

import tempfile
import unittest

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

from __future__ import annotations

import unittest
from unittest.mock import patch

from app.runners.claude_runner import ClaudeRunner


class ClaudeRunnerReviewTests(unittest.TestCase):
    def test_review_aggregates_findings_and_keeps_legacy_summary_shape(self) -> None:
        runner = ClaudeRunner(api_key=None)
        payloads = [
            {
                "findings": [
                    {
                        "id": "R1",
                        "severity": "high",
                        "origin": "introduced",
                        "confidence": 0.92,
                        "file": "app/x.py",
                        "line_start": 10,
                        "line_end": 12,
                        "claim": "Regression in approval gate",
                        "evidence": ["diff removes guard"],
                        "verifier_status": "unverified",
                    }
                ]
            },
            {"findings": []},
            {
                "findings": [
                    {
                        "id": "R2",
                        "severity": "medium",
                        "origin": "test_reviewer",
                        "confidence": 0.5,
                        "file": "tests/test_x.py",
                        "line_start": 1,
                        "line_end": 1,
                        "claim": "Missing regression test",
                        "evidence": ["no test coverage"],
                        "verifier_status": "unverified",
                    }
                ]
            },
            {"findings": []},
            {
                "findings": [
                    {
                        "id": "R1",
                        "severity": "high",
                        "origin": "introduced",
                        "confidence": 0.92,
                        "file": "app/x.py",
                        "line_start": 10,
                        "line_end": 12,
                        "claim": "Regression in approval gate",
                        "evidence": ["diff removes guard"],
                        "verifier_status": "confirmed",
                    },
                    {
                        "id": "R2",
                        "severity": "medium",
                        "origin": "test_reviewer",
                        "confidence": 0.5,
                        "file": "tests/test_x.py",
                        "line_start": 1,
                        "line_end": 1,
                        "claim": "Missing regression test",
                        "evidence": ["no test coverage"],
                        "verifier_status": "confirmed",
                    },
                ]
            },
            {
                "findings": [
                    {
                        "id": "R1",
                        "severity": "high",
                        "origin": "introduced",
                        "confidence": 0.95,
                        "file": "app/x.py",
                        "line_start": 10,
                        "line_end": 12,
                        "claim": "Regression in approval gate",
                        "evidence": ["diff removes guard"],
                        "verifier_status": "confirmed",
                    },
                    {
                        "id": "R2",
                        "severity": "medium",
                        "origin": "test_reviewer",
                        "confidence": 0.5,
                        "file": "tests/test_x.py",
                        "line_start": 1,
                        "line_end": 1,
                        "claim": "Missing regression test",
                        "evidence": ["no test coverage"],
                        "verifier_status": "confirmed",
                    },
                ]
            },
        ]

        with patch.object(runner.client, "json_response", side_effect=payloads):
            review = runner.review(
                workspace=".",
                git_diff="diff --git a/app/x.py b/app/x.py",
                changed_files={"changed_files": ["app/x.py"]},
                verification_summary={"status": "success"},
                plan={"goal": "fix"},
                test_plan={"cases": []},
            )

        self.assertEqual("reject", review["decision"])
        self.assertEqual(2, len(review["findings"]))
        self.assertEqual(1, len(review["postable_findings"]))
        self.assertIn("Missing regression test", review["test_gaps"])
        self.assertIn("Regression in approval gate", review["risk_items"])

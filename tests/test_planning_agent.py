from __future__ import annotations

import unittest

from app.planning_agent import _merge_test_plan_chunks, _renumber_test_cases


class PlanningAgentHelpersTests(unittest.TestCase):
    def test_renumber_test_cases_groups_by_target(self) -> None:
        cases = [
            {"id": "x", "target": "split_sections", "name": "a"},
            {"id": "y", "target": "split_sections", "name": "b"},
            {"id": "z", "target": "generate_image", "name": "c"},
        ]

        renumbered = _renumber_test_cases(cases)

        self.assertEqual("TS-01-TC-01", renumbered[0]["id"])
        self.assertEqual("TS-01-TC-02", renumbered[1]["id"])
        self.assertEqual("TS-02-TC-01", renumbered[2]["id"])

    def test_merge_test_plan_chunks_dedupes_and_merges(self) -> None:
        overview = {
            "test_targets": ["split_sections", "generate_image", "split_sections"],
            "strategy": {
                "unit": ["parser", "parser"],
                "integration": ["api"],
                "e2e": [],
                "mocking": ["openai", "gemini"],
            },
        }
        chunks = [
            {
                "cases": [
                    {"id": "old1", "target": "split_sections", "name": "case1"},
                    {"id": "old2", "target": "generate_image", "name": "case2"},
                ],
                "regression_risks": ["risk-a", "risk-b"],
                "risks": [
                    {
                        "title": "Gemini model mismatch",
                        "severity": "high",
                        "likelihood": "medium",
                        "impact": "broken generation",
                        "mitigation": "pin model",
                        "detection": "integration test",
                    }
                ],
            },
            {
                "cases": [
                    {"id": "old3", "target": "split_sections", "name": "case3"},
                ],
                "regression_risks": ["risk-b", "risk-c"],
                "risks": [
                    {
                        "title": "Gemini model mismatch",
                        "severity": "high",
                        "likelihood": "medium",
                        "impact": "broken generation",
                        "mitigation": "pin model",
                        "detection": "integration test",
                    },
                    {
                        "title": "Timeout",
                        "severity": "medium",
                        "likelihood": "medium",
                        "impact": "slow requests",
                        "mitigation": "extend timeout",
                        "detection": "timeout monitoring",
                    },
                ],
            },
        ]

        merged = _merge_test_plan_chunks(overview, chunks)

        self.assertEqual(["split_sections", "generate_image"], merged["test_targets"])
        self.assertEqual(["parser"], merged["strategy"]["unit"])
        self.assertEqual(["risk-a", "risk-b", "risk-c"], merged["regression_risks"])
        self.assertEqual(2, len(merged["risks"]))
        self.assertEqual("TS-01-TC-01", merged["cases"][0]["id"])
        self.assertEqual("TS-02-TC-01", merged["cases"][1]["id"])
        self.assertEqual("TS-01-TC-02", merged["cases"][2]["id"])

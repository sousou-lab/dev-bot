from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from app.contracts.artifact_models import ConstraintReportV1, DesignBranch, PlanTask, PlanV2, RepoExplorerV1, RiskItem
from app.contracts.artifact_models import TestMappingItem as CriterionTestMappingItem
from app.planning_agent import PlanningAgent, _merge_test_plan_chunks, _renumber_test_cases
from tests.helpers import make_test_settings


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


class PlanningAgentCommitteeTests(unittest.TestCase):
    def test_build_artifacts_uses_committee_mode_when_workflow_requests_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir, "WORKFLOW.md").write_text(
                (
                    "---\n"
                    "planning:\n"
                    "  provider: claude-agent-sdk\n"
                    "  mode: committee\n"
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
                    "---\n"
                ),
                encoding="utf-8",
            )
            agent = PlanningAgent(make_test_settings())
            merged_plan = PlanV2(
                goal="Ship it",
                acceptance_criteria=["ac1"],
                out_of_scope=[],
                candidate_files=["app/x.py"],
                tasks=[PlanTask(id="T1", summary="Implement x", files=["app/x.py"], done_when="done")],
                design_branches=[DesignBranch(id="primary", summary="main")],
                risks=[RiskItem(risk="regression", mitigation="tests")],
                test_mapping=[CriterionTestMappingItem(criterion="ac1", tests=["tests/test_x.py"])],
                verification_profile="python-basic",
            )
            bundle = type(
                "Bundle",
                (),
                {
                    "repo": RepoExplorerV1(candidate_files=["app/x.py"]),
                    "risk": type(
                        "RiskOut",
                        (),
                        {
                            "risks": [RiskItem(risk="regression", mitigation="tests")],
                            "test_mapping": [CriterionTestMappingItem(criterion="ac1", tests=["tests/test_x.py"])],
                        },
                    )(),
                    "constraint": ConstraintReportV1(out_of_scope=[]),
                    "merged": merged_plan,
                },
            )()

            with patch.object(agent, "_create_planner_committee") as create_committee:
                create_committee.return_value.build_plan.side_effect = lambda issue_ctx: bundle
                with patch("app.planning_agent._run_async", side_effect=lambda value: value):
                    artifacts = agent.build_artifacts(
                        workspace=tmpdir,
                        summary={"goal": "Ship it", "acceptance_criteria": ["ac1"]},
                        repo_profile={"files": ["app/x.py"]},
                    )

            self.assertEqual("Ship it", artifacts.plan["goal"])
            self.assertEqual(["tests/test_x.py"], artifacts.test_plan["test_targets"])
            self.assertEqual("Ship it", artifacts.committee_plan["goal"])
            self.assertEqual({"enabled": False, "candidate_ids": ["primary"]}, artifacts.candidate_decision)

    def test_build_artifacts_enables_candidate_mode_for_rework_committee_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir, "WORKFLOW.md").write_text(
                (
                    "---\n"
                    "planning:\n"
                    "  provider: claude-agent-sdk\n"
                    "  mode: committee\n"
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
                    "---\n"
                ),
                encoding="utf-8",
            )
            agent = PlanningAgent(make_test_settings())
            merged_plan = PlanV2(
                goal="Ship it",
                acceptance_criteria=["ac1"],
                out_of_scope=[],
                candidate_files=["app/x.py"],
                tasks=[PlanTask(id="T1", summary="Implement x", files=["app/x.py"], done_when="done")],
                design_branches=[
                    DesignBranch(id="primary", summary="main"),
                    DesignBranch(id="alt1", summary="alternative"),
                ],
                risks=[RiskItem(risk="regression", mitigation="tests")],
                test_mapping=[CriterionTestMappingItem(criterion="ac1", tests=["tests/test_x.py"])],
                verification_profile="python-basic",
                planner_confidence=0.95,
            )
            bundle = type(
                "Bundle",
                (),
                {
                    "repo": RepoExplorerV1(candidate_files=["app/x.py"]),
                    "risk": type(
                        "RiskOut",
                        (),
                        {
                            "risks": [RiskItem(risk="regression", mitigation="tests")],
                            "test_mapping": [CriterionTestMappingItem(criterion="ac1", tests=["tests/test_x.py"])],
                        },
                    )(),
                    "constraint": ConstraintReportV1(out_of_scope=[]),
                    "merged": merged_plan,
                },
            )()

            with patch.object(agent, "_create_planner_committee") as create_committee:
                create_committee.return_value.build_plan.side_effect = lambda issue_ctx: bundle
                with patch("app.planning_agent._run_async", side_effect=lambda value: value):
                    artifacts = agent.build_artifacts(
                        workspace=tmpdir,
                        summary={"goal": "Ship it", "acceptance_criteria": ["ac1"]},
                        repo_profile={"files": ["app/x.py"]},
                        rework_count=1,
                    )

            self.assertEqual({"enabled": True, "candidate_ids": ["primary", "alt1"]}, artifacts.candidate_decision)

    def test_create_planner_committee_applies_role_tool_policy(self) -> None:
        agent = PlanningAgent(make_test_settings())

        committee = agent._create_planner_committee(
            planning_config=type(
                "PlanningCfg",
                (),
                {
                    "allowed_tools": ["Read", "Grep", "Glob", "Write"],
                    "settings_sources": ["project"],
                    "committee": type(
                        "CommitteeCfg",
                        (),
                        {
                            "roles": {
                                "repo_explorer": type(
                                    "RoleCfg",
                                    (),
                                    {
                                        "allowed_tools": ["Read", "Grep", "Glob", "Write"],
                                        "disallowed_tools": ["Write", "Glob"],
                                    },
                                )(),
                            }
                        },
                    )(),
                },
            )(),
        )

        self.assertEqual(["Read", "Grep"], committee.repo_explorer.allowed_tools)
        self.assertEqual(["project"], committee.repo_explorer.setting_sources)

    def test_build_artifacts_ignores_invalid_workflow_config_and_uses_legacy_planner(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            agent = PlanningAgent(make_test_settings())
            plan_client = Mock()
            test_plan_client = Mock()
            plan_client.json_response.return_value = {
                "goal": "Ship it",
                "scope": ["Implement x"],
                "assumptions": [],
                "candidate_files": ["app/x.py"],
                "implementation_steps": ["Implement x"],
                "verification_steps": ["Run tests"],
                "risks": [],
                "high_risk_changes": [],
            }
            test_plan_client.json_response.return_value = {
                "test_targets": ["tests/test_x.py"],
                "strategy": {"unit": [], "integration": [], "e2e": [], "mocking": []},
                "cases": [],
                "regression_risks": [],
                "risks": [],
            }
            test_plan_client.json_response_with_meta.side_effect = [
                type(
                    "Result",
                    (),
                    {
                        "payload": {
                            "test_targets": ["tests/test_x.py"],
                            "strategy": {"unit": [], "integration": [], "e2e": [], "mocking": []},
                        },
                        "session_id": "overview",
                    },
                )(),
                type(
                    "Result",
                    (),
                    {
                        "payload": {"cases": [], "regression_risks": [], "risks": []},
                        "session_id": "chunk-1",
                    },
                )(),
            ]

            with (
                patch("app.planning_agent.load_workflow_definition", return_value=type("Def", (), {"config": None})()),
                patch("app.planning_agent.ClaudeAgentClient", side_effect=[plan_client, test_plan_client]),
            ):
                artifacts = agent.build_artifacts(
                    workspace=tmpdir,
                    summary={"goal": "Ship it", "acceptance_criteria": ["ac1"]},
                    repo_profile={"files": ["app/x.py"]},
                )

            self.assertEqual("Ship it", artifacts.plan["goal"])
            self.assertEqual(["tests/test_x.py"], artifacts.test_plan["test_targets"])

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
            self.assertEqual(2, artifacts.committee_plan["version"])
            self.assertEqual([], artifacts.committee_plan["must_not_touch"])
            self.assertEqual([], artifacts.committee_plan["verification_focus"])
            self.assertFalse(artifacts.committee_plan["exploration_required"])
            self.assertEqual("Ship it", artifacts.plan_v2["goal"])
            self.assertEqual("committee", artifacts.committee_bundle["mode"])
            self.assertEqual("Ship it", artifacts.committee_bundle["plan_v2"]["goal"])
            self.assertEqual({"enabled": False, "candidate_ids": ["primary"]}, artifacts.candidate_decision)

    def test_build_artifacts_autoselects_committee_for_large_complex_summary_without_workflow(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            agent = PlanningAgent(make_test_settings())
            merged_plan = PlanV2(
                goal="Ship it",
                acceptance_criteria=[f"ac{i}" for i in range(1, 10)],
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
                        summary={
                            "goal": "Ship it",
                            "complexity": "complex",
                            "acceptance_criteria": [f"ac{i}" for i in range(1, 10)],
                            "background": "x" * 3000,
                        },
                        repo_profile={"files": ["app/x.py"]},
                    )

            self.assertEqual("committee", artifacts.committee_bundle["mode"])
            create_committee.assert_called_once()

    def test_build_artifacts_keeps_explicit_legacy_mode_for_large_complex_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir, "WORKFLOW.md").write_text(
                (
                    "---\n"
                    "planning:\n"
                    "  provider: claude-agent-sdk\n"
                    "  mode: legacy\n"
                    "implementation:\n"
                    "  backend: codex-app-server\n"
                    "review:\n"
                    "  provider: claude-agent-sdk\n"
                    "---\n"
                ),
                encoding="utf-8",
            )
            agent = PlanningAgent(make_test_settings())
            plan_client = Mock()
            test_plan_client = Mock()
            plan_client.json_response.return_value = {
                "version": 2,
                "goal": "Ship it",
                "scope": ["Do it"],
                "assumptions": [],
                "candidate_files": ["app/x.py"],
                "must_not_touch": ["WORKFLOW.md"],
                "verification_focus": ["keep tests green"],
                "exploration_required": False,
                "implementation_steps": ["Do it"],
                "verification_steps": ["Run tests"],
                "risks": [],
                "high_risk_changes": [],
            }
            test_plan_client.json_response_with_meta.side_effect = [
                Mock(
                    payload={
                        "test_targets": ["tests/test_x.py"],
                        "strategy": {"unit": [], "integration": [], "e2e": [], "mocking": []},
                    },
                    session_id="overview",
                ),
                *[
                    Mock(payload={"cases": [], "regression_risks": [], "risks": []}, session_id=f"chunk-{index}")
                    for index in range(1, 10)
                ],
            ]

            with (
                patch("app.planning_agent.ClaudeAgentClient", side_effect=[plan_client, test_plan_client]),
                patch.object(agent, "_create_planner_committee") as create_committee,
            ):
                artifacts = agent.build_artifacts(
                    workspace=tmpdir,
                    summary={
                        "goal": "Ship it",
                        "complexity": "complex",
                        "acceptance_criteria": [f"ac{i}" for i in range(1, 10)],
                        "background": "x" * 3000,
                    },
                    repo_profile={"files": ["app/x.py"]},
                )

            self.assertEqual("legacy", artifacts.committee_bundle["mode"])
            create_committee.assert_not_called()

    def test_build_artifacts_uses_auto_mode_thresholds_from_workflow(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir, "WORKFLOW.md").write_text(
                (
                    "---\n"
                    "planning:\n"
                    "  provider: claude-agent-sdk\n"
                    "  mode: auto\n"
                    "  autoselect_committee:\n"
                    "    min_acceptance_criteria: 20\n"
                    "    min_acceptance_criteria_when_complex: 20\n"
                    "    min_summary_chars_when_complex: 10000\n"
                    "    min_repo_files: 500\n"
                    "    min_acceptance_criteria_with_large_repo: 20\n"
                    "implementation:\n"
                    "  backend: codex-app-server\n"
                    "review:\n"
                    "  provider: claude-agent-sdk\n"
                    "---\n"
                ),
                encoding="utf-8",
            )
            agent = PlanningAgent(make_test_settings())
            plan_client = Mock()
            test_plan_client = Mock()
            plan_client.json_response.return_value = {
                "version": 2,
                "goal": "Ship it",
                "scope": ["Do it"],
                "assumptions": [],
                "candidate_files": ["app/x.py"],
                "must_not_touch": ["WORKFLOW.md"],
                "verification_focus": ["keep tests green"],
                "exploration_required": False,
                "implementation_steps": ["Do it"],
                "verification_steps": ["Run tests"],
                "risks": [],
                "high_risk_changes": [],
            }
            test_plan_client.json_response_with_meta.side_effect = [
                Mock(
                    payload={
                        "test_targets": ["tests/test_x.py"],
                        "strategy": {"unit": [], "integration": [], "e2e": [], "mocking": []},
                    },
                    session_id="overview",
                ),
                *[
                    Mock(payload={"cases": [], "regression_risks": [], "risks": []}, session_id=f"chunk-{index}")
                    for index in range(1, 10)
                ],
            ]

            with (
                patch("app.planning_agent.ClaudeAgentClient", side_effect=[plan_client, test_plan_client]),
                patch.object(agent, "_create_planner_committee") as create_committee,
            ):
                artifacts = agent.build_artifacts(
                    workspace=tmpdir,
                    summary={
                        "goal": "Ship it",
                        "complexity": "complex",
                        "acceptance_criteria": [f"ac{i}" for i in range(1, 10)],
                        "background": "x" * 3000,
                    },
                    repo_profile={"files": ["app/x.py"]},
                )

            self.assertEqual("legacy", artifacts.committee_bundle["mode"])
            create_committee.assert_not_called()

    def test_build_artifacts_auto_mode_can_force_committee_with_lower_thresholds(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir, "WORKFLOW.md").write_text(
                (
                    "---\n"
                    "planning:\n"
                    "  provider: claude-agent-sdk\n"
                    "  mode: auto\n"
                    "  autoselect_committee:\n"
                    "    min_acceptance_criteria: 5\n"
                    "    min_acceptance_criteria_when_complex: 4\n"
                    "    min_summary_chars_when_complex: 500\n"
                    "    min_repo_files: 10\n"
                    "    min_acceptance_criteria_with_large_repo: 3\n"
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
                acceptance_criteria=[f"ac{i}" for i in range(1, 6)],
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
                        summary={
                            "goal": "Ship it",
                            "complexity": "complex",
                            "acceptance_criteria": [f"ac{i}" for i in range(1, 6)],
                            "background": "x" * 1000,
                        },
                        repo_profile={"files": ["app/x.py"]},
                    )

            self.assertEqual("committee", artifacts.committee_bundle["mode"])
            create_committee.assert_called_once()

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

    def test_build_artifacts_respects_candidate_mode_trigger_thresholds(self) -> None:
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
                    "  candidate_mode:\n"
                    "    triggers:\n"
                    "      rework_count_gte: 2\n"
                    "      planner_confidence_lt: 0.5\n"
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
                planner_confidence=0.70,
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

            self.assertEqual({"enabled": False, "candidate_ids": ["primary"]}, artifacts.candidate_decision)

    def test_build_artifacts_respects_require_clear_design_branches_trigger(self) -> None:
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
                    "  candidate_mode:\n"
                    "    triggers:\n"
                    "      require_clear_design_branches: true\n"
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
                planner_confidence=0.70,
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

            self.assertEqual({"enabled": False, "candidate_ids": ["primary"]}, artifacts.candidate_decision)

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

    def test_legacy_path_uses_project_setting_sources(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir, "WORKFLOW.md").write_text(
                (
                    "---\n"
                    "planning:\n"
                    "  provider: claude-agent-sdk\n"
                    "  mode: legacy\n"
                    "  settings_sources: [project]\n"
                    "implementation:\n"
                    "  backend: codex-app-server\n"
                    "review:\n"
                    "  provider: claude-agent-sdk\n"
                    "---\n"
                ),
                encoding="utf-8",
            )
            agent = PlanningAgent(make_test_settings())
            calls: list[list[str]] = []

            class _FakeClient:
                def __init__(self, *args, **kwargs) -> None:
                    pass

                def json_response(self, *args, **kwargs):
                    calls.append(list(kwargs["setting_sources"]))
                    return {
                        "version": 2,
                        "goal": "Ship it",
                        "scope": ["Do it"],
                        "assumptions": [],
                        "candidate_files": ["app/x.py"],
                        "must_not_touch": ["WORKFLOW.md"],
                        "verification_focus": ["keep tests green"],
                        "exploration_required": False,
                        "implementation_steps": ["Do it"],
                        "verification_steps": ["Run tests"],
                        "risks": [],
                        "high_risk_changes": [],
                    }

                def json_response_with_meta(self, *args, **kwargs):
                    calls.append(list(kwargs["setting_sources"]))
                    schema = kwargs["output_schema"]
                    if "cases" in schema["required"]:
                        payload = {
                            "cases": [
                                {
                                    "id": "old1",
                                    "target": "tests/test_x.py",
                                    "name": "ac1",
                                    "category": "regression",
                                    "priority": "p1",
                                    "acceptance_criteria_refs": ["ac1"],
                                    "preconditions": [],
                                    "inputs": [],
                                    "steps": ["run"],
                                    "expected": ["pass"],
                                    "failure_mode": "ac1",
                                    "notes": [],
                                }
                            ],
                            "regression_risks": [],
                            "risks": [],
                        }
                    else:
                        payload = {
                            "test_targets": ["tests/test_x.py"],
                            "strategy": {"unit": [], "integration": [], "e2e": [], "mocking": []},
                        }
                    return Mock(payload=payload, session_id="sess_1")

            with patch("app.planning_agent.ClaudeAgentClient", _FakeClient):
                artifacts = agent.build_artifacts(
                    workspace=tmpdir,
                    summary={"goal": "Ship it", "acceptance_criteria": ["ac1"]},
                    repo_profile={"files": ["app/x.py"]},
                )

            self.assertEqual("Ship it", artifacts.plan["goal"])
            self.assertEqual(["WORKFLOW.md"], artifacts.plan["must_not_touch"])
            self.assertEqual(["keep tests green"], artifacts.plan["verification_focus"])
            self.assertEqual("Ship it", artifacts.plan_v2["goal"])
            self.assertEqual(["ac1"], artifacts.plan_v2["acceptance_criteria"])
            self.assertEqual("legacy", artifacts.committee_bundle["mode"])
            self.assertTrue(calls)
            self.assertTrue(all(call == ["project"] for call in calls))

    def test_legacy_test_plan_reuses_overview_session_with_forked_chunks(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            agent = PlanningAgent(make_test_settings())
            plan_client = Mock()
            test_plan_client = Mock()
            plan_client.json_response.return_value = {
                "version": 2,
                "goal": "Ship it",
                "scope": ["Implement x"],
                "assumptions": [],
                "candidate_files": ["app/x.py"],
                "must_not_touch": ["WORKFLOW.md"],
                "verification_focus": ["keep tests green"],
                "exploration_required": False,
                "implementation_steps": ["Implement x"],
                "verification_steps": ["Run tests"],
                "risks": [],
                "high_risk_changes": [],
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
                        "session_id": "overview-session",
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

            with patch("app.planning_agent.ClaudeAgentClient", side_effect=[plan_client, test_plan_client]):
                artifacts = agent.build_artifacts(
                    workspace=tmpdir,
                    summary={"goal": "Ship it", "acceptance_criteria": ["ac1"]},
                    repo_profile={"files": [f"app/file_{index}.py" for index in range(100)]},
                )

            self.assertEqual(["tests/test_x.py"], artifacts.test_plan["test_targets"])
            self.assertEqual(2, test_plan_client.json_response_with_meta.call_count)
            overview_call = test_plan_client.json_response_with_meta.call_args_list[0]
            chunk_call = test_plan_client.json_response_with_meta.call_args_list[1]
            overview_kwargs = overview_call.kwargs
            chunk_kwargs = chunk_call.kwargs
            overview_prompt = overview_call.args[1]
            chunk_prompt = chunk_call.args[1]
            self.assertTrue(overview_kwargs["include_partial_messages"])
            self.assertIsNone(overview_kwargs.get("resume_session_id"))
            self.assertEqual("overview-session", chunk_kwargs["resume_session_id"])
            self.assertTrue(chunk_kwargs["continue_conversation"])
            self.assertTrue(chunk_kwargs["fork_session"])
            self.assertTrue(chunk_kwargs["include_partial_messages"])
            self.assertIn("test_plan_seed", overview_prompt)
            self.assertIn("overview:", chunk_prompt)
            self.assertNotIn('"app/file_99.py"', chunk_prompt)

    def test_committee_failure_falls_back_to_legacy_when_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir, "WORKFLOW.md").write_text(
                (
                    "---\n"
                    "planning:\n"
                    "  provider: claude-agent-sdk\n"
                    "  mode: committee\n"
                    "  legacy_fallback:\n"
                    "    enabled: true\n"
                    "    use_only_on_committee_failure: true\n"
                    "  settings_sources: [project]\n"
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
            plan_client = Mock()
            test_plan_client = Mock()
            plan_client.json_response.return_value = {
                "version": 2,
                "goal": "Legacy fallback",
                "scope": ["Implement x"],
                "assumptions": [],
                "candidate_files": ["app/x.py"],
                "must_not_touch": ["WORKFLOW.md"],
                "verification_focus": ["keep tests green"],
                "exploration_required": False,
                "implementation_steps": ["Implement x"],
                "verification_steps": ["Run tests"],
                "risks": [],
                "high_risk_changes": [],
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
                patch.object(agent, "_create_planner_committee") as create_committee,
                patch("app.planning_agent.ClaudeAgentClient", side_effect=[plan_client, test_plan_client]),
            ):
                create_committee.return_value.build_plan.side_effect = RuntimeError("committee failed")
                artifacts = agent.build_artifacts(
                    workspace=tmpdir,
                    summary={"goal": "Ship it", "acceptance_criteria": ["ac1"]},
                    repo_profile={"files": ["app/x.py"]},
                )

            self.assertEqual("Legacy fallback", artifacts.plan["goal"])
            self.assertEqual("legacy", artifacts.committee_bundle["mode"])

    def test_committee_failure_raises_when_legacy_fallback_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            Path(tmpdir, "WORKFLOW.md").write_text(
                (
                    "---\n"
                    "planning:\n"
                    "  provider: claude-agent-sdk\n"
                    "  mode: committee\n"
                    "  legacy_fallback:\n"
                    "    enabled: false\n"
                    "    use_only_on_committee_failure: true\n"
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

            with patch.object(agent, "_create_planner_committee") as create_committee:
                create_committee.return_value.build_plan.side_effect = RuntimeError("committee failed")
                with self.assertRaisesRegex(RuntimeError, "committee failed"):
                    agent.build_artifacts(
                        workspace=tmpdir,
                        summary={"goal": "Ship it", "acceptance_criteria": ["ac1"]},
                        repo_profile={"files": ["app/x.py"]},
                    )

    def test_build_artifacts_ignores_invalid_workflow_config_and_uses_legacy_planner(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            agent = PlanningAgent(make_test_settings())
            plan_client = Mock()
            test_plan_client = Mock()
            plan_client.json_response.return_value = {
                "version": 2,
                "goal": "Ship it",
                "scope": ["Implement x"],
                "assumptions": [],
                "candidate_files": ["app/x.py"],
                "must_not_touch": ["WORKFLOW.md"],
                "verification_focus": ["keep tests green"],
                "exploration_required": False,
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

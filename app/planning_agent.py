from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, TypedDict, cast

from app.agent_sdk_client import ClaudeAgentClient
from app.config import Settings
from app.contracts.artifact_models import PlanV2
from app.contracts.workflow_schema import (
    CandidateModeTriggers,
    CommitteeRoleConfig,
    PlanningAutoselectCommitteeConfig,
    PlanningConfig,
)
from app.implementation.candidate_policy import CandidateDecision, decide_candidates
from app.planning.committee import PlannerCommittee
from app.planning.roles.constraint_checker import ConstraintChecker
from app.planning.roles.plan_merger import PlanMerger
from app.planning.roles.repo_explorer import RepoExplorer
from app.planning.roles.risk_test_planner import RiskTestPlanner
from app.verification_profiles import build_verification_plan
from app.workflow_loader import load_workflow_definition

READ_ONLY_TOOLS = [
    "Read",
    "Grep",
    "Glob",
    "ToolSearch",
    "Agent",
    "Skill",
    "WebSearch",
    "TodoWrite",
    "AskUserQuestion",
]

PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "version": {"type": "integer"},
        "goal": {"type": "string"},
        "scope": {"type": "array", "items": {"type": "string"}},
        "assumptions": {"type": "array", "items": {"type": "string"}},
        "candidate_files": {"type": "array", "items": {"type": "string"}},
        "must_not_touch": {"type": "array", "items": {"type": "string"}},
        "verification_focus": {"type": "array", "items": {"type": "string"}},
        "exploration_required": {"type": "boolean"},
        "implementation_steps": {"type": "array", "items": {"type": "string"}},
        "verification_steps": {"type": "array", "items": {"type": "string"}},
        "risks": {"type": "array", "items": {"type": "string"}},
        "high_risk_changes": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "version",
        "goal",
        "scope",
        "assumptions",
        "candidate_files",
        "must_not_touch",
        "verification_focus",
        "exploration_required",
        "implementation_steps",
        "verification_steps",
        "risks",
        "high_risk_changes",
    ],
}

TEST_PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "test_targets": {"type": "array", "items": {"type": "string"}},
        "strategy": {
            "type": "object",
            "properties": {
                "unit": {"type": "array", "items": {"type": "string"}},
                "integration": {"type": "array", "items": {"type": "string"}},
                "e2e": {"type": "array", "items": {"type": "string"}},
                "mocking": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["unit", "integration", "e2e", "mocking"],
        },
        "cases": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "target": {"type": "string"},
                    "name": {"type": "string"},
                    "category": {
                        "type": "string",
                        "enum": [
                            "happy_path",
                            "boundary",
                            "error_handling",
                            "integration",
                            "regression",
                            "performance",
                        ],
                    },
                    "priority": {
                        "type": "string",
                        "enum": ["p0", "p1", "p2"],
                    },
                    "acceptance_criteria_refs": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "preconditions": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "inputs": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "steps": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "expected": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "failure_mode": {"type": "string"},
                    "notes": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "required": [
                    "id",
                    "target",
                    "name",
                    "category",
                    "priority",
                    "acceptance_criteria_refs",
                    "preconditions",
                    "inputs",
                    "steps",
                    "expected",
                    "failure_mode",
                    "notes",
                ],
            },
        },
        "regression_risks": {"type": "array", "items": {"type": "string"}},
        "risks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "severity": {
                        "type": "string",
                        "enum": ["low", "medium", "high"],
                    },
                    "likelihood": {
                        "type": "string",
                        "enum": ["low", "medium", "high"],
                    },
                    "impact": {"type": "string"},
                    "mitigation": {"type": "string"},
                    "detection": {"type": "string"},
                },
                "required": [
                    "title",
                    "severity",
                    "likelihood",
                    "impact",
                    "mitigation",
                    "detection",
                ],
            },
        },
    },
    "required": ["test_targets", "strategy", "cases", "regression_risks", "risks"],
}

TEST_PLAN_OVERVIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "test_targets": {"type": "array", "items": {"type": "string"}},
        "strategy": {
            "type": "object",
            "properties": {
                "unit": {"type": "array", "items": {"type": "string"}},
                "integration": {"type": "array", "items": {"type": "string"}},
                "e2e": {"type": "array", "items": {"type": "string"}},
                "mocking": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["unit", "integration", "e2e", "mocking"],
        },
    },
    "required": ["test_targets", "strategy"],
}

TEST_PLAN_AC_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "cases": TEST_PLAN_SCHEMA["properties"]["cases"],
        "regression_risks": {"type": "array", "items": {"type": "string"}},
        "risks": TEST_PLAN_SCHEMA["properties"]["risks"],
    },
    "required": ["cases", "regression_risks", "risks"],
}

PLAN_SYSTEM_PROMPT = """あなたはソフトウェア開発の planning エージェントです。
与えられた要件と既存リポジトリを調査し、実装前レビュー用の plan.json を作成してください。

必須ルール:
- 使ってよいのは非破壊ツールのみ。Read / Grep / Glob / ToolSearch / Agent / Skill / WebSearch / TodoWrite / AskUserQuestion を使ってよい
- ファイル編集、コマンド実行、外部アクセスは禁止
- Write / Edit / Bash は絶対に使わない
- plan.json というファイルを保存しない。schema に一致する JSON オブジェクトを最終出力として返すだけ
- 実装案は既存コードに沿って最小変更で設計する
- 未確定事項は assumptions または risks に残す
- 変更対象ファイルは candidate_files として挙げる
- migration や secrets 変更の可能性は risks に明記する
- 変更禁止パスや契約ファイルは must_not_touch に列挙する
- verification_focus には回帰させたくない観点を列挙する
- 調査を始める宣言文や途中説明を書かない
- 思考過程を書かない
- ツールを使った後でも最終出力は schema に一致する JSON オブジェクトのみ
- JSON 以外は返さない

requirements summary には、通常の要件情報に加えて次の判断材料が含まれる場合がある:
- preferred_outcomes
- tradeoffs
- disallowed_approaches
- assumptions_to_validate
- recommended_direction
- preferences
- solution_options

これらの扱い方:
- これらは planning の入力として尊重してよい
- ただし、repo の実態調査より優先してはいけない
- recommended_direction は高レベルな方向づけであり、実装方針の確定ではない
- solution_options は比較の参考であり、そのまま採用してはいけない
- assumptions_to_validate は assumptions や risks に引き継いでよい
- disallowed_approaches は強い制約候補として扱い、無視する場合は理由を risks に残す
- preferences はユーザーの選好を表すが、技術的妥当性の代わりにはならない
- requirements summary に含まれる方向づけ情報は、設計の入力であって結論ではない。常に repository の実態確認を優先せよ。
"""

TEST_PLAN_SYSTEM_PROMPT = """あなたはソフトウェア開発の test planning エージェントです。
与えられた plan.json と既存リポジトリ情報をもとに、実装前レビュー用の test_plan.json を作成してください。

必須ルール:
- 使ってよいのは非破壊ツールのみ。Read / Grep / Glob / ToolSearch / Agent / Skill / WebSearch / TodoWrite / AskUserQuestion を使ってよい
- ファイル編集、コマンド実行、外部アクセスは禁止
- Write / Edit / Bash は絶対に使わない
- test_plan.json というファイルを保存しない。schema に一致する JSON オブジェクトを最終出力として返すだけ
- acceptance criteria ごとに最低 3 件以上のテストケースを作る
- 各 acceptance criteria について、少なくとも happy_path / boundary / error_handling を含める
- acceptance criteria と 1 対 1 以上で結びつくテスト観点を作る
- DB や migration が関わる場合は db_strategy に明記する
- 外部 API 連携がある場合は success / invalid input / malformed response / timeout / rate limit / auth error を可能な限り分解する
- JSON schema や structured output が関係する場合は parse failure と空レスポンスも必ず考慮する
- 非同期処理がある場合は timeout / cancellation / concurrency を考慮する
- setup/test/lint コマンドは repo_profile に合わせて現実的に書く
- 既存テストの流儀に合わせる
- 境界条件と回帰観点を最低限含める
- 調査を始める宣言文や途中説明を書かない
- 思考過程を書かない
- ツールを使った後でも最終出力は schema に一致する JSON オブジェクトのみ
- JSON 以外は返さない

ケース生成ルール:
- id は `TS-<target番号>-TC-<連番>` 形式にする
- target は対象機能や関数名を具体的に書く
- preconditions / inputs / steps / expected は省略しない
- expected は観測可能な結果にする
- failure_mode には「何が壊れたときのケースか」を短く書く
- priority は business impact と regression risk を踏まえて付与する
- risks は抽象論ではなく、実装時に起こる具体的失敗として書く
- risks の mitigation は「どう防ぐか」、detection は「どう検知するか」を書く
"""


@dataclass(frozen=True)
class PlanningArtifacts:
    repo_profile: dict[str, Any]
    plan: dict[str, Any]
    test_plan: dict[str, Any]
    verification_plan: dict[str, Any]
    plan_v2: dict[str, Any] | None = None
    candidate_decision: dict[str, Any] | None = None
    committee_plan: dict[str, Any] | None = None
    committee_reports: dict[str, Any] | None = None
    committee_bundle: dict[str, Any] | None = None


class CandidateModeDecisionKwargs(TypedDict):
    rework_count_gte: int
    planner_confidence_lt: float
    require_clear_design_branches: bool


class PlanningAgent:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def build_artifacts(
        self,
        *,
        workspace: str,
        summary: dict[str, Any],
        repo_profile: dict[str, Any],
        rework_count: int = 0,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
        debug_recorder: Callable[[dict[str, Any]], None] | None = None,
    ) -> PlanningArtifacts:
        if not self._has_plannable_summary(summary):
            raise ValueError(
                "requirement_summary.json に planning に必要な goal / in_scope / acceptance_criteria が不足しています。"
            )
        workflow_definition = load_workflow_definition(workspace=workspace, repo_root=".")
        planning_config = (
            workflow_definition.config.planning if workflow_definition and workflow_definition.config else None
        )
        candidate_mode_triggers = self._candidate_mode_triggers(workflow_definition)
        planning_mode = self._select_planning_mode(
            planning_config=planning_config,
            summary=summary,
            repo_profile=repo_profile,
        )
        if planning_mode == "committee":
            try:
                return self._build_committee_artifacts(
                    workspace=workspace,
                    summary=summary,
                    repo_profile=repo_profile,
                    rework_count=rework_count,
                    planning_config=planning_config,
                    candidate_mode_triggers=candidate_mode_triggers,
                )
            except Exception:
                if not self._allow_legacy_fallback(planning_config):
                    raise
        return self._build_legacy_artifacts(
            workspace=workspace,
            summary=summary,
            repo_profile=repo_profile,
            rework_count=rework_count,
            planning_config=planning_config,
            candidate_mode_triggers=candidate_mode_triggers,
            progress_callback=progress_callback,
            debug_recorder=debug_recorder,
        )

    def _build_legacy_artifacts(
        self,
        *,
        workspace: str,
        summary: dict[str, Any],
        repo_profile: dict[str, Any],
        rework_count: int,
        planning_config: PlanningConfig | None,
        candidate_mode_triggers: CandidateModeTriggers | None,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
        debug_recorder: Callable[[dict[str, Any]], None] | None = None,
    ) -> PlanningArtifacts:
        client = ClaudeAgentClient(
            api_key=self.settings.anthropic_api_key,
            timeout_seconds=float(300),
            max_buffer_size=self.settings.claude_agent_max_buffer_size,
        )
        test_plan_client = ClaudeAgentClient(
            api_key=self.settings.anthropic_api_key,
            timeout_seconds=float(600),
            max_buffer_size=self.settings.claude_agent_max_buffer_size,
        )
        repo_context = _build_repo_context(workspace, repo_profile)
        common_prompt = (
            "以下の要件サマリーとリポジトリ情報を見て判断してください。\n\n"
            f"requirement_summary:\n{json.dumps(summary, ensure_ascii=False, indent=2)}\n\n"
            f"repo_profile:\n{json.dumps(repo_profile, ensure_ascii=False, indent=2)}\n\n"
            f"repo_context:\n{repo_context}\n"
        )
        plan = client.json_response(
            PLAN_SYSTEM_PROMPT,
            (f"{common_prompt}\n既存コードを読んで plan.json を作成してください。"),
            cwd=workspace,
            max_turns=4,
            allowed_tools=READ_ONLY_TOOLS,
            permission_mode="default",
            setting_sources=self._planning_setting_sources(planning_config),
            output_schema=PLAN_SCHEMA,
            prompt_kind="plan",
            debug_recorder=debug_recorder,
            debug_context={"phase": "plan"},
        )
        test_plan = self._build_test_plan(
            client=test_plan_client,
            workspace=workspace,
            summary=summary,
            repo_profile=repo_profile,
            repo_context=_build_test_plan_repo_context(workspace, repo_profile),
            plan=plan,
            planning_config=planning_config,
            progress_callback=progress_callback,
            debug_recorder=debug_recorder,
        )
        verification_plan = build_verification_plan(workspace=workspace, repo_profile=repo_profile, plan=plan)
        plan_v2 = self._legacy_plan_to_plan_v2(
            summary=summary,
            plan=plan,
            test_plan=test_plan,
            verification_plan=verification_plan,
        )
        candidate_decision = self._candidate_decision_from_plan_v2_json(
            plan_v2=plan_v2,
            rework_count=rework_count,
            candidate_mode_triggers=candidate_mode_triggers,
        )
        return PlanningArtifacts(
            repo_profile=repo_profile,
            plan=plan,
            test_plan=test_plan,
            verification_plan=verification_plan,
            plan_v2=plan_v2,
            candidate_decision=_candidate_decision_to_json(candidate_decision),
            committee_bundle={
                "version": 1,
                "mode": "legacy",
                "plan_v2": plan_v2,
                "committee_reports": {},
            },
        )

    def _build_committee_artifacts(
        self,
        *,
        workspace: str,
        summary: dict[str, Any],
        repo_profile: dict[str, Any],
        rework_count: int,
        planning_config: PlanningConfig | None,
        candidate_mode_triggers: CandidateModeTriggers | None,
    ) -> PlanningArtifacts:
        committee = self._create_planner_committee(planning_config=planning_config)
        issue_ctx = _CommitteeIssueContext(
            issue_key="planning/local#0",
            repo_root=workspace,
            workpad_text=json.dumps(summary, ensure_ascii=False, indent=2),
            issue_body=self._build_committee_issue_body(summary),
            acceptance_hints=[str(item) for item in summary.get("acceptance_criteria", []) if str(item).strip()],
            extra_docs=self._build_committee_extra_docs(repo_profile),
        )
        bundle_coro = committee.build_plan(issue_ctx)
        try:
            bundle = _run_async(bundle_coro)
        except Exception:
            bundle_coro.close()
            raise
        plan = self._committee_plan_to_legacy(bundle.merged)
        test_plan = self._committee_test_plan_to_legacy(bundle.merged)
        verification_plan = build_verification_plan(workspace=workspace, repo_profile=repo_profile, plan=plan)
        candidate_decision = decide_candidates(
            bundle.merged,
            rework_count,
            **self._candidate_mode_trigger_kwargs(candidate_mode_triggers),
        )
        committee_plan = _plan_v2_to_json(bundle.merged)
        committee_reports = {
            "repo": _to_jsonable(bundle.repo),
            "risk": _to_jsonable(bundle.risk),
            "constraint": _to_jsonable(bundle.constraint),
        }
        return PlanningArtifacts(
            repo_profile=repo_profile,
            plan=plan,
            test_plan=test_plan,
            verification_plan=verification_plan,
            plan_v2=committee_plan,
            candidate_decision=_candidate_decision_to_json(candidate_decision),
            committee_plan=committee_plan,
            committee_reports=committee_reports,
            committee_bundle={
                "version": 1,
                "mode": "committee",
                "plan_v2": committee_plan,
                "committee_reports": committee_reports,
            },
        )

    def _create_planner_committee(self, *, planning_config: PlanningConfig | None = None) -> PlannerCommittee:
        client = ClaudeAgentClient(
            api_key=self.settings.anthropic_api_key,
            timeout_seconds=float(300),
            max_buffer_size=self.settings.claude_agent_max_buffer_size,
        )
        role_configs = planning_config.committee.roles if planning_config and planning_config.committee else {}
        return PlannerCommittee(
            repo_explorer=RepoExplorer(
                client=client,
                **self._planner_role_kwargs(planning_config, role_configs.get("repo_explorer")),
            ),
            risk_test_planner=RiskTestPlanner(
                client=client,
                **self._planner_role_kwargs(planning_config, role_configs.get("risk_test_planner")),
            ),
            constraint_checker=ConstraintChecker(
                client=client,
                **self._planner_role_kwargs(planning_config, role_configs.get("constraint_checker")),
            ),
            merger=PlanMerger(
                client=client,
                **self._planner_role_kwargs(planning_config, role_configs.get("merger")),
            ),
        )

    def _build_committee_issue_body(self, summary: dict[str, Any]) -> str:
        lines = [
            f"Goal: {summary.get('goal', '')}",
            "",
            "In scope:",
            *[f"- {item}" for item in summary.get("in_scope", []) if str(item).strip()],
            "",
            "Acceptance criteria:",
            *[f"- {item}" for item in summary.get("acceptance_criteria", []) if str(item).strip()],
            "",
            "Constraints:",
            *[f"- {item}" for item in summary.get("constraints", []) if str(item).strip()],
        ]
        return "\n".join(lines).strip()

    def _build_committee_extra_docs(self, repo_profile: dict[str, Any]) -> list[str]:
        docs: list[str] = []
        profiler_notes = repo_profile.get("notes", [])
        if isinstance(profiler_notes, list):
            docs.extend(str(item) for item in profiler_notes if str(item).strip())
        files = repo_profile.get("files", [])
        if isinstance(files, list):
            docs.extend(str(item) for item in files[:5] if str(item).strip())
        return docs

    def _planner_role_kwargs(
        self,
        planning_config: PlanningConfig | None,
        role_config: CommitteeRoleConfig | None,
    ) -> dict[str, Any]:
        allowed_tools = (
            list(planning_config.allowed_tools)
            if planning_config and planning_config.allowed_tools
            else [
                "Read",
                "Grep",
                "Glob",
            ]
        )
        if role_config and role_config.allowed_tools:
            allowed_tools = list(role_config.allowed_tools)
        if role_config and role_config.disallowed_tools:
            blocked = set(role_config.disallowed_tools)
            allowed_tools = [tool for tool in allowed_tools if tool not in blocked]
        setting_sources = self._planning_setting_sources(planning_config)
        return {
            "allowed_tools": allowed_tools,
            "setting_sources": setting_sources,
            "permission_mode": "default",
        }

    def _planning_setting_sources(self, planning_config: PlanningConfig | None) -> list[str]:
        if planning_config and planning_config.settings_sources:
            return list(planning_config.settings_sources)
        return ["project"]

    def _select_planning_mode(
        self,
        *,
        planning_config: PlanningConfig | None,
        summary: dict[str, Any],
        repo_profile: dict[str, Any],
    ) -> str:
        if planning_config is not None:
            mode = str(planning_config.mode or "committee").strip() or "committee"
            if mode != "auto":
                return mode
            if self._should_autoselect_committee(
                summary=summary,
                repo_profile=repo_profile,
                autoselect=planning_config.autoselect_committee,
            ):
                return "committee"
            return "legacy"
        if self._should_autoselect_committee(
            summary=summary,
            repo_profile=repo_profile,
            autoselect=PlanningAutoselectCommitteeConfig(),
        ):
            return "committee"
        return "legacy"

    def _allow_legacy_fallback(self, planning_config: PlanningConfig | None) -> bool:
        if planning_config is None:
            return True
        fallback = getattr(planning_config, "legacy_fallback", None)
        if fallback is None:
            return False
        return bool(getattr(fallback, "enabled", True)) and bool(
            getattr(fallback, "use_only_on_committee_failure", True)
        )

    def _should_autoselect_committee(
        self,
        *,
        summary: dict[str, Any],
        repo_profile: dict[str, Any],
        autoselect: PlanningAutoselectCommitteeConfig,
    ) -> bool:
        if not autoselect.enabled:
            return False
        acceptance_count = len(
            [str(item).strip() for item in summary.get("acceptance_criteria", []) if str(item).strip()]
        )
        complexity = str(summary.get("complexity", "")).strip().lower()
        summary_chars = len(json.dumps(summary, ensure_ascii=False))
        repo_files = len([str(item).strip() for item in repo_profile.get("files", []) if str(item).strip()])

        if acceptance_count >= autoselect.min_acceptance_criteria:
            return True
        if complexity == "complex" and acceptance_count >= autoselect.min_acceptance_criteria_when_complex:
            return True
        if complexity == "complex" and summary_chars >= autoselect.min_summary_chars_when_complex:
            return True
        if (
            repo_files >= autoselect.min_repo_files
            and acceptance_count >= autoselect.min_acceptance_criteria_with_large_repo
        ):
            return True
        return False

    def _candidate_mode_triggers(self, workflow_definition: Any) -> CandidateModeTriggers | None:
        if workflow_definition is None or getattr(workflow_definition, "config", None) is None:
            return None
        implementation = getattr(workflow_definition.config, "implementation", None)
        candidate_mode = getattr(implementation, "candidate_mode", None) if implementation is not None else None
        return getattr(candidate_mode, "triggers", None)

    def _candidate_mode_trigger_kwargs(
        self,
        candidate_mode_triggers: CandidateModeTriggers | None,
    ) -> CandidateModeDecisionKwargs:
        if candidate_mode_triggers is None:
            return {
                "rework_count_gte": 1,
                "planner_confidence_lt": 0.75,
                "require_clear_design_branches": False,
            }
        return {
            "rework_count_gte": int(getattr(candidate_mode_triggers, "rework_count_gte", 1)),
            "planner_confidence_lt": float(getattr(candidate_mode_triggers, "planner_confidence_lt", 0.75)),
            "require_clear_design_branches": bool(
                getattr(candidate_mode_triggers, "require_clear_design_branches", False)
            ),
        }

    def _candidate_decision_from_plan_v2_json(
        self,
        *,
        plan_v2: dict[str, Any],
        rework_count: int,
        candidate_mode_triggers: CandidateModeTriggers | None,
    ) -> CandidateDecision:
        surrogate = SimpleNamespace(
            planner_confidence=float(plan_v2.get("planner_confidence", 1.0) or 1.0),
            design_branches=list(plan_v2.get("design_branches", [])),
        )
        return decide_candidates(
            cast(PlanV2, surrogate),
            rework_count,
            **self._candidate_mode_trigger_kwargs(candidate_mode_triggers),
        )

    def _committee_plan_to_legacy(self, plan_v2: PlanV2) -> dict[str, Any]:
        return {
            "version": plan_v2.version,
            "goal": plan_v2.goal,
            "scope": [task.summary for task in plan_v2.tasks],
            "assumptions": list(plan_v2.constraints),
            "candidate_files": list(plan_v2.candidate_files),
            "must_not_touch": list(plan_v2.must_not_touch),
            "verification_focus": list(plan_v2.verification_focus),
            "exploration_required": plan_v2.exploration_required,
            "implementation_steps": [task.summary for task in plan_v2.tasks],
            "verification_steps": [f"Validate {item.criterion}" for item in plan_v2.test_mapping],
            "risks": [item.risk for item in plan_v2.risks],
            "high_risk_changes": [branch.summary for branch in plan_v2.design_branches[1:]],
        }

    def _committee_test_plan_to_legacy(self, plan_v2: PlanV2) -> dict[str, Any]:
        cases: list[dict[str, Any]] = []
        for index, mapping in enumerate(plan_v2.test_mapping, start=1):
            names = mapping.tests or [mapping.criterion]
            for subindex, test_name in enumerate(names, start=1):
                cases.append(
                    {
                        "id": f"TS-{index:02d}-TC-{subindex:02d}",
                        "target": test_name,
                        "name": mapping.criterion,
                        "category": "regression",
                        "priority": "p1",
                        "acceptance_criteria_refs": [mapping.criterion],
                        "preconditions": [],
                        "inputs": [],
                        "steps": [f"Run {test_name}"],
                        "expected": [f"{mapping.criterion} is satisfied"],
                        "failure_mode": mapping.criterion,
                        "notes": [],
                    }
                )
        return {
            "test_targets": [item for mapping in plan_v2.test_mapping for item in mapping.tests]
            or plan_v2.candidate_files,
            "strategy": {"unit": [], "integration": [], "e2e": [], "mocking": []},
            "cases": cases,
            "regression_risks": [item.risk for item in plan_v2.risks],
            "risks": [
                {
                    "title": item.risk,
                    "severity": "medium",
                    "likelihood": "medium",
                    "impact": item.risk,
                    "mitigation": item.mitigation,
                    "detection": "verification",
                }
                for item in plan_v2.risks
            ],
        }

    def _legacy_plan_to_plan_v2(
        self,
        *,
        summary: dict[str, Any],
        plan: dict[str, Any],
        test_plan: dict[str, Any],
        verification_plan: dict[str, Any],
    ) -> dict[str, Any]:
        acceptance_criteria = [
            str(item).strip() for item in summary.get("acceptance_criteria", []) if str(item).strip()
        ]
        candidate_files = [str(item).strip() for item in plan.get("candidate_files", []) if str(item).strip()]
        implementation_steps = [str(item).strip() for item in plan.get("implementation_steps", []) if str(item).strip()]
        high_risk_changes = [str(item).strip() for item in plan.get("high_risk_changes", []) if str(item).strip()]
        test_targets = [str(item).strip() for item in test_plan.get("test_targets", []) if str(item).strip()]
        verification_profile = str(verification_plan.get("profile", "")).strip()
        if not verification_profile:
            raw_checks = verification_plan.get("required_checks", [])
            if isinstance(raw_checks, list) and raw_checks:
                verification_profile = "generated"
        return {
            "version": int(plan.get("version", 2) or 2),
            "goal": str(plan.get("goal", "")).strip(),
            "acceptance_criteria": acceptance_criteria,
            "out_of_scope": [str(item).strip() for item in summary.get("out_of_scope", []) if str(item).strip()],
            "constraints": [str(item).strip() for item in plan.get("assumptions", []) if str(item).strip()],
            "candidate_files": candidate_files,
            "must_not_touch": [str(item).strip() for item in plan.get("must_not_touch", []) if str(item).strip()],
            "verification_focus": [
                str(item).strip() for item in plan.get("verification_focus", []) if str(item).strip()
            ],
            "exploration_required": bool(plan.get("exploration_required", False)),
            "tasks": [
                {"id": f"T{index:02d}", "summary": step, "files": list(candidate_files), "done_when": ""}
                for index, step in enumerate(implementation_steps, start=1)
            ],
            "design_branches": [
                {
                    "id": "primary",
                    "summary": "default implementation path",
                    "pros": [],
                    "cons": [],
                    "recommended": True,
                },
                *[
                    {
                        "id": f"alt{index}",
                        "summary": item,
                        "pros": [],
                        "cons": [],
                        "recommended": False,
                    }
                    for index, item in enumerate(high_risk_changes, start=1)
                ],
            ],
            "risks": [
                {"risk": str(item).strip(), "mitigation": ""} for item in plan.get("risks", []) if str(item).strip()
            ],
            "test_mapping": [
                {"criterion": criterion, "tests": list(test_targets)} for criterion in acceptance_criteria
            ],
            "verification_profile": verification_profile,
            "planner_confidence": 1.0,
        }

    def _has_plannable_summary(self, summary: dict[str, Any]) -> bool:
        goal = str(summary.get("goal", "")).strip()
        in_scope = summary.get("in_scope", [])
        acceptance = summary.get("acceptance_criteria", [])
        if goal:
            return True
        if isinstance(in_scope, list) and any(str(item).strip() for item in in_scope):
            return True
        if isinstance(acceptance, list) and any(str(item).strip() for item in acceptance):
            return True
        return False

    def _build_test_plan(
        self,
        *,
        client: ClaudeAgentClient,
        workspace: str,
        summary: dict[str, Any],
        repo_profile: dict[str, Any],
        repo_context: str,
        plan: dict[str, Any],
        planning_config: PlanningConfig | None = None,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
        debug_recorder: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        seed_context = _build_test_plan_seed_context(
            summary=summary,
            repo_profile=repo_profile,
            repo_context=repo_context,
            plan=plan,
        )
        overview_started_at_dt = datetime.now(UTC)
        overview_started_at = _format_progress_timestamp(overview_started_at_dt)
        if progress_callback is not None:
            progress_callback(
                {
                    "status": "test_plan_generating",
                    "phase": "overview",
                    "current": 0,
                    "total": 0,
                    "message": "test_plan overview を生成中",
                    "started_at": overview_started_at,
                    "last_event_at": overview_started_at,
                    "elapsed_ms": 0,
                    "last_event_kind": "started",
                }
            )
        overview_result = client.json_response_with_meta(
            TEST_PLAN_SYSTEM_PROMPT,
            (
                f"{seed_context}\n\n"
                "まず test_plan の全体方針だけを作成してください。"
                " test_targets と strategy のみを返し、cases / regression_risks / risks はまだ出力しないでください。"
            ),
            cwd=workspace,
            max_turns=4,
            allowed_tools=READ_ONLY_TOOLS,
            permission_mode="default",
            setting_sources=self._planning_setting_sources(planning_config),
            output_schema=TEST_PLAN_OVERVIEW_SCHEMA,
            prompt_kind="test_plan",
            debug_recorder=debug_recorder,
            debug_context={"phase": "overview"},
            include_partial_messages=True,
            event_callback=_make_test_plan_progress_event_callback(
                progress_callback,
                status="test_plan_generating",
                phase="overview",
                current=0,
                total=0,
                message="test_plan overview を生成中",
                started_at=overview_started_at_dt,
            ),
        )
        overview = overview_result.payload
        acceptance_items = [str(item).strip() for item in summary.get("acceptance_criteria", []) if str(item).strip()]
        if not acceptance_items:
            acceptance_items = ["全体要件"]
        if progress_callback is not None:
            progress_callback(
                {
                    "status": "test_plan_generating",
                    "phase": "overview_completed",
                    "current": 0,
                    "total": len(acceptance_items),
                    "message": "test_plan overview 完了",
                    "started_at": overview_started_at,
                    "last_event_at": _format_progress_timestamp(),
                    "elapsed_ms": _elapsed_ms(overview_started_at_dt),
                    "last_event_kind": "completed",
                    "session_id": overview_result.session_id or "",
                }
            )

        case_chunks: list[dict[str, Any]] = []
        for index, acceptance in enumerate(acceptance_items, start=1):
            chunk_started_at_dt = datetime.now(UTC)
            chunk_started_at = _format_progress_timestamp(chunk_started_at_dt)
            if progress_callback is not None:
                progress_callback(
                    {
                        "status": "test_plan_generating",
                        "phase": "acceptance_criterion",
                        "current": index,
                        "total": len(acceptance_items),
                        "acceptance_criterion": acceptance,
                        "message": f"test_plan chunk {index}/{len(acceptance_items)} を生成中",
                        "started_at": chunk_started_at,
                        "last_event_at": chunk_started_at,
                        "elapsed_ms": 0,
                        "last_event_kind": "started",
                    }
                )
            chunk_prompt = (
                f"{seed_context}\n\n"
                f"overview:\n{json.dumps(overview, ensure_ascii=False, indent=2)}\n\n"
                f"対象の acceptance criterion ({index}/{len(acceptance_items)}): {acceptance}\n\n"
                "この acceptance criterion に対応する cases / regression_risks / risks のみを返してください。"
                " cases はこの acceptance criterion を必ず参照し、他の acceptance criterion のケースは混ぜないでください。"
                " overview で確定した test_targets / strategy と矛盾しない内容にしてください。"
            )
            partial_result = client.json_response_with_meta(
                TEST_PLAN_SYSTEM_PROMPT,
                chunk_prompt,
                cwd=workspace,
                max_turns=4,
                allowed_tools=READ_ONLY_TOOLS,
                permission_mode="default",
                setting_sources=self._planning_setting_sources(planning_config),
                output_schema=TEST_PLAN_AC_SCHEMA,
                prompt_kind="test_plan",
                debug_recorder=debug_recorder,
                debug_context={
                    "phase": "acceptance_criterion",
                    "phase_index": index,
                    "phase_label": acceptance,
                },
                resume_session_id=overview_result.session_id,
                continue_conversation=bool(overview_result.session_id),
                fork_session=bool(overview_result.session_id),
                include_partial_messages=True,
                event_callback=_make_test_plan_progress_event_callback(
                    progress_callback,
                    status="test_plan_generating",
                    phase="acceptance_criterion",
                    current=index,
                    total=len(acceptance_items),
                    message=f"test_plan chunk {index}/{len(acceptance_items)} を生成中",
                    started_at=chunk_started_at_dt,
                    acceptance_criterion=acceptance,
                ),
            )
            case_chunks.append(partial_result.payload)
            if progress_callback is not None:
                progress_callback(
                    {
                        "status": "test_plan_generating",
                        "phase": "acceptance_criterion_completed",
                        "current": index,
                        "total": len(acceptance_items),
                        "acceptance_criterion": acceptance,
                        "message": f"test_plan chunk {index}/{len(acceptance_items)} 完了",
                        "started_at": chunk_started_at,
                        "last_event_at": _format_progress_timestamp(),
                        "elapsed_ms": _elapsed_ms(chunk_started_at_dt),
                        "last_event_kind": "completed",
                        "session_id": partial_result.session_id or "",
                    }
                )

        return _merge_test_plan_chunks(overview, case_chunks)


def _build_repo_context(workspace: str, repo_profile: dict[str, Any]) -> str:
    root = Path(workspace)
    sections: list[str] = []

    readme_files = [str(item).strip() for item in repo_profile.get("readme_files", []) if str(item).strip()]
    for relative_path in readme_files[:3]:
        excerpt = _read_excerpt(root / relative_path, 6000)
        if excerpt:
            sections.append(f"[file] {relative_path}\n{excerpt}")

    for relative_path in ("WORKFLOW.md", "docs/ARCHITECTURE.md", "package.json", "pyproject.toml", "requirements.txt"):
        excerpt = _read_excerpt(root / relative_path, 4000)
        if excerpt:
            sections.append(f"[file] {relative_path}\n{excerpt}")

    files = [str(item).strip() for item in repo_profile.get("files", []) if str(item).strip()]
    if files:
        sections.append("[file_list]\n" + "\n".join(files[:200]))

    return "\n\n".join(sections)[:20000]


def _build_test_plan_repo_context(workspace: str, repo_profile: dict[str, Any]) -> str:
    root = Path(workspace)
    sections: list[str] = []

    readme_files = [str(item).strip() for item in repo_profile.get("readme_files", []) if str(item).strip()]
    for relative_path in readme_files[:1]:
        excerpt = _read_excerpt(root / relative_path, 1800)
        if excerpt:
            sections.append(f"[file] {relative_path}\n{excerpt}")

    for relative_path in ("package.json", "pyproject.toml", "requirements.txt"):
        excerpt = _read_excerpt(root / relative_path, 1200)
        if excerpt:
            sections.append(f"[file] {relative_path}\n{excerpt}")

    files = [str(item).strip() for item in repo_profile.get("files", []) if str(item).strip()]
    if files:
        sections.append("[file_list]\n" + "\n".join(files[:40]))

    return "\n\n".join(sections)[:4000]


def _read_excerpt(path: Path, limit: int) -> str:
    if not path.exists() or not path.is_file():
        return ""
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        text = path.read_text(encoding="utf-8", errors="replace")
    return text[:limit].strip()


def _build_test_plan_seed_context(
    *,
    summary: dict[str, Any],
    repo_profile: dict[str, Any],
    repo_context: str,
    plan: dict[str, Any],
) -> str:
    compact_payload = {
        "requirement_summary": {
            "goal": _truncate_text(summary.get("goal", ""), 800),
            "in_scope": _coerce_string_list(summary.get("in_scope"), limit=12),
            "acceptance_criteria": _coerce_string_list(summary.get("acceptance_criteria"), limit=20),
            "constraints": _coerce_string_list(summary.get("constraints"), limit=8),
            "test_focus": _coerce_string_list(summary.get("test_focus"), limit=8),
            "preferred_outcomes": _coerce_string_list(summary.get("preferred_outcomes"), limit=6),
            "disallowed_approaches": _coerce_string_list(summary.get("disallowed_approaches"), limit=6),
            "recommended_direction": _truncate_text(summary.get("recommended_direction", ""), 500),
        },
        "repo_profile": {
            "languages": _coerce_string_list(repo_profile.get("languages"), limit=6),
            "setup_commands": _coerce_string_list(repo_profile.get("setup_commands"), limit=4),
            "test_commands": _coerce_string_list(repo_profile.get("test_commands"), limit=4),
            "lint_commands": _coerce_string_list(repo_profile.get("lint_commands"), limit=4),
            "typecheck_commands": _coerce_string_list(repo_profile.get("typecheck_commands"), limit=4),
            "format_commands": _coerce_string_list(repo_profile.get("format_commands"), limit=4),
            "build_commands": _coerce_string_list(repo_profile.get("build_commands"), limit=4),
            "suggested_verification_profile": _truncate_text(
                repo_profile.get("suggested_verification_profile", ""),
                200,
            ),
            "files": _coerce_string_list(repo_profile.get("files"), limit=40),
        },
        "repo_context_excerpt": _truncate_text(repo_context, 2400),
        "plan": {
            "goal": _truncate_text(plan.get("goal", ""), 400),
            "scope": _coerce_string_list(plan.get("scope"), limit=12),
            "candidate_files": _coerce_string_list(plan.get("candidate_files"), limit=20),
            "must_not_touch": _coerce_string_list(plan.get("must_not_touch"), limit=12),
            "verification_focus": _coerce_string_list(plan.get("verification_focus"), limit=12),
            "implementation_steps": _coerce_string_list(plan.get("implementation_steps"), limit=12),
            "verification_steps": _coerce_string_list(plan.get("verification_steps"), limit=12),
            "risks": _coerce_string_list(plan.get("risks"), limit=8),
            "high_risk_changes": _coerce_string_list(plan.get("high_risk_changes"), limit=8),
        },
    }
    return (
        "以下の compact planning context を前提に test_plan を作成してください。"
        " 追加の repo 読み直しは避け、ここにある情報と既存の読取結果だけで判断してください。\n\n"
        f"test_plan_seed:\n{json.dumps(compact_payload, ensure_ascii=False, indent=2)}"
    )


def _coerce_string_list(value: Any, *, limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    items = [str(item).strip() for item in value if str(item).strip()]
    return items[:limit]


def _truncate_text(value: Any, limit: int) -> str:
    text = str(value).strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."


def _format_progress_timestamp(value: datetime | None = None) -> str:
    timestamp = value or datetime.now(UTC)
    return timestamp.isoformat().replace("+00:00", "Z")


def _elapsed_ms(started_at: datetime) -> int:
    return max(0, int((datetime.now(UTC) - started_at).total_seconds() * 1000))


def _make_test_plan_progress_event_callback(
    progress_callback: Callable[[dict[str, Any]], None] | None,
    *,
    status: str,
    phase: str,
    current: int,
    total: int,
    message: str,
    started_at: datetime,
    acceptance_criterion: str = "",
) -> Callable[[dict[str, Any]], None] | None:
    if progress_callback is None:
        return None

    started_at_text = _format_progress_timestamp(started_at)

    def _callback(event: dict[str, Any]) -> None:
        payload: dict[str, Any] = {
            "status": status,
            "phase": phase,
            "current": current,
            "total": total,
            "message": message,
            "started_at": started_at_text,
            "last_event_at": str(event.get("timestamp") or _format_progress_timestamp()),
            "elapsed_ms": _elapsed_ms(started_at),
            "last_event_kind": str(event.get("event_kind") or "").strip() or "event",
        }
        if acceptance_criterion:
            payload["acceptance_criterion"] = acceptance_criterion
        session_id = str(event.get("session_id") or "").strip()
        if session_id:
            payload["session_id"] = session_id
        progress_callback(payload)

    return _callback


def _merge_test_plan_chunks(overview: dict[str, Any], chunks: list[dict[str, Any]]) -> dict[str, Any]:
    strategy = overview.get("strategy", {})
    merged_strategy = {
        key: _dedupe_preserve_order(strategy.get(key, [])) for key in ("unit", "integration", "e2e", "mocking")
    }
    all_cases: list[dict[str, Any]] = []
    regression_risks: list[str] = []
    risks: list[dict[str, Any]] = []
    seen_risk_titles: set[str] = set()

    for chunk in chunks:
        all_cases.extend(chunk.get("cases", []))
        regression_risks.extend(str(item).strip() for item in chunk.get("regression_risks", []) if str(item).strip())
        for risk in chunk.get("risks", []):
            if not isinstance(risk, dict):
                continue
            title = str(risk.get("title", "")).strip()
            if title and title not in seen_risk_titles:
                seen_risk_titles.add(title)
                risks.append(risk)

    return {
        "test_targets": _dedupe_preserve_order(overview.get("test_targets", [])),
        "strategy": merged_strategy,
        "cases": _renumber_test_cases(all_cases),
        "regression_risks": _dedupe_preserve_order(regression_risks),
        "risks": risks,
    }


def _dedupe_preserve_order(items: list[Any]) -> list[Any]:
    seen: set[str] = set()
    ordered: list[Any] = []
    for item in items:
        key = json.dumps(item, ensure_ascii=False, sort_keys=True) if isinstance(item, (dict, list)) else str(item)
        if key in seen:
            continue
        seen.add(key)
        ordered.append(item)
    return ordered


def _renumber_test_cases(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    renumbered: list[dict[str, Any]] = []
    target_map: dict[str, int] = {}
    target_counter = 1
    case_counter_by_target: dict[int, int] = {}
    for case in cases:
        if not isinstance(case, dict):
            continue
        target = str(case.get("target", "")).strip() or "general"
        if target not in target_map:
            target_map[target] = target_counter
            target_counter += 1
        target_number = target_map[target]
        case_counter_by_target[target_number] = case_counter_by_target.get(target_number, 0) + 1
        cloned = dict(case)
        cloned["id"] = f"TS-{target_number:02d}-TC-{case_counter_by_target[target_number]:02d}"
        renumbered.append(cloned)
    return renumbered


@dataclass(frozen=True)
class _CommitteeIssueContext:
    issue_key: str
    repo_root: str
    workpad_text: str
    issue_body: str
    acceptance_hints: list[str]
    extra_docs: list[str]


def _run_async(coro: Any) -> Any:
    import asyncio

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    raise RuntimeError(f"Planner committee cannot run inside active event loop: {loop}")


def _to_jsonable(value: Any) -> dict[str, Any]:
    from dataclasses import asdict, is_dataclass

    if is_dataclass(value):
        data = asdict(cast(Any, value))
        return data if isinstance(data, dict) else {"value": data}
    if isinstance(value, dict):
        return value
    return {"value": value}


def _plan_v2_to_json(plan_v2: PlanV2) -> dict[str, Any]:
    return {
        "version": plan_v2.version,
        "goal": plan_v2.goal,
        "acceptance_criteria": list(plan_v2.acceptance_criteria),
        "out_of_scope": list(plan_v2.out_of_scope),
        "constraints": list(plan_v2.constraints),
        "candidate_files": list(plan_v2.candidate_files),
        "must_not_touch": list(plan_v2.must_not_touch),
        "verification_focus": list(plan_v2.verification_focus),
        "exploration_required": plan_v2.exploration_required,
        "tasks": [
            {"id": task.id, "summary": task.summary, "files": list(task.files), "done_when": task.done_when}
            for task in plan_v2.tasks
        ],
        "design_branches": [
            {
                "id": branch.id,
                "summary": branch.summary,
                "pros": list(branch.pros),
                "cons": list(branch.cons),
                "recommended": branch.recommended,
            }
            for branch in plan_v2.design_branches
        ],
        "risks": [{"risk": item.risk, "mitigation": item.mitigation} for item in plan_v2.risks],
        "test_mapping": [{"criterion": item.criterion, "tests": list(item.tests)} for item in plan_v2.test_mapping],
        "verification_profile": plan_v2.verification_profile,
        "planner_confidence": plan_v2.planner_confidence,
    }


def _candidate_decision_to_json(decision: CandidateDecision) -> dict[str, Any]:
    return {
        "enabled": decision.enabled,
        "candidate_ids": list(decision.candidate_ids),
    }

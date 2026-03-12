from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.agent_sdk_client import ClaudeAgentClient
from app.config import Settings
from app.verification_profiles import build_verification_plan

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
        "goal": {"type": "string"},
        "scope": {"type": "array", "items": {"type": "string"}},
        "assumptions": {"type": "array", "items": {"type": "string"}},
        "candidate_files": {"type": "array", "items": {"type": "string"}},
        "implementation_steps": {"type": "array", "items": {"type": "string"}},
        "verification_steps": {"type": "array", "items": {"type": "string"}},
        "risks": {"type": "array", "items": {"type": "string"}},
        "high_risk_changes": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "goal",
        "scope",
        "assumptions",
        "candidate_files",
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


class PlanningAgent:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def build_artifacts(
        self,
        *,
        workspace: str,
        summary: dict[str, Any],
        repo_profile: dict[str, Any],
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> PlanningArtifacts:
        if not self._has_plannable_summary(summary):
            raise ValueError(
                "requirement_summary.json に planning に必要な goal / in_scope / acceptance_criteria が不足しています。"
            )
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
            setting_sources=[],
            output_schema=PLAN_SCHEMA,
            prompt_kind="plan",
        )
        test_plan = self._build_test_plan(
            client=test_plan_client,
            workspace=workspace,
            summary=summary,
            repo_profile=repo_profile,
            repo_context=_build_test_plan_repo_context(workspace, repo_profile),
            plan=plan,
            progress_callback=progress_callback,
        )
        verification_plan = build_verification_plan(workspace=workspace, repo_profile=repo_profile, plan=plan)
        return PlanningArtifacts(
            repo_profile=repo_profile,
            plan=plan,
            test_plan=test_plan,
            verification_plan=verification_plan,
        )

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
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        common_prompt = (
            "以下の要件サマリーとリポジトリ情報を見て判断してください。\n\n"
            f"requirement_summary:\n{json.dumps(summary, ensure_ascii=False, indent=2)}\n\n"
            f"repo_profile:\n{json.dumps(repo_profile, ensure_ascii=False, indent=2)}\n\n"
            f"repo_context:\n{repo_context}\n\n"
            f"plan.json:\n{json.dumps(plan, ensure_ascii=False, indent=2)}\n"
        )
        if progress_callback is not None:
            progress_callback(
                {
                    "status": "test_plan_generating",
                    "phase": "overview",
                    "current": 0,
                    "total": 0,
                    "message": "test_plan overview を生成中",
                }
            )
        overview_result = client.json_response_with_meta(
            TEST_PLAN_SYSTEM_PROMPT,
            (
                f"{common_prompt}\n"
                "まず test_plan の全体方針だけを作成してください。"
                " test_targets と strategy のみを返し、cases / regression_risks / risks はまだ出力しないでください。"
            ),
            cwd=workspace,
            max_turns=4,
            allowed_tools=READ_ONLY_TOOLS,
            permission_mode="default",
            setting_sources=[],
            output_schema=TEST_PLAN_OVERVIEW_SCHEMA,
            prompt_kind="test_plan",
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
                    "session_id": overview_result.session_id or "",
                }
            )

        case_chunks: list[dict[str, Any]] = []
        for index, acceptance in enumerate(acceptance_items, start=1):
            if progress_callback is not None:
                progress_callback(
                    {
                        "status": "test_plan_generating",
                        "phase": "acceptance_criterion",
                        "current": index,
                        "total": len(acceptance_items),
                        "acceptance_criterion": acceptance,
                        "message": f"test_plan chunk {index}/{len(acceptance_items)} を生成中",
                    }
                )
            partial_result = client.json_response_with_meta(
                TEST_PLAN_SYSTEM_PROMPT,
                (
                    f"{common_prompt}\n"
                    f"対象の acceptance criterion ({index}/{len(acceptance_items)}): {acceptance}\n\n"
                    "この acceptance criterion に対応する cases / regression_risks / risks のみを返してください。"
                    " cases はこの acceptance criterion を必ず参照し、他の acceptance criterion のケースは混ぜないでください。"
                ),
                cwd=workspace,
                max_turns=4,
                allowed_tools=READ_ONLY_TOOLS,
                permission_mode="default",
                setting_sources=[],
                output_schema=TEST_PLAN_AC_SCHEMA,
                prompt_kind="test_plan",
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
    for relative_path in readme_files[:2]:
        excerpt = _read_excerpt(root / relative_path, 3000)
        if excerpt:
            sections.append(f"[file] {relative_path}\n{excerpt}")

    for relative_path in ("package.json", "pyproject.toml", "requirements.txt"):
        excerpt = _read_excerpt(root / relative_path, 2000)
        if excerpt:
            sections.append(f"[file] {relative_path}\n{excerpt}")

    files = [str(item).strip() for item in repo_profile.get("files", []) if str(item).strip()]
    if files:
        sections.append("[file_list]\n" + "\n".join(files[:80]))

    return "\n\n".join(sections)[:8000]


def _read_excerpt(path: Path, limit: int) -> str:
    if not path.exists() or not path.is_file():
        return ""
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        text = path.read_text(encoding="utf-8", errors="replace")
    return text[:limit].strip()


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

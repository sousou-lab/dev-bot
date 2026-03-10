from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from claude_agent_sdk import AgentDefinition, ClaudeSDKClient, HookMatcher

from app.agent_sdk_client import AgentResult, ClaudeAgentClient, _build_options, _collect_client_agent_result
from app.repo_profiler import build_repo_profile

AUTONOMOUS_IMPLEMENT_SYSTEM = """あなたはソフトウェア実装エージェントです。
与えられた要件と既存リポジトリを見て、作業ディレクトリ内で自律的に実装を完了してください。

必須ルール:
- 作業ディレクトリ内のファイルを直接読んで編集する
- 必要に応じて Bash でセットアップ・テスト・静的解析を実行する
- まず現状を把握し、最小の変更で目的を達成する
- 失敗したテストやコマンド結果を見て修正を反復する
- plan.json と test_plan.json があれば、それを最優先の実装契約として扱う
- TodoWrite を使って作業状況を管理してよい
- 最後に、実施内容と検証結果だけを JSON で返す
- JSON 以外は返さない

返却形式:
{
  "summary": "実装概要",
  "tests": [
    {"command": "実行コマンド", "status": "passed または failed", "details": "要約"}
  ],
  "changed_files": ["relative/path"],
  "notes": ["補足"]
}
"""


DEFAULT_ALLOWED_TOOLS = [
    "Read",
    "Write",
    "Edit",
    "Glob",
    "Grep",
    "Bash",
    "TodoWrite",
    "Task",
]

NOTIFIABLE_TOOLS = {"Bash", "Write", "Edit", "Task"}
NOTIFIABLE_SUBAGENTS = {"requirements-analyst", "test-designer", "implementer", "test-runner"}
ACTIVITY_HISTORY_LIMIT = 200
DANGEROUS_BASH_PATTERNS = (
    "rm -rf /",
    "sudo ",
    "shutdown",
    "reboot",
    "halt",
    "poweroff",
    "terraform apply",
    "kubectl delete",
    "git push --force",
    "drop database",
    "drop schema",
)


@dataclass
class ActivityStore:
    artifacts_dir: Path
    sequence: int = 0

    def __post_init__(self) -> None:
        self.current_path = self.artifacts_dir / "current_activity.json"
        self.history_path = self.artifacts_dir / "activity_history.json"
        self.log_path = self.artifacts_dir / "run.log"
        self.error_path = self.artifacts_dir / "agent_failure.json"
        self.last_failure_path = self.artifacts_dir / "last_failure.json"
        self._history: deque[dict[str, Any]] = deque(maxlen=ACTIVITY_HISTORY_LIMIT)
        if self.history_path.exists():
            try:
                existing = json.loads(self.history_path.read_text(encoding="utf-8"))
                if isinstance(existing, list):
                    self._history.extend(item for item in existing if isinstance(item, dict))
            except json.JSONDecodeError:
                pass
        if self._history:
            self.sequence = max(int(item.get("sequence", 0)) for item in self._history)

    def record(
        self,
        *,
        phase: str,
        tool_name: str,
        summary: str,
        status: str,
        tool_use_id: str | None = None,
        details: dict[str, Any] | None = None,
        notify: bool = False,
    ) -> None:
        self.sequence += 1
        payload = {
            "sequence": self.sequence,
            "timestamp": _now(),
            "phase": phase,
            "tool_name": tool_name,
            "summary": summary,
            "status": status,
            "tool_use_id": tool_use_id,
            "details": details or {},
            "notify": notify,
        }
        self._history.append(payload)
        self.current_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        self.history_path.write_text(json.dumps(list(self._history), ensure_ascii=False, indent=2), encoding="utf-8")
        with self.log_path.open("a", encoding="utf-8") as fh:
            fh.write(f"{payload['timestamp']} [{status}] {tool_name}: {summary}\n")
            if details:
                fh.write(f"{json.dumps(details, ensure_ascii=False, indent=2)}\n")
        if status == "failed":
            self.last_failure_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def record_failure(self, message: str, stderr: list[str] | None = None) -> None:
        payload = {
            "timestamp": _now(),
            "message": message,
            "stderr": stderr or [],
        }
        self.error_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        self.last_failure_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        with self.log_path.open("a", encoding="utf-8") as fh:
            fh.write(f"{payload['timestamp']} [error] {message}\n")


def main() -> int:
    args = _parse_args()
    workspace = args.workspace or os.environ.get("WORKSPACE_DIR", "/workspace")
    artifacts = Path(args.artifacts_dir or os.environ.get("ARTIFACTS_DIR", "/artifacts"))
    summary = json.loads((artifacts / "requirement_summary.json").read_text(encoding="utf-8"))
    issue = json.loads((artifacts / "issue.json").read_text(encoding="utf-8"))
    plan = _load_optional_json(artifacts / "plan.json")
    test_plan = _load_optional_json(artifacts / "test_plan.json")
    profile = build_repo_profile(workspace)
    _write_json(artifacts / "repo_profile.json", profile)

    activity_store = ActivityStore(artifacts)
    autonomous_timeout_seconds = float(os.environ.get("AUTONOMOUS_AGENT_TIMEOUT_SECONDS", "900"))
    client = ClaudeAgentClient(
        api_key=os.environ.get("ANTHROPIC_API_KEY"),
        timeout_seconds=autonomous_timeout_seconds,
    )

    max_iterations = int(os.environ.get("MAX_IMPLEMENTATION_ITERATIONS", "5"))
    verification_history: list[dict[str, Any]] = []
    last_verification: dict[str, Any] = {}
    try:
        last_agent_result, last_verification = asyncio.run(
            _run_autonomous_iterations(
                client=client,
                workspace=workspace,
                artifacts_dir=artifacts,
                summary=summary,
                issue=issue,
                plan=plan if isinstance(plan, dict) else {},
                test_plan=test_plan if isinstance(test_plan, dict) else {},
                profile=profile,
                activity_store=activity_store,
                max_iterations=max_iterations,
                verification_history=verification_history,
            )
        )
    except Exception as exc:
        activity_store.record_failure(str(exc))
        raise

    final_payload = {
        "success": bool(last_verification.get("success")),
        "issue_number": issue.get("number"),
        "agent_result": last_agent_result,
        "test_result": last_verification,
    }
    _write_json(artifacts / "final_result.json", final_payload)
    _write_json(artifacts / "verification_history.json", {"items": verification_history})
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace")
    parser.add_argument("--artifacts-dir")
    return parser.parse_args()


def _build_iteration_prompt(
    summary: dict,
    issue: dict,
    plan: dict[str, Any],
    test_plan: dict[str, Any],
    profile: dict,
    iteration: int,
    previous_verification: dict[str, Any],
    verification_history: list[dict[str, Any]],
) -> str:
    return (
        f"実装イテレーション: {iteration}\n\n"
        "Issue 情報:\n"
        f"{json.dumps(issue, ensure_ascii=False, indent=2)}\n\n"
        "要件サマリー:\n"
        f"{json.dumps(summary, ensure_ascii=False, indent=2)}\n\n"
        "実装計画 plan.json:\n"
        f"{json.dumps(plan, ensure_ascii=False, indent=2)}\n\n"
        "テスト計画 test_plan.json:\n"
        f"{json.dumps(test_plan, ensure_ascii=False, indent=2)}\n\n"
        "リポジトリプロフィール:\n"
        f"{json.dumps(profile, ensure_ascii=False, indent=2)}\n\n"
        "これまでの検証履歴:\n"
        f"{json.dumps(verification_history, ensure_ascii=False, indent=2)}\n\n"
        "直前の検証結果:\n"
        f"{json.dumps(previous_verification, ensure_ascii=False, indent=2)}\n\n"
        "この workspace 内で直接調査・編集・検証を行ってください。\n"
        "候補のセットアップコマンドとテストコマンドは repo_profile を参考にしてください。\n"
        " 直前の検証が失敗している場合は、その失敗を解消する修正を優先してください。\n"
        "最後は指定した JSON だけを返してください。"
    )


async def _run_autonomous_iterations(
    *,
    client: ClaudeAgentClient,
    workspace: str,
    artifacts_dir: Path,
    summary: dict,
    issue: dict,
    plan: dict[str, Any],
    test_plan: dict[str, Any],
    profile: dict,
    activity_store: ActivityStore,
    max_iterations: int,
    verification_history: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    options, _stderr_lines = _build_options(
        api_key=client.api_key,
        system={
            "type": "preset",
            "preset": "claude_code",
            "append": AUTONOMOUS_IMPLEMENT_SYSTEM,
        },
        cwd=workspace,
        max_turns=max_iterations * 4,
        allowed_tools=DEFAULT_ALLOWED_TOOLS,
        permission_mode="default",
        setting_sources=["project"],
        hooks=_build_hooks(activity_store),
        agents=_build_subagents(),
        output_schema={"type": "object"},
    )
    last_verification: dict[str, Any] = {}
    last_agent_result: dict[str, Any] = {}
    timeout_seconds = client.timeout_seconds

    async def _sequence() -> tuple[dict[str, Any], dict[str, Any]]:
        nonlocal last_verification, last_agent_result
        async with ClaudeSDKClient(options=options) as sdk_client:
            for iteration in range(1, max_iterations + 1):
                activity_store.record(
                    phase="agent",
                    tool_name="Agent",
                    summary=f"{iteration} 回目の実装を開始します。",
                    status="started",
                    notify=True,
                    details={"iteration": iteration},
                )
                prompt = _build_iteration_prompt(
                    summary=summary,
                    issue=issue,
                    plan=plan,
                    test_plan=test_plan,
                    profile=profile,
                    iteration=iteration,
                    previous_verification=last_verification,
                    verification_history=verification_history,
                )
                await sdk_client.query(prompt)
                raw_result = await _collect_client_agent_result(sdk_client)
                last_agent_result = (
                    raw_result.structured_output if isinstance(raw_result.structured_output, dict) else {}
                )
                if not last_agent_result:
                    try:
                        last_agent_result = json.loads(raw_result.result)
                    except json.JSONDecodeError as exc:
                        raise RuntimeError(
                            f"Claude Agent SDK did not return valid JSON. Raw response: {raw_result.result[:500]!r}"
                        ) from exc
                last_agent_result["session_id"] = raw_result.session_id
                last_agent_result["total_cost_usd"] = raw_result.total_cost_usd
                last_agent_result["usage"] = raw_result.usage
                activity_store.record(
                    phase="agent",
                    tool_name="Agent",
                    summary=f"{iteration} 回目の自律実装が完了しました。",
                    status="completed",
                    notify=True,
                )
                _write_json(artifacts_dir / f"agent_result_{iteration}.json", last_agent_result)
                _write_json(artifacts_dir / "agent_result.json", last_agent_result)
                last_verification = _run_commands(workspace, profile)
                last_verification["iteration"] = iteration
                verification_history.append(last_verification)
                _write_json(artifacts_dir / f"verification_result_{iteration}.json", last_verification)
                _write_json(artifacts_dir / "verification_result.json", last_verification)
                if last_verification.get("success"):
                    break
                activity_store.record(
                    phase="verification",
                    tool_name="Verification",
                    summary=f"{iteration} 回目の検証で失敗しました。修正を続行します。",
                    status="failed",
                    notify=True,
                    details=_truncate_details(last_verification),
                )
        return last_agent_result, last_verification

    if timeout_seconds is None:
        return await _sequence()
    return await asyncio.wait_for(_sequence(), timeout=timeout_seconds)


def _build_hooks(activity_store: ActivityStore) -> dict[str, list[HookMatcher]]:
    async def pre_tool_use(input_data: dict[str, Any], tool_use_id: str | None, context: Any) -> dict[str, Any]:
        tool_name = str(input_data.get("tool_name", "unknown"))
        tool_input = input_data.get("tool_input", {})
        if tool_name == "Bash":
            command = str(tool_input.get("command") or "").strip().lower()
            for pattern in DANGEROUS_BASH_PATTERNS:
                if pattern in command:
                    activity_store.record(
                        phase="tool",
                        tool_name=tool_name,
                        summary=f"危険な Bash コマンドを拒否しました: {pattern}",
                        status="failed",
                        tool_use_id=tool_use_id,
                        details={"tool_input": _truncate_details(tool_input)},
                        notify=True,
                    )
                    return {
                        "hookSpecificOutput": {
                            "hookEventName": "PreToolUse",
                            "permissionDecision": "deny",
                            "permissionDecisionReason": f"Dangerous bash command blocked: {pattern}",
                        }
                    }
        summary = _summarize_tool(tool_name, tool_input)
        activity_store.record(
            phase="tool",
            tool_name=tool_name,
            summary=summary,
            status="started",
            tool_use_id=tool_use_id,
            details={"tool_input": _truncate_details(tool_input)},
            notify=tool_name in NOTIFIABLE_TOOLS,
        )
        return {}

    async def post_tool_use(input_data: dict[str, Any], tool_use_id: str | None, context: Any) -> dict[str, Any]:
        tool_name = str(input_data.get("tool_name", "unknown"))
        summary = _summarize_tool_result(tool_name, input_data.get("tool_response"))
        activity_store.record(
            phase="tool",
            tool_name=tool_name,
            summary=summary,
            status="completed",
            tool_use_id=tool_use_id,
            details={"tool_response": _truncate_details(input_data.get("tool_response"))},
            notify=False,
        )
        return {}

    async def post_tool_failure(input_data: dict[str, Any], tool_use_id: str | None, context: Any) -> dict[str, Any]:
        tool_name = str(input_data.get("tool_name", "unknown"))
        summary = _summarize_tool_failure(tool_name, input_data.get("tool_response"))
        details = _build_failure_details(input_data)
        activity_store.record(
            phase="tool",
            tool_name=tool_name,
            summary=summary,
            status="failed",
            tool_use_id=tool_use_id,
            details=details,
            notify=True,
        )
        return {}

    async def subagent_start(input_data: dict[str, Any], tool_use_id: str | None, context: Any) -> dict[str, Any]:
        agent_type = str(input_data.get("agent_type", "unknown"))
        activity_store.record(
            phase="subagent",
            tool_name=agent_type,
            summary=f"サブエージェント `{agent_type}` を開始しました",
            status="started",
            tool_use_id=tool_use_id,
            details=_truncate_details(input_data),
            notify=agent_type in NOTIFIABLE_SUBAGENTS,
        )
        return {}

    async def subagent_stop(input_data: dict[str, Any], tool_use_id: str | None, context: Any) -> dict[str, Any]:
        agent_type = str(input_data.get("agent_type", "unknown"))
        activity_store.record(
            phase="subagent",
            tool_name=agent_type,
            summary=f"サブエージェント `{agent_type}` が完了しました",
            status="completed",
            tool_use_id=tool_use_id,
            details=_truncate_details(input_data),
            notify=False,
        )
        return {}

    return {
        "PreToolUse": [HookMatcher(hooks=[pre_tool_use])],
        "PostToolUse": [HookMatcher(hooks=[post_tool_use])],
        "PostToolUseFailure": [HookMatcher(hooks=[post_tool_failure])],
        "SubagentStart": [HookMatcher(hooks=[subagent_start])],
        "SubagentStop": [HookMatcher(hooks=[subagent_stop])],
    }


def _build_subagents() -> dict[str, AgentDefinition]:
    return {
        "requirements-analyst": AgentDefinition(
            description="要件の分析と不足情報の特定を行う専門エージェント。仕様の不明点整理や追加調査が必要なときに使う。",
            prompt=(
                "あなたは要件分析の専門家です。"
                " 要件の曖昧さ、不足情報、受け入れ条件、制約を整理してください。"
                " 調査が必要な場合は必要最小限の範囲で行い、実装そのものは行わないでください。"
            ),
            tools=["Read", "Grep", "Glob", "WebSearch"],
            model="sonnet",
        ),
        "test-designer": AgentDefinition(
            description="テスト観点の整理、追加すべきテストケースの洗い出し、失敗パターンの分析に使う。",
            prompt=(
                "あなたはテスト設計の専門家です。"
                " 要件と既存コードから必要なテストケース、境界条件、回帰観点を整理してください。"
                " 実装変更やコマンド実行は行わないでください。"
            ),
            tools=["Read", "Grep", "Glob"],
            model="sonnet",
        ),
        "implementer": AgentDefinition(
            description="コード実装とファイル修正を行う専門エージェント。具体的な修正が必要なときに使う。",
            prompt=(
                "あなたは実装担当エージェントです。"
                " 要件と既存コードに基づいて、最小の変更で必要な実装を行ってください。"
                " テスト実行は行わず、必要なコード変更に集中してください。"
            ),
            tools=["Read", "Grep", "Glob", "Edit", "Write"],
            model="sonnet",
        ),
        "test-runner": AgentDefinition(
            description="テスト実行と失敗分析を行う専門エージェント。コマンド実行と結果の解釈が必要なときに使う。",
            prompt=(
                "あなたはテスト実行担当エージェントです。"
                " テストや検証コマンドを実行し、失敗原因を簡潔に分析してください。"
                " 不要なコード変更は行わず、実行結果の解釈に集中してください。"
            ),
            tools=["Bash", "Read", "Grep"],
            model="sonnet",
        ),
        "code-reviewer": AgentDefinition(
            description="最終レビュー、保守性、設計、リスク確認を行う専門エージェント。実装後の品質確認に使う。",
            prompt=(
                "あなたはコードレビュー担当エージェントです。"
                " 変更内容を読み取り、保守性、設計整合性、潜在的リスクを確認してください。"
                " 実装変更やコマンド実行は行わないでください。"
            ),
            tools=["Read", "Grep", "Glob"],
            model="sonnet",
        ),
    }


def _run_commands(workspace: str, profile: dict) -> dict:
    steps: list[dict[str, Any]] = []
    migration = profile.get("migration") if isinstance(profile.get("migration"), dict) else {}
    apply_cmds = list(migration.get("apply_cmds", [])) if migration else []
    rollback_cmds = list(migration.get("rollback_cmds", [])) if migration else []

    for cmd in profile.get("setup_commands", []):
        result = _run_shell(cmd, workspace)
        result["phase"] = "setup"
        steps.append(result)
        if result["exit_code"] != 0:
            return {
                "success": False,
                "phase": "setup",
                "command": cmd,
                "steps": steps,
                "output": result["output"],
            }

    for cmd in apply_cmds:
        result = _run_shell(cmd, workspace)
        result["phase"] = "migration_apply"
        steps.append(result)
        if result["exit_code"] != 0:
            return {
                "success": False,
                "phase": "migration_apply",
                "command": cmd,
                "steps": steps,
                "output": result["output"],
            }

    for cmd in profile.get("lint_commands", []):
        result = _run_shell(cmd, workspace)
        result["phase"] = "lint"
        steps.append(result)
        if result["exit_code"] != 0:
            return {
                "success": False,
                "phase": "lint",
                "command": cmd,
                "steps": steps,
                "output": result["output"],
            }

    test_commands = profile.get("test_commands", ["pytest -q"])
    if not test_commands:
        return {"success": False, "phase": "test", "output": "No test commands configured.", "steps": steps}

    for cmd in test_commands:
        result = _run_shell(cmd, workspace)
        result["phase"] = "test"
        steps.append(result)
        if result["exit_code"] != 0:
            return {
                "success": False,
                "phase": "test",
                "command": cmd,
                "steps": steps,
                "output": result["output"],
                "migration": {
                    "applied": bool(apply_cmds),
                    "rolled_back": False,
                    "reapplied": False,
                },
            }

    rolled_back = False
    reapplied = False
    if apply_cmds and rollback_cmds:
        for cmd in rollback_cmds:
            result = _run_shell(cmd, workspace)
            result["phase"] = "migration_rollback"
            steps.append(result)
            if result["exit_code"] != 0:
                return {
                    "success": False,
                    "phase": "migration_rollback",
                    "command": cmd,
                    "steps": steps,
                    "output": result["output"],
                }
        rolled_back = True

        for cmd in apply_cmds:
            result = _run_shell(cmd, workspace)
            result["phase"] = "migration_reapply"
            steps.append(result)
            if result["exit_code"] != 0:
                return {
                    "success": False,
                    "phase": "migration_reapply",
                    "command": cmd,
                    "steps": steps,
                    "output": result["output"],
                }
        reapplied = True

        for cmd in test_commands:
            result = _run_shell(cmd, workspace)
            result["phase"] = "test_after_reapply"
            steps.append(result)
            if result["exit_code"] != 0:
                return {
                    "success": False,
                    "phase": "test_after_reapply",
                    "command": cmd,
                    "steps": steps,
                    "output": result["output"],
                    "migration": {
                        "applied": True,
                        "rolled_back": True,
                        "reapplied": True,
                    },
                }

    return {
        "success": True,
        "command": test_commands[-1],
        "output": steps[-1]["output"] if steps else "",
        "steps": steps,
        "migration": {
            "applied": bool(apply_cmds),
            "rolled_back": rolled_back,
            "reapplied": reapplied,
            "engine": migration.get("engine", ""),
        },
    }


def _load_optional_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _run_shell(command: str, workspace: str) -> dict:
    completed = subprocess.run(
        command,
        cwd=workspace,
        shell=True,
        text=True,
        capture_output=True,
    )
    return {
        "command": command,
        "exit_code": completed.returncode,
        "output": (completed.stdout + "\n" + completed.stderr).strip(),
    }


def _summarize_tool(tool_name: str, tool_input: dict[str, Any]) -> str:
    if tool_name == "Bash":
        description = str(tool_input.get("description") or "").strip()
        if description:
            return f"Bash を実行中: {description}"
        command = str(tool_input.get("command") or "").strip().replace("\n", " ")
        return f"Bash を実行中: {command[:120]}"
    if tool_name in {"Write", "Edit"}:
        file_path = str(tool_input.get("file_path") or "")
        name = Path(file_path).name if file_path else "unknown"
        verb = "書き込み中" if tool_name == "Write" else "編集中"
        return f"{name} を{verb}"
    if tool_name == "Task":
        description = str(tool_input.get("description") or "").strip()
        return f"サブタスクを実行中: {description or '詳細不明'}"
    return f"{tool_name} を実行中"


def _summarize_tool_result(tool_name: str, tool_response: Any) -> str:
    if tool_name == "Bash" and isinstance(tool_response, dict):
        exit_code = tool_response.get("exitCode", tool_response.get("exit_code"))
        return f"Bash が終了しました (exit={exit_code})"
    if tool_name in {"Write", "Edit"} and isinstance(tool_response, dict):
        file_path = str(tool_response.get("file_path") or tool_response.get("filePath") or "")
        name = Path(file_path).name if file_path else "unknown"
        return f"{name} の更新が完了しました"
    if tool_name == "Task":
        return "サブタスクが完了しました"
    return f"{tool_name} が完了しました"


def _summarize_tool_failure(tool_name: str, tool_response: Any) -> str:
    if tool_name == "Bash" and isinstance(tool_response, dict):
        exit_code = tool_response.get("exitCode", tool_response.get("exit_code"))
        return f"Bash が失敗しました (exit={exit_code})"
    return f"{tool_name} が失敗しました"


def _truncate_details(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _truncate_details(val) for key, val in value.items()}
    if isinstance(value, list):
        return [_truncate_details(item) for item in value[:10]]
    if isinstance(value, str):
        compact = _mask_secrets(value.replace("\r", "").strip())
        return compact[:1500]
    return value


def _build_failure_details(input_data: dict[str, Any]) -> dict[str, Any]:
    tool_response = input_data.get("tool_response")
    tool_input = input_data.get("tool_input")
    payload = {
        "tool_input": _truncate_details(tool_input),
        "tool_response": _truncate_details(tool_response),
    }
    # Failure hooks sometimes provide sparse tool_response. Keep a trimmed copy
    # of the full hook input so we can inspect alternate error fields later.
    payload["hook_input"] = _truncate_details(input_data)
    if isinstance(tool_response, dict):
        payload["error_excerpt"] = _truncate_details(
            tool_response.get("stderr")
            or tool_response.get("error")
            or tool_response.get("message")
            or tool_response.get("output")
            or ""
        )
    return payload


def _write_json(path: Path, payload: dict | AgentResult) -> None:
    if isinstance(payload, AgentResult):
        serializable = {
            "result": payload.result,
            "structured_output": payload.structured_output,
            "stderr": payload.stderr,
            "session_id": payload.session_id,
            "total_cost_usd": payload.total_cost_usd,
            "usage": payload.usage,
        }
    else:
        serializable = payload
    path.write_text(json.dumps(serializable, ensure_ascii=False, indent=2), encoding="utf-8")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _mask_secrets(text: str) -> str:
    masked = text
    patterns = [
        (r"(?i)(authorization\s*[:=]\s*)(.+)", r"\1[REDACTED]"),
        (r"(?i)(bearer\s+)[A-Za-z0-9._\-]+", r"\1[REDACTED]"),
        (r"(?i)(token\s*[:=]\s*)[^\s]+", r"\1[REDACTED]"),
        (r"(?i)(api[_-]?key\s*[:=]\s*)[^\s]+", r"\1[REDACTED]"),
    ]
    import re

    for pattern, replacement in patterns:
        masked = re.sub(pattern, replacement, masked)
    return masked


if __name__ == "__main__":
    raise SystemExit(main())

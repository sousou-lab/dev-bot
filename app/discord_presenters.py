from __future__ import annotations

from typing import Any


def format_status_message(
    *,
    thread_id: int,
    meta: dict[str, Any],
    issue: dict[str, Any],
    pr: dict[str, Any],
    summary: dict[str, Any],
    plan: dict[str, Any],
    test_plan: dict[str, Any],
    verification: dict[str, Any],
    review: dict[str, Any],
    pending_approval: dict[str, Any],
    planning_progress: dict[str, Any],
    current_activity: dict[str, Any],
    process: dict[str, Any] | None,
    runtime_active: bool,
) -> str:
    lines = [
        f"status: `{meta.get('status', 'unknown')}`",
        f"thread_id: `{thread_id}`",
        f"running: `{runtime_active}`",
        f"attempts: `{meta.get('attempt_count', 0)}`",
    ]
    if meta.get("github_repo"):
        lines.append(f"repo: `{meta.get('github_repo')}`")
    if summary:
        lines.append(f"goal: {summary.get('goal', '(no goal)')}")
    if plan:
        lines.append(f"plan: `{len(plan.get('implementation_steps', []))}` steps")
    if test_plan:
        lines.append(f"test_plan: `{len(test_plan.get('cases', []))}` cases")
    if isinstance(planning_progress, dict) and planning_progress:
        current = int(planning_progress.get("current", 0))
        total = int(planning_progress.get("total", 0))
        phase = str(planning_progress.get("phase", ""))
        if total > 0:
            lines.append(f"planning_progress: `{phase} {current}/{total}`")
        elif phase:
            lines.append(f"planning_progress: `{phase}`")
    if isinstance(current_activity, dict) and current_activity:
        phase = str(current_activity.get("phase", "")).strip()
        summary_text = str(current_activity.get("summary", "")).strip()
        status_text = str(current_activity.get("status", "")).strip()
        timestamp = str(current_activity.get("timestamp", "")).strip()
        if phase or summary_text:
            lines.append(f"activity: `{phase or 'unknown'}` `{status_text or 'unknown'}`")
        if summary_text:
            lines.append(f"activity_summary: {summary_text}")
        if timestamp:
            lines.append(f"activity_updated_at: `{timestamp}`")
    if issue:
        lines.append(f"issue: [#{issue.get('number')}]({issue.get('url')})")
    if pr:
        lines.append(f"pr: [#{pr.get('number')}]({pr.get('url')})")
    if verification:
        lines.append(f"verification: `{verification.get('status', 'unknown')}`")
    if review:
        lines.append(f"review: `{review.get('decision', 'unknown')}`")
    if isinstance(pending_approval, dict) and pending_approval.get("status") == "pending":
        lines.append(
            "pending_approval: "
            f"`{pending_approval.get('tool_name', 'unknown')}` "
            f"- {pending_approval.get('input_text', '')}"
        )
    if process:
        lines.append(f"process: pid=`{process.get('pid')}` pgid=`{process.get('pgid')}`")
    return "\n".join(lines)


def format_why_failed_message(
    *,
    last_failure: dict[str, Any],
    verification: dict[str, Any],
    final_result: dict[str, Any],
) -> str:
    lines = ["直近の失敗要約"]
    if isinstance(last_failure, dict) and last_failure:
        if last_failure.get("stage"):
            lines.append(f"- stage: `{last_failure.get('stage')}`")
        if last_failure.get("message"):
            lines.append(f"- message: {last_failure.get('message')}")
        details = last_failure.get("details")
        if isinstance(details, dict):
            repo = str(details.get("repo", "")).strip()
            if repo:
                lines.append(f"- repo: `{repo}`")
            planning_progress = details.get("planning_progress")
            if isinstance(planning_progress, dict) and planning_progress:
                phase = str(planning_progress.get("phase", "")).strip()
                current = int(planning_progress.get("current", 0))
                total = int(planning_progress.get("total", 0))
                acceptance = str(planning_progress.get("acceptance_criterion", "")).strip()
                last_session_id = str(planning_progress.get("last_session_id", "")).strip()
                if total > 0:
                    lines.append(f"- planning_progress: `{phase} {current}/{total}`")
                elif phase:
                    lines.append(f"- planning_progress: `{phase}`")
                if acceptance:
                    lines.append(f"- acceptance_criterion: {acceptance}")
                if last_session_id:
                    lines.append(f"- planning_session: `{last_session_id}`")
            debug_artifacts = details.get("debug_artifacts")
            if isinstance(debug_artifacts, list) and debug_artifacts:
                lines.append(f"- debug_artifacts: `{len(debug_artifacts)}` files")
                lines.append(f"- latest_debug_artifact: `{debug_artifacts[-1]}`")
            traceback_artifact = str(details.get("traceback_artifact", "")).strip()
            if traceback_artifact:
                lines.append(f"- traceback_artifact: `{traceback_artifact}`")
        stderr_lines = last_failure.get("stderr")
        if isinstance(stderr_lines, list):
            for item in stderr_lines[-3:]:
                snippet = str(item).strip()
                if snippet:
                    lines.append(f"- stderr: {snippet[:400]}")
    if isinstance(verification, dict) and verification:
        lines.append(f"- status: `{verification.get('status', 'unknown')}`")
        lines.append(f"- failure_type: `{verification.get('failure_type', 'unknown')}`")
        for note in verification.get("notes", [])[:5]:
            lines.append(f"- note: {note}")
    if isinstance(final_result, dict) and final_result and not final_result.get("success", True):
        lines.append(f"- final_failure_type: `{final_result.get('failure_type', 'unknown')}`")
    if len(lines) == 1:
        lines.append("- 直近の失敗情報は見つかりませんでした。")
    return "\n".join(lines)


def format_budget_message(*, attempt_count: int, verification: dict[str, Any], final_result: dict[str, Any]) -> str:
    lines = [
        f"attempts: `{attempt_count}`",
        f"verification_status: `{verification.get('status', 'unknown') if isinstance(verification, dict) else 'unknown'}`",
    ]
    if isinstance(final_result, dict) and final_result:
        lines.append(f"success: `{final_result.get('success', False)}`")
    return "\n".join(lines)


def format_plan_message(repo: str, plan: dict[str, Any], test_plan: dict[str, Any]) -> str:
    scope = "\n".join(f"- {item}" for item in plan.get("scope", [])[:6]) or "- なし"
    steps = "\n".join(f"- {item}" for item in plan.get("implementation_steps", [])[:6]) or "- なし"
    risks = "\n".join(f"- {item}" for item in plan.get("risks", [])[:4]) or "- なし"
    test_cases = (
        "\n".join(
            f"- {case.get('id', 'TC')} {case.get('name', '')} [{case.get('category', '')}/{case.get('priority', '')}]"
            for case in test_plan.get("cases", [])[:6]
            if isinstance(case, dict)
        )
        or "- なし"
    )
    return (
        "plan.json / test_plan.json を生成しました。\n"
        f"- Repo: `{repo}`\n"
        f"- Goal: {plan.get('goal', '(no goal)')}\n\n"
        "Scope\n"
        f"{scope}\n\n"
        "Implementation steps\n"
        f"{steps}\n\n"
        "Test cases\n"
        f"{test_cases}\n\n"
        "Risks\n"
        f"{risks}\n\n"
        "続けて実装 run を開始します。"
    )

from __future__ import annotations

import asyncio
from typing import Any

from app.issue_draft import build_issue_body, build_issue_title
from app.orchestrator import Orchestrator, WorkItem
from app.state_store import FileStateStore


async def ensure_issue_and_enqueue(
    *,
    thread_id: int,
    repo_full_name: str,
    state_store: FileStateStore,
    github_client: Any,
    orchestrator: Orchestrator,
    thread_url: str = "",
) -> dict[str, Any]:
    issue = await ensure_issue_for_thread(
        thread_id=thread_id,
        repo_full_name=repo_full_name,
        state_store=state_store,
        github_client=github_client,
        thread_url=thread_url,
    )
    workspace_key = state_store.bind_issue(thread_id, repo_full_name, int(issue["number"]))
    started = await enqueue_issue_run(
        thread_id=thread_id,
        repo_full_name=repo_full_name,
        issue=issue,
        issue_key=workspace_key,
        orchestrator=orchestrator,
    )
    if not started:
        raise RuntimeError("パイプラインの起動に失敗しました。")
    return issue


async def ensure_issue_for_thread(
    *,
    thread_id: int,
    repo_full_name: str,
    state_store: FileStateStore,
    github_client: Any,
    thread_url: str = "",
) -> dict[str, Any]:
    summary = state_store.load_artifact(thread_id, "requirement_summary.json")
    plan = state_store.load_artifact(thread_id, "plan.json")
    test_plan = state_store.load_artifact(thread_id, "test_plan.json")
    if (
        not isinstance(summary, dict)
        or not isinstance(plan, dict)
        or not isinstance(test_plan, dict)
        or not plan
        or not test_plan
    ):
        raise ValueError("先に `/plan repo:owner/repo` を実行してください。")

    issue = state_store.load_artifact(thread_id, "issue.json")
    if not isinstance(issue, dict) or not issue:
        title = build_issue_title(summary)
        body = build_issue_body(summary, thread_url)
        try:
            created = await asyncio.to_thread(
                github_client.create_issue,
                repo_full_name=repo_full_name,
                title=title,
                body=body,
            )
        except Exception as exc:
            raise RuntimeError(f"Issue 作成に失敗しました: `{exc}`") from exc
        issue = {
            "repo_full_name": created.repo_full_name,
            "number": created.number,
            "title": created.title,
            "body": created.body,
            "url": created.url,
        }
        state_store.write_artifact(thread_id, "issue.json", issue)
    return issue


async def enqueue_issue_run(
    *,
    thread_id: int,
    repo_full_name: str,
    issue: dict[str, Any],
    issue_key: str,
    orchestrator: Orchestrator,
) -> bool:
    started = await orchestrator.enqueue(
        WorkItem(
            thread_id=thread_id,
            repo_full_name=repo_full_name,
            issue=issue,
            issue_key=issue_key,
            workspace_key=issue_key,
        )
    )
    return started

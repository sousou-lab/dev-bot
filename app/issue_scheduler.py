from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from app.logging_setup import get_logger
from app.orchestrator import Orchestrator, WorkItem
from app.run_request import enqueue_issue_run
from app.state_store import FileStateStore

logger = get_logger(__name__)


class IssueScheduler:
    def __init__(
        self,
        *,
        state_store: FileStateStore,
        github_client: Any,
        orchestrator: Orchestrator,
        process_registry: Any,
        settings: Any,
        run_blocking: Callable[..., Awaitable[Any]],
        ensure_issue_thread_binding: Callable[[str], Awaitable[int]],
        process_merging_issue: Callable[..., Awaitable[None]],
        reconcile_runtime_state: Callable[[str | int, int], None],
        restore_pending_approval: Callable[[int], Awaitable[None]],
    ) -> None:
        self.state_store = state_store
        self.github_client = github_client
        self.orchestrator = orchestrator
        self.process_registry = process_registry
        self.settings = settings
        self._run_blocking = run_blocking
        self._ensure_issue_thread_binding = ensure_issue_thread_binding
        self._process_merging_issue = process_merging_issue
        self._reconcile_runtime_state = reconcile_runtime_state
        self._restore_pending_approval = restore_pending_approval
        self._scheduler_task: asyncio.Task[None] | None = None
        self._scheduler_tick_lock = asyncio.Lock()

    def ensure_started(self) -> None:
        if self._scheduler_task and not self._scheduler_task.done():
            return
        self._scheduler_task = asyncio.create_task(self._scheduler_loop())

    async def scheduler_tick(self) -> None:
        async with self._scheduler_tick_lock:
            metas = await self._run_blocking(self.sync_project_board_state)
            for meta in metas:
                issue_key = str(meta.get("issue_key", "")).strip()
                thread_id_text = str(meta.get("thread_id", "")).strip()
                repo_full_name = str(meta.get("github_repo", "")).strip()
                issue_number_text = str(meta.get("issue_number", "")).strip()
                status = str(meta.get("status", "")).strip()
                if not issue_key or not repo_full_name or not issue_number_text:
                    continue
                thread_id = int(thread_id_text) if thread_id_text else 0
                issue_number = int(issue_number_text)
                if thread_id <= 0 and status not in {"Done", "Cancelled"}:
                    thread_id = await self._ensure_issue_thread_binding(issue_key)
                if status in {"Ready", "Rework"}:
                    await self._dispatch_issue_if_ready(
                        thread_id=thread_id,
                        issue_key=issue_key,
                        repo_full_name=repo_full_name,
                        issue_number=issue_number,
                        expected_state=status,
                    )
                    continue
                if status == "In Progress":
                    self._reconcile_runtime_state(issue_key if thread_id <= 0 else thread_id, thread_id)
                    continue
                if status == "Merging":
                    await self._process_merging_issue(
                        issue_key=issue_key,
                        thread_id=thread_id,
                        repo_full_name=repo_full_name,
                        issue_number=issue_number,
                    )

    async def restore_pending_runs(self) -> None:
        items: list[WorkItem] = []
        metas = self.state_store.list_runs_by_status({"Ready", "Rework", "In Progress", "Merging"})
        for meta in metas:
            issue_key = str(meta.get("issue_key", ""))
            thread_id_text = str(meta.get("thread_id", "")).strip()
            if not issue_key or not thread_id_text:
                continue
            thread_id = int(thread_id_text)
            issue = self.state_store.load_artifact(issue_key, "issue.json")
            repo_full_name = str(meta.get("github_repo", ""))
            runtime_status = str(meta.get("runtime_status", "")).strip()
            if not isinstance(issue, dict) or not issue or not repo_full_name:
                continue
            if runtime_status == "awaiting_high_risk_approval":
                await self._restore_pending_approval(thread_id)
                continue
            if str(meta.get("status")) == "In Progress":
                if self.process_registry.is_active(issue_key):
                    continue
                self.state_store.update_status(issue_key, "Rework")
                self.state_store.update_meta(issue_key, runtime_status="")
                issue_number = int(str(meta.get("issue_number", "0")).strip() or 0)
                if repo_full_name and issue_number:
                    try:
                        await self._run_blocking(
                            self.github_client.update_issue_state, repo_full_name, issue_number, "Rework"
                        )
                    except Exception as exc:
                        logger.warning("restore: failed to update project state for %s: %s", issue_key, exc)
                continue
            if str(meta.get("status")) not in {"Ready", "Rework"}:
                continue
            issue_number = int(str(meta.get("issue_number", "0")).strip() or 0)
            if self._project_enabled() and repo_full_name and issue_number:
                gate = await self._run_blocking(self.scheduler_gate_for_issue, repo_full_name, issue_number, issue_key)
                if gate.get("state") not in {"Ready", "Rework"} or gate.get("plan") != "Approved":
                    continue
            items.append(
                WorkItem(
                    thread_id=thread_id,
                    repo_full_name=repo_full_name,
                    issue=issue,
                    issue_key=issue_key,
                    workspace_key=f"{repo_full_name}#{issue.get('number')}",
                )
            )
        if items:
            await self.orchestrator.restore(items)

    def sync_project_board_state(self) -> list[dict[str, Any]]:
        try:
            project_issues = self.github_client.list_project_issues()
        except Exception as exc:
            logger.warning("scheduler project sync failed: %s", exc)
            return []

        if not project_issues:
            logger.warning("scheduler project sync returned no items; skipping scheduler actions")
            return []

        synced_issue_keys: list[str] = []
        for project_issue in project_issues:
            repo_full_name = str(project_issue.get("repo_full_name", "")).strip()
            issue_number = int(project_issue.get("number", 0) or 0)
            state = str(project_issue.get("state", "")).strip()
            plan = str(project_issue.get("plan", "")).strip()
            if not repo_full_name or issue_number <= 0:
                continue
            issue_key = f"{repo_full_name}#{issue_number}"
            synced_issue_keys.append(issue_key)
            issue_meta = self.state_store.load_issue_meta(issue_key)
            if not issue_meta:
                self.state_store.create_issue_record(issue_key, status=state or "Backlog")
                issue_meta = self.state_store.load_issue_meta(issue_key)
            resolved_state = self._resolve_synced_state(
                issue_key=issue_key,
                project_state=state,
                issue_meta=issue_meta,
            )
            self.state_store.update_issue_meta(
                issue_key,
                status=resolved_state,
                plan_state=plan,
                github_repo=repo_full_name,
                issue_number=str(issue_number),
            )
            self.state_store.write_artifact(
                issue_key,
                "issue.json",
                {
                    "repo_full_name": repo_full_name,
                    "number": issue_number,
                    "title": str(project_issue.get("title", "") or ""),
                    "body": str(project_issue.get("body", "") or ""),
                    "url": str(project_issue.get("url", "") or ""),
                    "state": str(project_issue.get("issue_state", "") or ""),
                },
            )
        return [self.state_store.load_issue_meta(issue_key) for issue_key in synced_issue_keys]

    def _resolve_synced_state(self, *, issue_key: str, project_state: str, issue_meta: dict[str, Any]) -> str:
        local_state = str(issue_meta.get("status", "")).strip()
        synced_state = project_state or local_state
        if synced_state != "In Progress" or local_state != "Rework":
            return synced_state

        thread_id = int(str(issue_meta.get("thread_id", "")).strip() or 0)
        has_process = self.process_registry.is_active(issue_key) or (
            thread_id > 0 and self.process_registry.is_active(thread_id)
        )
        is_active = has_process or (thread_id > 0 and self.orchestrator.is_running(thread_id)) or (
            thread_id > 0 and self.orchestrator.is_queued(thread_id)
        )
        if is_active:
            return synced_state

        logger.info("scheduler sync: keeping local Rework for %s because remote In Progress looks stale", issue_key)
        return local_state

    def scheduler_gate_for_issue(self, repo_full_name: str, issue_number: int, issue_key: str) -> dict[str, str]:
        try:
            gate = self.github_client.get_issue_project_fields(repo_full_name, issue_number)
        except Exception as exc:
            logger.warning("scheduler gate lookup failed for %s: %s", issue_key, exc)
            return {}
        if gate.get("state") and gate.get("plan"):
            return gate
        logger.warning("scheduler gate incomplete for %s: %s", issue_key, gate)
        return {}

    async def _scheduler_loop(self) -> None:
        interval = max(1, int(getattr(self.settings, "scheduler_poll_interval_seconds", 15)))
        while True:
            try:
                await self.scheduler_tick()
            except Exception as exc:
                logger.warning("scheduler tick failed: %s", exc)
            await asyncio.sleep(interval)

    async def _dispatch_issue_if_ready(
        self,
        *,
        thread_id: int,
        issue_key: str,
        repo_full_name: str,
        issue_number: int,
        expected_state: str,
    ) -> None:
        if thread_id <= 0:
            thread_id = await self._ensure_issue_thread_binding(issue_key)
            if thread_id <= 0:
                return
        if self.orchestrator.is_running(thread_id) or self.orchestrator.is_queued(thread_id):
            return
        if self.process_registry.is_active(issue_key):
            return
        if not self.has_planning_artifacts(thread_id):
            logger.info("scheduler skip: planning artifacts missing for %s", issue_key)
            return
        if self._project_enabled():
            gate = await self._run_blocking(self.scheduler_gate_for_issue, repo_full_name, issue_number, issue_key)
            if gate.get("state") != expected_state or gate.get("plan") != "Approved":
                return
        issue = self.state_store.load_artifact(issue_key, "issue.json")
        if not isinstance(issue, dict) or not issue:
            issue = self.state_store.load_artifact(thread_id, "issue.json")
        if not isinstance(issue, dict) or not issue:
            return
        issue_state = str(issue.get("state", "")).strip().upper()
        if issue_state == "CLOSED":
            logger.info("scheduler skip: issue is closed for %s", issue_key)
            return
        await enqueue_issue_run(
            thread_id=thread_id,
            repo_full_name=repo_full_name,
            issue=issue,
            issue_key=issue_key,
            orchestrator=self.orchestrator,
        )

    def has_planning_artifacts(self, thread_id: int) -> bool:
        summary = self.state_store.load_artifact(thread_id, "requirement_summary.json")
        plan = self.state_store.load_artifact(thread_id, "plan.json")
        test_plan = self.state_store.load_artifact(thread_id, "test_plan.json")
        return (
            isinstance(summary, dict)
            and bool(summary)
            and isinstance(plan, dict)
            and bool(plan)
            and isinstance(test_plan, dict)
            and bool(test_plan)
        )

    def _project_enabled(self) -> bool:
        return bool(str(getattr(self.settings, "github_project_id", "")).strip())

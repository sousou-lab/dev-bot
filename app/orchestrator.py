from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from app.state_store import FileStateStore


@dataclass(frozen=True)
class WorkItem:
    thread_id: int | None
    repo_full_name: str
    issue: dict
    issue_key: str = ""
    workspace_key: str = ""


class Orchestrator:
    def __init__(
        self,
        state_store: FileStateStore,
        executor: Callable[[WorkItem], Awaitable[None]],
        max_concurrency: int = 5,
    ) -> None:
        self.state_store = state_store
        self.executor = executor
        self.max_concurrency = max_concurrency
        self._queue: asyncio.Queue[WorkItem] = asyncio.Queue()
        self._running: dict[str, asyncio.Task[None]] = {}
        self._running_keys: set[str] = set()
        self._queued_thread_ids: set[int] = set()
        self._queued_keys: set[str] = set()
        self._thread_to_key: dict[int, str] = {}
        self._dispatcher_task: asyncio.Task[None] | None = None

    def ensure_started(self) -> None:
        if self._dispatcher_task and not self._dispatcher_task.done():
            return
        self._dispatcher_task = asyncio.create_task(self._dispatch_loop())

    async def enqueue(self, item: WorkItem) -> bool:
        item_key = self._item_key(item)
        if (
            (item.thread_id is not None and item.thread_id in self._queued_thread_ids)
            or item_key in self._running_keys
            or item_key in self._queued_keys
        ):
            return False
        if item.thread_id is not None and self._is_key_running(item_key):
            return False
        if item.thread_id is not None:
            self.state_store.update_meta(item.issue_key or item.thread_id, runtime_status="queued")
            self._queued_thread_ids.add(item.thread_id)
            self._thread_to_key[item.thread_id] = item_key
        else:
            self.state_store.update_meta(item.issue_key or item.workspace_key, runtime_status="queued")
        await self._queue.put(item)
        self._queued_keys.add(item_key)
        self.ensure_started()
        return True

    def is_running(self, thread_id: int) -> bool:
        item_key = self._thread_to_key.get(thread_id)
        return bool(item_key and self._is_key_running(item_key))

    def is_queued(self, thread_id: int) -> bool:
        return thread_id in self._queued_thread_ids

    def pending_count(self) -> int:
        return self._queue.qsize()

    def active_count(self) -> int:
        self._cleanup_finished()
        return len(self._running)

    async def drain(self) -> None:
        """Gracefully stop: cancel dispatcher, drain queue, cancel running tasks."""
        if self._dispatcher_task and not self._dispatcher_task.done():
            self._dispatcher_task.cancel()
            try:
                await self._dispatcher_task
            except asyncio.CancelledError:
                pass

        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        self._queued_thread_ids.clear()
        self._queued_keys.clear()

        tasks = list(self._running.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._running.clear()
        self._running_keys.clear()

    async def _dispatch_loop(self) -> None:
        while True:
            if len(self._running) >= self.max_concurrency:
                await asyncio.sleep(1)
                self._cleanup_finished()
                continue
            item = await self._queue.get()
            item_key = self._item_key(item)
            if item.thread_id is not None:
                self._queued_thread_ids.discard(item.thread_id)
                self._thread_to_key[item.thread_id] = item_key
            self._queued_keys.discard(item_key)
            self._cleanup_finished()
            self.state_store.update_meta(
                item.issue_key or item.workspace_key or item.thread_id or item_key, runtime_status="running"
            )
            task = asyncio.create_task(self._run_item(item))
            self._running[item_key] = task
            self._running_keys.add(item_key)

    async def _run_item(self, item: WorkItem) -> None:
        item_key = self._item_key(item)
        status_target = (
            item.issue_key or item.workspace_key or (item.thread_id if item.thread_id is not None else item_key)
        )
        try:
            await self.executor(item)
        except Exception as exc:
            self.state_store.record_failure(
                status_target,
                stage="run_execution",
                message=str(exc),
                details={"repo": item.repo_full_name, "issue_key": item_key},
            )
            self.state_store.update_meta(status_target, runtime_status="failed")
            self.state_store.update_status(status_target, "Rework" if item.issue_key else "failed")
        finally:
            self.state_store.update_meta(status_target, runtime_status="")
            self._running.pop(item_key, None)
            self._running_keys.discard(item_key)

    def _cleanup_finished(self) -> None:
        stale = [item_key for item_key, task in self._running.items() if task.done()]
        for item_key in stale:
            self._running.pop(item_key, None)

    def _item_key(self, item: WorkItem) -> str:
        return (
            item.issue_key or item.workspace_key or f"{item.repo_full_name}#{item.issue.get('number', item.thread_id)}"
        )

    def _is_key_running(self, item_key: str) -> bool:
        task = self._running.get(item_key)
        return bool(task and not task.done())

    async def restore(self, items: list[WorkItem]) -> None:
        for item in items:
            await self.enqueue(item)

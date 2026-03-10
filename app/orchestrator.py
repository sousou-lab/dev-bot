from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from app.state_store import FileStateStore


@dataclass(frozen=True)
class WorkItem:
    thread_id: int
    repo_full_name: str
    issue: dict
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
        self._running: dict[int, asyncio.Task[None]] = {}
        self._running_keys: set[str] = set()
        self._queued_thread_ids: set[int] = set()
        self._queued_keys: set[str] = set()
        self._dispatcher_task: asyncio.Task[None] | None = None

    def ensure_started(self) -> None:
        if self._dispatcher_task and not self._dispatcher_task.done():
            return
        self._dispatcher_task = asyncio.create_task(self._dispatch_loop())

    async def enqueue(self, item: WorkItem) -> bool:
        item_key = self._item_key(item)
        if (
            item.thread_id in self._running
            or item.thread_id in self._queued_thread_ids
            or item_key in self._running_keys
            or item_key in self._queued_keys
        ):
            return False
        self.state_store.update_status(item.thread_id, "queued")
        await self._queue.put(item)
        self._queued_thread_ids.add(item.thread_id)
        self._queued_keys.add(item_key)
        self.ensure_started()
        return True

    def is_running(self, thread_id: int) -> bool:
        task = self._running.get(thread_id)
        return bool(task and not task.done())

    def is_queued(self, thread_id: int) -> bool:
        return thread_id in self._queued_thread_ids

    def pending_count(self) -> int:
        return self._queue.qsize()

    def active_count(self) -> int:
        self._cleanup_finished()
        return len(self._running)

    async def drain(self) -> None:
        """Gracefully stop: cancel dispatcher, drain queue, cancel running tasks."""
        # Stop the dispatcher loop
        if self._dispatcher_task and not self._dispatcher_task.done():
            self._dispatcher_task.cancel()
            try:
                await self._dispatcher_task
            except asyncio.CancelledError:
                pass

        # Drain the queue
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        self._queued_thread_ids.clear()
        self._queued_keys.clear()

        # Cancel running tasks
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
            self._queued_thread_ids.discard(item.thread_id)
            self._queued_keys.discard(self._item_key(item))
            self._cleanup_finished()
            task = asyncio.create_task(self._run_item(item))
            self._running[item.thread_id] = task
            self._running_keys.add(self._item_key(item))

    async def _run_item(self, item: WorkItem) -> None:
        try:
            await self.executor(item)
        except Exception as exc:
            self.state_store.record_failure(
                item.thread_id,
                stage="run_execution",
                message=str(exc),
                details={"repo": item.repo_full_name},
            )
            self.state_store.update_status(item.thread_id, "failed")
        finally:
            self._running.pop(item.thread_id, None)
            self._running_keys.discard(self._item_key(item))

    def _cleanup_finished(self) -> None:
        stale = [thread_id for thread_id, task in self._running.items() if task.done()]
        for thread_id in stale:
            self._running.pop(thread_id, None)

    def _item_key(self, item: WorkItem) -> str:
        return item.workspace_key or f"{item.repo_full_name}#{item.issue.get('number', item.thread_id)}"

    async def restore(self, items: list[WorkItem]) -> None:
        for item in items:
            await self.enqueue(item)

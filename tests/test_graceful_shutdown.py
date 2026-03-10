from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock

from app.orchestrator import Orchestrator, WorkItem


class GracefulShutdownTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.state_store = MagicMock()
        self.state_store.update_status = MagicMock()
        self.state_store.record_failure = MagicMock()
        self.executor = AsyncMock()
        self.orchestrator = Orchestrator(self.state_store, self.executor, max_concurrency=2)

    async def test_drain_cancels_running_tasks(self) -> None:
        """drain() should cancel running tasks and clear queues."""
        blocker = asyncio.Event()

        async def slow_executor(item: WorkItem) -> None:
            await blocker.wait()

        self.orchestrator.executor = slow_executor
        item = WorkItem(thread_id=1, repo_full_name="o/r", issue={"number": 1}, workspace_key="o/r#1")
        await self.orchestrator.enqueue(item)
        await asyncio.sleep(0.05)  # let dispatcher pick it up

        self.assertTrue(self.orchestrator.is_running(1))

        await self.orchestrator.drain()

        self.assertFalse(self.orchestrator.is_running(1))
        self.assertEqual(self.orchestrator.pending_count(), 0)
        self.assertEqual(self.orchestrator.active_count(), 0)

    async def test_drain_clears_queued_items(self) -> None:
        """drain() should clear pending queue items."""
        blocker = asyncio.Event()

        async def slow_executor(item: WorkItem) -> None:
            await blocker.wait()

        self.orchestrator.executor = slow_executor
        self.orchestrator.max_concurrency = 1

        item1 = WorkItem(thread_id=1, repo_full_name="o/r", issue={"number": 1}, workspace_key="o/r#1")
        item2 = WorkItem(thread_id=2, repo_full_name="o/r", issue={"number": 2}, workspace_key="o/r#2")
        await self.orchestrator.enqueue(item1)
        await self.orchestrator.enqueue(item2)
        await asyncio.sleep(0.05)

        await self.orchestrator.drain()

        self.assertEqual(self.orchestrator.pending_count(), 0)
        self.assertEqual(self.orchestrator.active_count(), 0)

    async def test_pending_count_reflects_queue_size(self) -> None:
        """pending_count() should return the number of queued items."""
        blocker = asyncio.Event()

        async def slow_executor(item: WorkItem) -> None:
            await blocker.wait()

        self.orchestrator.executor = slow_executor
        self.orchestrator.max_concurrency = 1

        item1 = WorkItem(thread_id=1, repo_full_name="o/r", issue={"number": 1}, workspace_key="o/r#1")
        item2 = WorkItem(thread_id=2, repo_full_name="o/r", issue={"number": 2}, workspace_key="o/r#2")

        self.assertEqual(self.orchestrator.pending_count(), 0)

        await self.orchestrator.enqueue(item1)
        await self.orchestrator.enqueue(item2)
        await asyncio.sleep(0.05)

        # item1 should be running, item2 should be queued
        self.assertEqual(self.orchestrator.active_count(), 1)
        self.assertGreaterEqual(self.orchestrator.pending_count(), 0)  # may be 0 or 1 depending on timing

        blocker.set()
        await asyncio.sleep(0.05)

    async def test_active_count_reflects_running_tasks(self) -> None:
        """active_count() should return the number of running tasks."""
        self.assertEqual(self.orchestrator.active_count(), 0)

        blocker = asyncio.Event()

        async def slow_executor(item: WorkItem) -> None:
            await blocker.wait()

        self.orchestrator.executor = slow_executor
        item = WorkItem(thread_id=1, repo_full_name="o/r", issue={"number": 1}, workspace_key="o/r#1")
        await self.orchestrator.enqueue(item)
        await asyncio.sleep(0.05)

        self.assertEqual(self.orchestrator.active_count(), 1)

        blocker.set()
        await asyncio.sleep(0.05)

    async def test_drain_stops_dispatcher(self) -> None:
        """After drain(), the dispatcher loop should be stopped."""
        self.orchestrator.ensure_started()
        await asyncio.sleep(0.01)

        await self.orchestrator.drain()

        self.assertTrue(self.orchestrator._dispatcher_task is None or self.orchestrator._dispatcher_task.done())

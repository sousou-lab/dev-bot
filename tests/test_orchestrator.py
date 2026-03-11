from __future__ import annotations

import asyncio
import tempfile
import unittest

from app.orchestrator import Orchestrator, WorkItem
from app.state_store import FileStateStore


class OrchestratorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.state_store = FileStateStore(self.tempdir.name)
        self.state_store.create_run(thread_id=1, parent_message_id=10, channel_id=20)
        self.state_store.create_run(thread_id=2, parent_message_id=11, channel_id=21)
        self.events: list[int] = []
        self.release = asyncio.Event()

        async def executor(item: WorkItem) -> None:
            self.events.append(item.thread_id)
            await self.release.wait()

        self.orchestrator = Orchestrator(self.state_store, executor=executor, max_concurrency=1)

    async def asyncTearDown(self) -> None:
        self.release.set()
        self.tempdir.cleanup()

    async def test_enqueue_sets_status_and_prevents_duplicates(self) -> None:
        item = WorkItem(thread_id=1, repo_full_name="owner/repo", issue={"number": 1})
        started = await self.orchestrator.enqueue(item)
        duplicate = await self.orchestrator.enqueue(item)

        self.assertTrue(started)
        self.assertFalse(duplicate)
        self.assertEqual("draft", self.state_store.load_meta(1)["status"])
        self.assertEqual("queued", self.state_store.load_meta(1)["runtime_status"])

    async def test_restore_requeues_items(self) -> None:
        await self.orchestrator.restore(
            [
                WorkItem(thread_id=1, repo_full_name="owner/repo", issue={"number": 1}),
                WorkItem(thread_id=2, repo_full_name="owner/repo", issue={"number": 2}),
            ]
        )
        await asyncio.sleep(0.1)
        self.assertEqual([1], self.events)
        self.release.set()
        for _ in range(20):
            if 2 in self.events:
                break
            await asyncio.sleep(0.05)
        self.assertIn(2, self.events)

    async def test_executor_exception_marks_thread_failed(self) -> None:
        async def failing_executor(item: WorkItem) -> None:
            raise RuntimeError(f"boom-{item.thread_id}")

        orchestrator = Orchestrator(self.state_store, executor=failing_executor, max_concurrency=1)
        await orchestrator.enqueue(WorkItem(thread_id=1, repo_full_name="owner/repo", issue={"number": 1}))
        await asyncio.sleep(0.1)

        self.assertEqual("failed", self.state_store.load_meta(1)["status"])
        failure = self.state_store.load_artifact(1, "last_failure.json")
        self.assertEqual("run_execution", failure["stage"])
        self.assertIn("boom-1", failure["message"])

    async def test_enqueue_rejects_duplicate_workspace_key(self) -> None:
        first = await self.orchestrator.enqueue(
            WorkItem(thread_id=1, repo_full_name="owner/repo", issue={"number": 1}, workspace_key="owner/repo#1")
        )
        second = await self.orchestrator.enqueue(
            WorkItem(thread_id=2, repo_full_name="owner/repo", issue={"number": 1}, workspace_key="owner/repo#1")
        )

        self.assertTrue(first)
        self.assertFalse(second)

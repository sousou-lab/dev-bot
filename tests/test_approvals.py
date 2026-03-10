from __future__ import annotations

import asyncio
import tempfile
import unittest

from app.approvals import ApprovalCoordinator, is_high_risk_command
from app.state_store import FileStateStore


class ApprovalCoordinatorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.state_store = FileStateStore(self.tempdir.name)
        self.state_store.create_run(thread_id=1, parent_message_id=10, channel_id=20)
        self.coordinator = ApprovalCoordinator(self.state_store)

    async def asyncTearDown(self) -> None:
        self.tempdir.cleanup()

    async def test_request_and_resolve_updates_artifact(self) -> None:
        self.coordinator.create_request(1, "run-1", "Bash", "terraform apply", "dangerous")
        waiter = asyncio.create_task(self.coordinator.wait_for_resolution(1))
        await asyncio.sleep(0)
        self.assertTrue(self.coordinator.has_pending_request(1))

        resolution = self.coordinator.resolve(1, approved=True, actor="tester")
        approved = await waiter

        self.assertEqual("resolved", resolution)
        self.assertTrue(approved)
        payload = self.state_store.load_artifact(1, "pending_approval.json")
        self.assertEqual("approved", payload["status"])
        self.assertEqual("tester", payload["resolved_by"])

    async def test_resolve_without_future_marks_persisted_only(self) -> None:
        self.coordinator.create_request(1, "run-1", "Bash", "terraform apply", "dangerous")
        restored = ApprovalCoordinator(self.state_store)

        resolution = restored.resolve(1, approved=False, actor="tester")

        self.assertEqual("persisted_only", resolution)
        payload = self.state_store.load_artifact(1, "pending_approval.json")
        self.assertEqual("rejected", payload["status"])

    async def test_high_risk_patterns(self) -> None:
        self.assertTrue(is_high_risk_command("terraform apply"))
        self.assertTrue(is_high_risk_command("ALEMBIC REVISION --autogenerate".lower()))
        self.assertFalse(is_high_risk_command("pytest -q"))

    async def test_request_timeout_marks_expired(self) -> None:
        self.coordinator.create_request(1, "run-1", "Bash", "terraform apply", "dangerous")

        approved = await self.coordinator.wait_for_resolution(1, timeout_seconds=0)

        self.assertFalse(approved)
        payload = self.state_store.load_artifact(1, "pending_approval.json")
        self.assertEqual("expired", payload["status"])

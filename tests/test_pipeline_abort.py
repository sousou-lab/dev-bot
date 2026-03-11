from __future__ import annotations

import tempfile
import unittest
from unittest.mock import MagicMock

from app.approvals import ApprovalCoordinator
from app.pipeline import DevelopmentPipeline
from app.process_registry import ProcessRegistry
from app.state_store import FileStateStore
from tests.helpers import make_test_settings


class PipelineAbortTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.state_store = FileStateStore(self.tmpdir.name)
        self.state_store.create_run(thread_id=321, parent_message_id=1, channel_id=2)
        self.state_store.bind_issue(321, "owner/repo", 42)
        self.state_store.update_issue_meta("owner/repo#42", github_repo="owner/repo", issue_number="42")
        self.github_client = MagicMock()
        self.process_registry = ProcessRegistry(self.tmpdir.name)
        self.pipeline = DevelopmentPipeline(
            settings=make_test_settings(state_dir=self.tmpdir.name),
            state_store=self.state_store,
            github_client=self.github_client,
            process_registry=self.process_registry,
            approval_coordinator=ApprovalCoordinator(self.state_store),
        )

        async def _run_blocking(func, /, *args, **kwargs):
            return func(*args, **kwargs)

        self.pipeline._run_blocking = _run_blocking  # type: ignore[method-assign]

    async def asyncTearDown(self) -> None:
        self.tmpdir.cleanup()

    async def test_abort_updates_bound_issue_state_to_blocked(self) -> None:
        self.process_registry.terminate = lambda target: True  # type: ignore[method-assign]

        await self.pipeline.abort(321)

        self.github_client.update_issue_state.assert_called_with("owner/repo", 42, "Blocked")
        meta = self.state_store.load_issue_meta("owner/repo#42")
        self.assertEqual("Blocked", meta["status"])

    async def test_abort_is_noop_when_no_live_process_exists(self) -> None:
        self.state_store.update_issue_meta("owner/repo#42", status="In Progress", runtime_status="running")
        self.process_registry.terminate = lambda target: False  # type: ignore[method-assign]

        stopped = await self.pipeline.abort(321)

        self.assertFalse(stopped)
        self.github_client.update_issue_state.assert_not_called()
        meta = self.state_store.load_issue_meta("owner/repo#42")
        self.assertEqual("In Progress", meta["status"])
        self.assertEqual("running", meta["runtime_status"])

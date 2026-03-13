from __future__ import annotations

import tempfile
import unittest
from unittest.mock import MagicMock

from app.issue_scheduler import IssueScheduler
from app.state_store import FileStateStore
from tests.helpers import make_test_settings


class IssueSchedulerPlanningArtifactsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.state_store = FileStateStore(self.tmpdir.name)
        self.scheduler = IssueScheduler(
            state_store=self.state_store,
            github_client=MagicMock(),
            orchestrator=MagicMock(),
            process_registry=MagicMock(),
            settings=make_test_settings(state_dir=self.tmpdir.name),
            run_blocking=MagicMock(),
            ensure_issue_thread_binding=MagicMock(),
            process_merging_issue=MagicMock(),
            reconcile_runtime_state=MagicMock(),
            restore_pending_approval=MagicMock(),
        )

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_has_planning_artifacts_requires_core_artifacts(self) -> None:
        thread_id = 123
        self.state_store.create_run(thread_id=thread_id, parent_message_id=1, channel_id=2)
        self.state_store.write_artifact(thread_id, "requirement_summary.json", {"goal": "ship"})
        self.state_store.write_artifact(thread_id, "plan.json", {"steps": ["one"]})

        self.assertFalse(self.scheduler.has_planning_artifacts(thread_id))

    def test_has_planning_artifacts_allows_missing_recommended_artifacts(self) -> None:
        thread_id = 456
        self.state_store.create_run(thread_id=thread_id, parent_message_id=1, channel_id=2)
        self.state_store.write_artifact(thread_id, "requirement_summary.json", {"goal": "ship"})
        self.state_store.write_artifact(thread_id, "plan.json", {"steps": ["one"]})
        self.state_store.write_artifact(thread_id, "test_plan.json", {"checks": ["tests"]})

        self.assertTrue(self.scheduler.has_planning_artifacts(thread_id))

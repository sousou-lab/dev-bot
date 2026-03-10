from __future__ import annotations

import tempfile
import unittest

from app.state_store import FileStateStore


class FileStateStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = FileStateStore(self.tempdir.name)
        self.store.create_run(thread_id=1, parent_message_id=10, channel_id=20)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_create_execution_run_updates_meta(self) -> None:
        run_id = self.store.create_execution_run(1)

        meta = self.store.load_meta(1)
        self.assertEqual(run_id, meta["current_run_id"])
        self.assertEqual(1, meta["attempt_count"])

    def test_write_execution_artifact_updates_thread_and_run_scope(self) -> None:
        run_id = self.store.create_execution_run(1)
        self.store.write_execution_artifact(1, "plan.json", {"goal": "x"}, run_id)

        self.assertEqual({"goal": "x"}, self.store.load_artifact(1, "plan.json"))
        self.assertEqual({"goal": "x"}, self.store.load_execution_artifact(1, "plan.json", run_id))

    def test_record_failure_writes_agent_and_last_failure(self) -> None:
        payload = self.store.record_failure(
            1,
            stage="plan_generation",
            message="Claude Agent SDK did not return valid JSON.",
            details={
                "repo": "owner/repo",
                "prompt_kind": "plan",
                "session_id": "sess_123",
                "raw_response": "Let me explore",
            },
            stderr=["line1", "line2"],
        )

        self.assertEqual("plan_generation", payload["stage"])
        self.assertEqual("sess_123", payload["details"]["session_id"])
        self.assertEqual(payload, self.store.load_artifact(1, "agent_failure.json"))
        self.assertEqual(payload, self.store.load_artifact(1, "last_failure.json"))

    def test_record_failure_masks_tokens_in_stderr(self) -> None:
        payload = self.store.record_failure(
            1,
            stage="plan_generation",
            message="failed",
            stderr=[
                "Git remote URL: https://x-access-token:secret123@github.com/owner/repo.git",
                "Authorization: Bearer topsecret",
            ],
        )

        self.assertIn("https://[REDACTED]@github.com/owner/repo.git", payload["stderr"][0])
        self.assertNotIn("secret123", payload["stderr"][0])
        self.assertIn("Authorization: [REDACTED]", payload["stderr"][1])

    def test_record_activity_updates_current_and_history(self) -> None:
        payload = self.store.record_activity(
            1,
            phase="workspace",
            summary="workspace ready",
            status="running",
            run_id="run_123",
            details={"path": "/tmp/workspace"},
        )

        self.assertEqual(payload, self.store.load_artifact(1, "current_activity.json"))
        history = self.store.load_artifact(1, "activity_history.json")
        self.assertEqual(1, len(history["items"]))
        self.assertEqual("workspace", history["items"][0]["phase"])

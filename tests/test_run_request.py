from __future__ import annotations

import tempfile
import unittest
from types import SimpleNamespace

from app.run_request import ensure_issue_and_enqueue
from app.state_store import FileStateStore
from tests.helpers import setup_planning_artifacts


class RecordingOrchestrator:
    def __init__(self, *, enqueue_result: bool = True) -> None:
        self.enqueue_result = enqueue_result
        self.items: list[object] = []

    async def enqueue(self, item: object) -> bool:
        self.items.append(item)
        return self.enqueue_result


class RunRequestTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.state_store = FileStateStore(self.tempdir.name)
        self.state_store.create_run(thread_id=100, parent_message_id=200, channel_id=300)
        setup_planning_artifacts(100, self.state_store)

    async def asyncTearDown(self) -> None:
        self.tempdir.cleanup()

    async def test_creates_issue_and_enqueues_bound_workspace_key(self) -> None:
        github_client = SimpleNamespace(
            create_issue=lambda **_: SimpleNamespace(
                repo_full_name="owner/repo",
                number=12,
                title="Issue title",
                body="Issue body",
                url="https://example.test/issues/12",
            )
        )
        orchestrator = RecordingOrchestrator()

        issue = await ensure_issue_and_enqueue(
            thread_id=100,
            repo_full_name="owner/repo",
            state_store=self.state_store,
            github_client=github_client,
            orchestrator=orchestrator,
            thread_url="https://test.local/channels/100",
        )

        self.assertEqual(12, issue["number"])
        self.assertEqual("owner/repo#12", self.state_store.load_meta(100)["issue_key"])
        self.assertEqual("owner/repo#12", orchestrator.items[0].workspace_key)

    async def test_reuses_bound_issue_without_creating_a_new_one(self) -> None:
        self.state_store.write_artifact(
            100,
            "issue.json",
            {
                "repo_full_name": "owner/repo",
                "number": 12,
                "title": "Existing issue",
                "body": "Existing body",
                "url": "https://example.test/issues/12",
            },
        )
        github_client = SimpleNamespace(create_issue=self.fail)
        orchestrator = RecordingOrchestrator()

        issue = await ensure_issue_and_enqueue(
            thread_id=100,
            repo_full_name="owner/repo",
            state_store=self.state_store,
            github_client=github_client,
            orchestrator=orchestrator,
        )

        self.assertEqual(12, issue["number"])
        self.assertEqual("owner/repo#12", orchestrator.items[0].workspace_key)
        self.assertEqual("owner/repo#12", self.state_store.load_meta(100)["issue_key"])

    async def test_raises_when_enqueue_is_rejected(self) -> None:
        self.state_store.write_artifact(
            100,
            "issue.json",
            {
                "repo_full_name": "owner/repo",
                "number": 12,
                "title": "Existing issue",
                "body": "Existing body",
                "url": "https://example.test/issues/12",
            },
        )
        orchestrator = RecordingOrchestrator(enqueue_result=False)

        with self.assertRaisesRegex(RuntimeError, "パイプラインの起動に失敗しました"):
            await ensure_issue_and_enqueue(
                thread_id=100,
                repo_full_name="owner/repo",
                state_store=self.state_store,
                github_client=SimpleNamespace(create_issue=self.fail),
                orchestrator=orchestrator,
            )

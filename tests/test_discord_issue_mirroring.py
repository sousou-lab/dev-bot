from __future__ import annotations

import tempfile
import unittest

from app.discord_adapter import DevBotClient
from app.state_store import FileStateStore
from tests.helpers import make_test_settings


class _FakeThread:
    def __init__(self, thread_id: int) -> None:
        self.id = thread_id
        self.messages: list[str] = []

    async def send(self, content: str) -> None:
        self.messages.append(content)


class _FakeStatusChannel:
    def __init__(self) -> None:
        self.created_threads: list[_FakeThread] = []

    async def create_thread(self, *, name: str, auto_archive_duration: int) -> _FakeThread:
        del name, auto_archive_duration
        thread = _FakeThread(99901)
        self.created_threads.append(thread)
        return thread


class DiscordIssueMirroringTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.state_store = FileStateStore(self.tmpdir.name)
        self.settings = make_test_settings(state_dir=self.tmpdir.name, discord_status_channel_id="67890")
        self.client = DevBotClient(settings=self.settings, state_store=self.state_store)
        self.status_channel = _FakeStatusChannel()
        self.client.get_channel = lambda channel_id: self.status_channel if channel_id == 67890 else None  # type: ignore[method-assign]

    async def asyncTearDown(self) -> None:
        self.tmpdir.cleanup()

    async def test_ensure_issue_thread_binding_creates_status_thread_and_binds_issue(self) -> None:
        issue_key = "owner/repo#42"
        self.state_store.create_issue_record(issue_key, status="Ready")
        self.state_store.update_issue_meta(
            issue_key, plan_state="Approved", github_repo="owner/repo", issue_number="42"
        )
        self.state_store.write_artifact(
            issue_key,
            "issue.json",
            {
                "repo_full_name": "owner/repo",
                "number": 42,
                "title": "Ship scheduler",
                "url": "https://github.com/owner/repo/issues/42",
            },
        )

        thread_id = await self.client._ensure_issue_thread_binding(issue_key)

        self.assertEqual(99901, thread_id)
        self.assertEqual("99901", self.state_store.thread_id_for_issue(issue_key))
        self.assertEqual(issue_key, self.state_store.issue_key_for_thread(99901))
        self.assertEqual(1, len(self.status_channel.created_threads))
        self.assertIn(
            "GitHub Issue を status mirror thread に同期しました。", self.status_channel.created_threads[0].messages[0]
        )
        summary = self.state_store.load_artifact(issue_key, "requirement_summary.json")
        self.assertEqual("Ship scheduler", summary["goal"])
        self.assertEqual(["Ship scheduler"], summary["acceptance_criteria"])
        conversation_path = self.state_store.entity_dir(issue_key) / "conversation.jsonl"
        self.assertTrue(conversation_path.exists())
        self.assertIn("GitHub issue から初期化した要件です。", conversation_path.read_text(encoding="utf-8"))

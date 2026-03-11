from __future__ import annotations

import tempfile
import unittest
from unittest.mock import MagicMock

from app.discord_adapter import DevBotClient
from app.state_store import FileStateStore
from tests.helpers import make_test_settings


class DiscordSchedulerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.state_store = FileStateStore(self.tmpdir.name)
        self.settings = make_test_settings(state_dir=self.tmpdir.name)
        self.client = DevBotClient(settings=self.settings, state_store=self.state_store)

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_sync_project_board_state_creates_issue_records_from_project_items(self) -> None:
        self.client.github_client = MagicMock()
        self.client.github_client.list_project_issues.return_value = [
            {
                "repo_full_name": "owner/repo",
                "number": 42,
                "title": "Ship scheduler",
                "body": "body",
                "url": "https://github.com/owner/repo/issues/42",
                "issue_state": "OPEN",
                "state": "Ready",
                "plan": "Approved",
            }
        ]

        metas = self.client._sync_project_board_state()

        self.assertEqual(1, len(metas))
        meta = self.state_store.load_issue_meta("owner/repo#42")
        self.assertEqual("Ready", meta["status"])
        self.assertEqual("Approved", meta["plan_state"])
        issue = self.state_store.load_artifact("owner/repo#42", "issue.json")
        self.assertEqual("Ship scheduler", issue["title"])


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
        thread = _FakeThread(91234)
        self.created_threads.append(thread)
        return thread


class DiscordSchedulerAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.state_store = FileStateStore(self.tmpdir.name)
        self.settings = make_test_settings(state_dir=self.tmpdir.name)
        self.client = DevBotClient(settings=self.settings, state_store=self.state_store)
        self.status_channel = _FakeStatusChannel()
        self.client.get_channel = lambda channel_id: self.status_channel if channel_id == 67890 else None  # type: ignore[method-assign]

    async def asyncTearDown(self) -> None:
        self.tmpdir.cleanup()

    async def test_scheduler_tick_creates_status_thread_for_unbound_issue(self) -> None:
        self.client.github_client = MagicMock()
        self.client.github_client.list_project_issues.return_value = [
            {
                "repo_full_name": "owner/repo",
                "number": 42,
                "title": "Ship scheduler",
                "body": "## 目的\nIssue body based goal",
                "url": "https://github.com/owner/repo/issues/42",
                "issue_state": "OPEN",
                "state": "Backlog",
                "plan": "Drafted",
            }
        ]

        await self.client._scheduler_tick()

        self.assertEqual("91234", self.state_store.thread_id_for_issue("owner/repo#42"))
        self.assertEqual(1, len(self.status_channel.created_threads))

    async def test_scheduler_tick_merges_pr_when_state_is_merging(self) -> None:
        issue_key = "owner/repo#42"
        self.state_store.create_issue_record(issue_key, thread_id=321, status="Merging")
        self.state_store.update_issue_meta(
            issue_key, github_repo="owner/repo", issue_number="42", plan_state="Approved"
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
        self.state_store.write_artifact(
            issue_key,
            "pr.json",
            {"number": 99, "url": "https://github.com/owner/repo/pull/99"},
        )
        thread = _FakeThread(321)
        self.client.get_channel = lambda channel_id: thread if channel_id == 321 else self.status_channel  # type: ignore[method-assign]
        self.client.github_client = MagicMock()
        self.client.github_client.list_project_issues.return_value = [
            {
                "repo_full_name": "owner/repo",
                "number": 42,
                "title": "Ship scheduler",
                "body": "body",
                "url": "https://github.com/owner/repo/issues/42",
                "issue_state": "OPEN",
                "state": "Merging",
                "plan": "Approved",
            }
        ]
        self.client.github_client.merge_pull_request.return_value = {
            "merged": True,
            "message": "merged",
            "sha": "abc123",
        }
        self.client.github_client.get_pull_request_status.return_value = {
            "draft": False,
            "mergeable": True,
            "mergeable_state": "clean",
            "head_sha": "headsha123",
        }
        self.client.github_client.update_issue_state.return_value = None
        self.client.github_client.upsert_workpad_comment.return_value = None

        await self.client._scheduler_tick()

        self.assertEqual("Done", self.state_store.load_issue_meta(issue_key)["status"])
        self.assertTrue(any("merge" in message for message in thread.messages))

    async def test_scheduler_tick_blocks_merging_issue_without_pr(self) -> None:
        issue_key = "owner/repo#42"
        self.state_store.create_issue_record(issue_key, thread_id=321, status="Merging")
        self.state_store.update_issue_meta(
            issue_key, github_repo="owner/repo", issue_number="42", plan_state="Approved"
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
        self.client.github_client = MagicMock()
        self.client.github_client.list_project_issues.return_value = [
            {
                "repo_full_name": "owner/repo",
                "number": 42,
                "title": "Ship scheduler",
                "body": "body",
                "url": "https://github.com/owner/repo/issues/42",
                "issue_state": "OPEN",
                "state": "Merging",
                "plan": "Approved",
            }
        ]
        self.client.github_client.update_issue_state.return_value = None
        self.client.github_client.upsert_workpad_comment.return_value = None

        await self.client._scheduler_tick()

        self.assertEqual("Blocked", self.state_store.load_issue_meta(issue_key)["status"])

    async def test_scheduler_tick_blocks_merging_issue_when_pr_is_still_draft(self) -> None:
        issue_key = "owner/repo#42"
        self.state_store.create_issue_record(issue_key, thread_id=321, status="Merging")
        self.state_store.update_issue_meta(
            issue_key, github_repo="owner/repo", issue_number="42", plan_state="Approved"
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
        self.state_store.write_artifact(
            issue_key,
            "pr.json",
            {"number": 99, "url": "https://github.com/owner/repo/pull/99"},
        )
        self.client.github_client = MagicMock()
        self.client.github_client.list_project_issues.return_value = [
            {
                "repo_full_name": "owner/repo",
                "number": 42,
                "title": "Ship scheduler",
                "body": "body",
                "url": "https://github.com/owner/repo/issues/42",
                "issue_state": "OPEN",
                "state": "Merging",
                "plan": "Approved",
            }
        ]
        self.client.github_client.get_pull_request_status.return_value = {
            "draft": True,
            "mergeable": True,
            "mergeable_state": "clean",
            "head_sha": "headsha123",
        }
        self.client.github_client.update_issue_state.return_value = None
        self.client.github_client.upsert_workpad_comment.return_value = None

        await self.client._scheduler_tick()

        self.assertEqual("Blocked", self.state_store.load_issue_meta(issue_key)["status"])

    async def test_restore_pending_runs_keeps_in_progress_issue_when_process_record_exists(self) -> None:
        issue_key = "owner/repo#42"
        self.state_store.create_issue_record(issue_key, thread_id=321, status="In Progress")
        self.state_store.update_issue_meta(
            issue_key,
            github_repo="owner/repo",
            issue_number="42",
            plan_state="Approved",
            runtime_status="running",
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
        self.client.process_registry.register(issue_key, "run-1", pid=1, runner_type="codex")

        await self.client._restore_pending_runs()

        meta = self.state_store.load_issue_meta(issue_key)
        self.assertEqual("In Progress", meta["status"])
        self.assertEqual("running", meta["runtime_status"])

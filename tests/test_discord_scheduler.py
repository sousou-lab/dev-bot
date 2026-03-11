from __future__ import annotations

import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock

from app import run_request
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

    def test_sync_project_board_state_returns_only_project_items_when_sync_succeeds(self) -> None:
        self.state_store.create_issue_record("owner/repo#7", status="Ready")
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

        self.assertEqual(["owner/repo#42"], [meta["issue_key"] for meta in metas])

    def test_clear_execution_artifacts_keeps_issue_number_for_issue_bound_thread(self) -> None:
        self.state_store.create_run(thread_id=1, parent_message_id=10, channel_id=20)
        self.state_store.bind_issue(1, "owner/repo", 42)
        self.state_store.update_issue_meta(
            "owner/repo#42",
            issue_number="42",
            pr_number="99",
            pr_url="https://github.com/owner/repo/pull/99",
            workspace="/tmp/work",
            branch_name="agent/gh-42-test",
            base_branch="main",
        )
        self.state_store.write_artifact(
            "owner/repo#42",
            "issue.json",
            {
                "repo_full_name": "owner/repo",
                "number": 42,
                "title": "Existing issue",
                "url": "https://github.com/owner/repo/issues/42",
            },
        )
        self.state_store.write_artifact("owner/repo#42", "plan.json", {"steps": ["keep identity"]})

        self.client._clear_execution_artifacts(1)

        issue_meta = self.state_store.load_issue_meta("owner/repo#42")
        self.assertEqual("42", issue_meta["issue_number"])
        self.assertEqual("", issue_meta["pr_number"])
        self.assertEqual("", issue_meta["workspace"])
        self.assertEqual(42, self.state_store.load_artifact("owner/repo#42", "issue.json")["number"])
        self.assertEqual({}, self.state_store.load_artifact("owner/repo#42", "plan.json"))


class _FakeThread:
    def __init__(self, thread_id: int) -> None:
        self.id = thread_id
        self.messages: list[str] = []

    async def send(self, content: str) -> None:
        self.messages.append(content)


class _FakeMessage:
    def __init__(self, channel: object, *, message_id: int = 1) -> None:
        self.channel = channel
        self.id = message_id


class _FakeResponse:
    def __init__(self) -> None:
        self.messages: list[tuple[str, bool]] = []
        self.deferred: list[bool] = []

    def is_done(self) -> bool:
        return False

    async def send_message(self, content: str, ephemeral: bool = False) -> None:
        self.messages.append((content, ephemeral))

    async def defer(self, thinking: bool = False) -> None:
        self.deferred.append(thinking)


class _FakeInteraction:
    def __init__(self, channel: object) -> None:
        self.channel = channel
        self.response = _FakeResponse()


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
        self._orig_run_request_blocking = run_request.run_blocking

        async def _run_blocking(func, /, *args, **kwargs):
            return func(*args, **kwargs)

        self.client._run_blocking = _run_blocking  # type: ignore[method-assign]
        run_request.run_blocking = _run_blocking
        self.status_channel = _FakeStatusChannel()
        self.client.get_channel = lambda channel_id: self.status_channel if channel_id == 67890 else None  # type: ignore[method-assign]

    async def asyncTearDown(self) -> None:
        run_request.run_blocking = self._orig_run_request_blocking
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

    async def test_scheduler_tick_does_not_dispatch_from_local_cache_when_project_sync_fails(self) -> None:
        issue_key = "owner/repo#42"
        self.state_store.create_issue_record(issue_key, thread_id=321, status="Ready")
        self.state_store.update_issue_meta(
            issue_key,
            github_repo="owner/repo",
            issue_number="42",
            plan_state="Approved",
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
        self.client.github_client.list_project_issues.side_effect = RuntimeError("project unavailable")
        enqueue_mock = MagicMock()
        self.client.orchestrator.enqueue = enqueue_mock  # type: ignore[method-assign]

        await self.client._scheduler_tick()

        enqueue_mock.assert_not_called()

    async def test_revise_keeps_issue_identity_for_issue_bound_thread(self) -> None:
        self.state_store.create_run(thread_id=321, parent_message_id=10, channel_id=20)
        self.state_store.bind_issue(321, "owner/repo", 42)
        self.state_store.update_issue_meta(
            "owner/repo#42",
            status="Human Review",
            issue_number="42",
            pr_number="99",
            workspace="/tmp/work",
        )
        self.state_store.write_artifact(
            "owner/repo#42",
            "issue.json",
            {
                "repo_full_name": "owner/repo",
                "number": 42,
                "title": "Existing issue",
                "url": "https://github.com/owner/repo/issues/42",
            },
        )
        self.state_store.write_artifact("owner/repo#42", "plan.json", {"steps": ["replan"]})

        self.client._ensure_managed_thread = lambda channel: 321  # type: ignore[method-assign]
        interaction = _FakeInteraction(_FakeThread(321))

        await self.client.revise_command(interaction)

        issue_meta = self.state_store.load_issue_meta("owner/repo#42")
        draft_meta = self.state_store.load_draft_meta(321)
        self.assertEqual("42", issue_meta["issue_number"])
        self.assertEqual("Human Review", issue_meta["status"])
        self.assertEqual("requirements_dialogue", draft_meta["status"])
        self.assertEqual("", issue_meta["pr_number"])
        self.assertEqual(42, self.state_store.load_artifact("owner/repo#42", "issue.json")["number"])
        self.assertEqual({}, self.state_store.load_artifact("owner/repo#42", "plan.json"))
        self.assertEqual(
            [("要件整理を再開しました。修正内容を投稿してください。", True)],
            interaction.response.messages,
        )

    async def test_handle_thread_message_keeps_issue_state_when_bound_thread_enters_requirements_dialogue(self) -> None:
        thread_id = 321
        issue_key = "owner/repo#42"
        self.state_store.create_run(thread_id=thread_id, parent_message_id=10, channel_id=20)
        self.state_store.bind_issue(thread_id, "owner/repo", 42)
        self.state_store.update_issue_meta(
            issue_key,
            status="Human Review",
            issue_number="42",
            github_repo="owner/repo",
        )

        async def _parse_message_inputs(_message):
            return {"error": None}

        async def _materialize_message_payload(_thread_id, _message, _parsed):
            del _message, _parsed
            return "follow-up question"

        async def _send_channel_text(_channel, _content):
            del _channel, _content
            return None

        self.client._parse_message_inputs = _parse_message_inputs  # type: ignore[method-assign]
        self.client._materialize_message_payload = _materialize_message_payload  # type: ignore[method-assign]
        self.client._send_channel_text = _send_channel_text  # type: ignore[method-assign]
        self.client.requirements_agent = MagicMock()
        self.client.requirements_agent.build_reply.return_value = MagicMock(
            body="Need one clarification",
            status="requirements_dialogue",
            artifacts={},
        )

        await self.client._handle_thread_message(_FakeMessage(_FakeThread(thread_id)))

        self.assertEqual("Human Review", self.state_store.load_issue_meta(issue_key)["status"])
        self.assertEqual("requirements_dialogue", self.state_store.load_draft_meta(thread_id)["status"])

    async def test_status_command_prefers_dialogue_state_for_issue_bound_thread_ui(self) -> None:
        thread_id = 321
        issue_key = "owner/repo#42"
        self.state_store.create_run(thread_id=thread_id, parent_message_id=10, channel_id=20)
        self.state_store.bind_issue(thread_id, "owner/repo", 42)
        self.state_store.update_issue_meta(
            issue_key,
            status="Human Review",
            issue_number="42",
            github_repo="owner/repo",
        )
        self.state_store.update_draft_meta(thread_id, status="requirements_dialogue")
        self.state_store.write_artifact(
            issue_key,
            "issue.json",
            {
                "repo_full_name": "owner/repo",
                "number": 42,
                "title": "Existing issue",
                "url": "https://github.com/owner/repo/issues/42",
            },
        )
        interaction = _FakeInteraction(_FakeThread(thread_id))
        self.client._ensure_managed_thread = lambda channel: thread_id  # type: ignore[method-assign]

        await self.client.status_command(interaction)

        self.assertIn("status: `requirements_dialogue`", interaction.response.messages[0][0])

    async def test_handle_thread_message_blocks_when_in_progress_process_exists_without_runtime_status(self) -> None:
        thread_id = 321
        issue_key = "owner/repo#42"
        self.state_store.create_run(thread_id=thread_id, parent_message_id=10, channel_id=20)
        self.state_store.bind_issue(thread_id, "owner/repo", 42)
        self.state_store.update_issue_meta(
            issue_key,
            status="In Progress",
            issue_number="42",
            github_repo="owner/repo",
            runtime_status="",
        )
        self.state_store.write_artifact(
            issue_key,
            "issue.json",
            {"repo_full_name": "owner/repo", "number": 42, "title": "Existing issue"},
        )
        self.client.process_registry.register(issue_key, "run-1", pid=1, runner_type="codex")
        called = {"parse": False}

        async def _parse_message_inputs(_message):
            called["parse"] = True
            return {"error": None}

        self.client._parse_message_inputs = _parse_message_inputs  # type: ignore[method-assign]
        message = _FakeMessage(_FakeThread(thread_id))

        await self.client._handle_thread_message(message)

        self.assertFalse(called["parse"])
        self.assertEqual("running", self.state_store.load_issue_meta(issue_key)["runtime_status"])

    async def test_handle_thread_message_keeps_execution_artifacts_for_human_review(self) -> None:
        thread_id = 321
        issue_key = "owner/repo#42"
        self.state_store.create_run(thread_id=thread_id, parent_message_id=10, channel_id=20)
        self.state_store.bind_issue(thread_id, "owner/repo", 42)
        self.state_store.update_issue_meta(
            issue_key,
            status="Human Review",
            issue_number="42",
            github_repo="owner/repo",
        )
        self.state_store.write_artifact(issue_key, "verification.json", {"checks": ["pytest"]})
        self.state_store.write_artifact(
            issue_key,
            "final_summary.json",
            {"summary": "ready for review"},
        )
        self.state_store.write_artifact(
            issue_key,
            "pr.json",
            {"number": 99, "url": "https://github.com/owner/repo/pull/99"},
        )

        async def _parse_message_inputs(_message):
            return {"error": None}

        async def _materialize_message_payload(_thread_id, _message, _parsed):
            del _message, _parsed
            return "follow-up question"

        async def _send_channel_text(_channel, _content):
            del _channel, _content
            return None

        self.client._parse_message_inputs = _parse_message_inputs  # type: ignore[method-assign]
        self.client._materialize_message_payload = _materialize_message_payload  # type: ignore[method-assign]
        self.client._send_channel_text = _send_channel_text  # type: ignore[method-assign]
        self.client.requirements_agent = MagicMock()
        self.client.requirements_agent.build_reply.return_value = MagicMock(
            body="Under review",
            status="Human Review",
            artifacts={},
        )

        await self.client._handle_thread_message(_FakeMessage(_FakeThread(thread_id)))

        self.assertEqual(
            {"checks": ["pytest"]},
            self.state_store.load_artifact(issue_key, "verification.json"),
        )
        self.assertEqual(
            {"summary": "ready for review"},
            self.state_store.load_artifact(issue_key, "final_summary.json"),
        )
        self.assertEqual(
            {"number": 99, "url": "https://github.com/owner/repo/pull/99"},
            self.state_store.load_artifact(issue_key, "pr.json"),
        )

    async def test_handle_thread_message_clears_execution_artifacts_for_done_thread(self) -> None:
        thread_id = 321
        issue_key = "owner/repo#42"
        self.state_store.create_run(thread_id=thread_id, parent_message_id=10, channel_id=20)
        self.state_store.bind_issue(thread_id, "owner/repo", 42)
        self.state_store.update_issue_meta(
            issue_key,
            status="Done",
            issue_number="42",
            github_repo="owner/repo",
            pr_number="99",
            workspace="/tmp/work",
            branch_name="agent/gh-42-test",
            base_branch="main",
        )
        self.state_store.write_artifact(
            issue_key,
            "issue.json",
            {
                "repo_full_name": "owner/repo",
                "number": 42,
                "title": "Existing issue",
                "url": "https://github.com/owner/repo/issues/42",
            },
        )
        self.state_store.write_artifact(
            issue_key,
            "pr.json",
            {"number": 99, "url": "https://github.com/owner/repo/pull/99"},
        )

        async def _parse_message_inputs(_message):
            return {"error": None}

        async def _materialize_message_payload(_thread_id, _message, _parsed):
            del _message, _parsed
            return "new requirement"

        async def _send_channel_text(_channel, _content):
            del _channel, _content
            return None

        self.client._parse_message_inputs = _parse_message_inputs  # type: ignore[method-assign]
        self.client._materialize_message_payload = _materialize_message_payload  # type: ignore[method-assign]
        self.client._send_channel_text = _send_channel_text  # type: ignore[method-assign]
        self.client.requirements_agent = MagicMock()
        self.client.requirements_agent.build_reply.return_value = MagicMock(
            body="Let's re-scope",
            status="requirements_dialogue",
            artifacts={},
        )

        await self.client._handle_thread_message(_FakeMessage(_FakeThread(thread_id)))

        issue_meta = self.state_store.load_issue_meta(issue_key)
        self.assertEqual("", issue_meta["pr_number"])
        self.assertEqual("", issue_meta["workspace"])
        self.assertEqual({}, self.state_store.load_artifact(issue_key, "pr.json"))

    async def test_scheduler_tick_reconciles_orphaned_in_progress_issue_by_issue_key(self) -> None:
        issue_key = "owner/repo#42"
        self.state_store.create_issue_record(issue_key, status="In Progress")
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
        self.client.github_client = MagicMock()
        self.client.github_client.list_project_issues.return_value = [
            {
                "repo_full_name": "owner/repo",
                "number": 42,
                "title": "Ship scheduler",
                "body": "body",
                "url": "https://github.com/owner/repo/issues/42",
                "issue_state": "OPEN",
                "state": "In Progress",
                "plan": "Approved",
            }
        ]

        async def _ensure_issue_thread_binding(_issue_key: str) -> int:
            return 0

        self.client._ensure_issue_thread_binding = _ensure_issue_thread_binding  # type: ignore[method-assign]
        self.client.github_client.update_issue_state.return_value = None

        await self.client._scheduler_tick()

        meta = self.state_store.load_issue_meta(issue_key)
        self.assertEqual("Rework", meta["status"])
        self.assertEqual("", meta["runtime_status"])
        self.client.github_client.update_issue_state.assert_called_with("owner/repo", 42, "Rework")

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

    async def test_reconcile_does_not_block_merging_issue_without_runtime_process(self) -> None:
        issue_key = "owner/repo#42"
        self.state_store.create_issue_record(issue_key, thread_id=321, status="Merging")
        self.state_store.update_issue_meta(
            issue_key,
            github_repo="owner/repo",
            issue_number="42",
            plan_state="Approved",
            runtime_status="",
        )

        self.client._reconcile_thread_runtime_state(321)

        meta = self.state_store.load_issue_meta(issue_key)
        self.assertEqual("Merging", meta["status"])

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
        self.client.github_client.update_issue_state.assert_called_with("owner/repo", 42, "Blocked")

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
        self.client.github_client.update_issue_state.assert_called_with("owner/repo", 42, "Blocked")

    async def test_scheduler_tick_blocks_merging_issue_when_pr_head_sha_changed(self) -> None:
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
            {"number": 99, "url": "https://github.com/owner/repo/pull/99", "head_sha": "expectedsha"},
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
            "draft": False,
            "mergeable": True,
            "mergeable_state": "clean",
            "head_sha": "actualsha",
        }
        self.client.github_client.update_issue_state.return_value = None
        self.client.github_client.upsert_workpad_comment.return_value = None

        await self.client._scheduler_tick()

        self.assertEqual("Blocked", self.state_store.load_issue_meta(issue_key)["status"])
        self.client.github_client.update_issue_state.assert_called_with("owner/repo", 42, "Blocked")

    async def test_scheduler_tick_blocks_merging_issue_when_pr_is_unstable(self) -> None:
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
            "draft": False,
            "mergeable": True,
            "mergeable_state": "unstable",
            "head_sha": "headsha123",
        }
        self.client.github_client.update_issue_state.return_value = None
        self.client.github_client.upsert_workpad_comment.return_value = None

        await self.client._scheduler_tick()

        self.assertEqual("Blocked", self.state_store.load_issue_meta(issue_key)["status"])
        self.client.github_client.merge_pull_request.assert_not_called()
        self.client.github_client.update_issue_state.assert_called_with("owner/repo", 42, "Blocked")

    async def test_scheduler_tick_keeps_merging_issue_when_pr_mergeability_is_unknown(self) -> None:
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
            "draft": False,
            "mergeable": True,
            "mergeable_state": "unknown",
            "head_sha": "headsha123",
        }
        self.client.github_client.merge_pull_request.return_value = {
            "merged": False,
            "message": "mergeability pending",
            "sha": "",
        }

        await self.client._scheduler_tick()

        self.assertEqual("Merging", self.state_store.load_issue_meta(issue_key)["status"])
        self.client.github_client.update_issue_state.assert_not_called()

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

    async def test_restore_pending_runs_updates_project_state_when_in_progress_run_is_missing(self) -> None:
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
        self.client.github_client = MagicMock()
        self.client.github_client.update_issue_state.return_value = None

        await self.client._restore_pending_runs()

        meta = self.state_store.load_issue_meta(issue_key)
        self.assertEqual("Rework", meta["status"])
        self.client.github_client.update_issue_state.assert_called_with("owner/repo", 42, "Rework")

    async def test_reconcile_marks_in_progress_issue_rework_when_process_record_is_stale(self) -> None:
        issue_key = "owner/repo#42"
        self.state_store.create_issue_record(issue_key, thread_id=321, status="In Progress")
        self.state_store.update_issue_meta(
            issue_key,
            github_repo="owner/repo",
            issue_number="42",
            plan_state="Approved",
            runtime_status="running",
        )
        self.client.process_registry.register(issue_key, "run-1", pid=999999, runner_type="codex")
        self.client.github_client = MagicMock()
        self.client.github_client.update_issue_state.return_value = None

        self.client._reconcile_thread_runtime_state(321)

        meta = self.state_store.load_issue_meta(issue_key)
        self.assertEqual("Rework", meta["status"])
        self.assertEqual("", meta["runtime_status"])
        self.assertEqual({}, self.client.process_registry.load(issue_key))
        self.client.github_client.update_issue_state.assert_called_with("owner/repo", 42, "Rework")

    async def test_reconcile_keeps_in_progress_issue_when_legacy_thread_process_record_is_active(self) -> None:
        issue_key = "owner/repo#42"
        self.state_store.create_issue_record(issue_key, thread_id=321, status="In Progress")
        self.state_store.update_issue_meta(
            issue_key,
            github_repo="owner/repo",
            issue_number="42",
            plan_state="Approved",
            runtime_status="running",
        )
        original_is_active = self.client.process_registry.is_active

        def is_active(identifier):
            if identifier == issue_key:
                return False
            if identifier == 321:
                return True
            return original_is_active(identifier)

        self.client.process_registry.is_active = is_active  # type: ignore[method-assign]
        self.client.github_client = MagicMock()

        self.client._reconcile_thread_runtime_state(321)

        meta = self.state_store.load_issue_meta(issue_key)
        self.assertEqual("In Progress", meta["status"])
        self.client.github_client.update_issue_state.assert_not_called()

    async def test_restore_pending_runs_skips_ready_issue_when_plan_is_no_longer_approved(self) -> None:
        issue_key = "owner/repo#42"
        self.client.settings.github_project_id = "project-1"
        self.state_store.create_issue_record(issue_key, thread_id=321, status="Ready")
        self.state_store.update_issue_meta(
            issue_key,
            github_repo="owner/repo",
            issue_number="42",
            plan_state="Approved",
            runtime_status="",
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
        self.client.github_client.get_issue_project_fields.return_value = {
            "state": "Ready",
            "plan": "Changes Requested",
        }
        restore_mock = AsyncMock()
        self.client.orchestrator.restore = restore_mock  # type: ignore[method-assign]

        await self.client._restore_pending_runs()

        restore_mock.assert_not_called()

    async def test_restore_pending_runs_restores_ready_issue_without_project_configuration(self) -> None:
        issue_key = "owner/repo#42"
        self.client.settings.github_project_id = ""
        self.state_store.create_issue_record(issue_key, thread_id=321, status="Ready")
        self.state_store.update_issue_meta(
            issue_key,
            github_repo="owner/repo",
            issue_number="42",
            plan_state="Approved",
            runtime_status="",
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
        restored: list[object] = []

        async def _restore(items):
            restored.extend(items)

        self.client.orchestrator.restore = _restore  # type: ignore[method-assign]

        await self.client._restore_pending_runs()

        self.assertEqual(1, len(restored))
        self.assertEqual(issue_key, restored[0].issue_key)

    async def test_dispatch_issue_if_ready_ignores_stale_process_record(self) -> None:
        issue_key = "owner/repo#42"
        self.state_store.create_issue_record(issue_key, thread_id=321, status="Ready")
        self.state_store.update_issue_meta(
            issue_key,
            github_repo="owner/repo",
            issue_number="42",
            plan_state="Approved",
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
        self.state_store.write_artifact(321, "requirement_summary.json", {"goal": "ship"})
        self.state_store.write_artifact(321, "plan.json", {"steps": ["one"]})
        self.state_store.write_artifact(321, "test_plan.json", {"checks": ["tests"]})
        self.client.process_registry.register(issue_key, "run-1", pid=999999, runner_type="codex")
        enqueue_mock = AsyncMock(return_value=True)
        self.client.orchestrator.enqueue = enqueue_mock  # type: ignore[method-assign]

        await self.client._dispatch_issue_if_ready(
            thread_id=321,
            issue_key=issue_key,
            repo_full_name="owner/repo",
            issue_number=42,
            expected_state="Ready",
        )

        enqueue_mock.assert_called_once()
        self.assertEqual({}, self.client.process_registry.load(issue_key))

    async def test_dispatch_issue_if_ready_skips_closed_issue(self) -> None:
        issue_key = "owner/repo#42"
        self.state_store.create_issue_record(issue_key, thread_id=321, status="Ready")
        self.state_store.update_issue_meta(
            issue_key,
            github_repo="owner/repo",
            issue_number="42",
            plan_state="Approved",
        )
        self.state_store.write_artifact(
            issue_key,
            "issue.json",
            {
                "repo_full_name": "owner/repo",
                "number": 42,
                "title": "Ship scheduler",
                "url": "https://github.com/owner/repo/issues/42",
                "state": "CLOSED",
            },
        )
        self.state_store.write_artifact(321, "requirement_summary.json", {"goal": "ship"})
        self.state_store.write_artifact(321, "plan.json", {"steps": ["one"]})
        self.state_store.write_artifact(321, "test_plan.json", {"checks": ["tests"]})
        enqueue_mock = AsyncMock(return_value=True)
        self.client.orchestrator.enqueue = enqueue_mock  # type: ignore[method-assign]

        await self.client._dispatch_issue_if_ready(
            thread_id=321,
            issue_key=issue_key,
            repo_full_name="owner/repo",
            issue_number=42,
            expected_state="Ready",
        )

        enqueue_mock.assert_not_called()

    async def test_promote_approved_plan_marks_promotion_failed_when_issue_binding_exists(self) -> None:
        thread_id = 321
        self.state_store.create_run(thread_id=thread_id, parent_message_id=1, channel_id=2)
        self.state_store.write_artifact(thread_id, "requirement_summary.json", {"goal": "ship"})
        self.state_store.write_artifact(thread_id, "plan.json", {"steps": ["one"]})
        self.state_store.write_artifact(thread_id, "test_plan.json", {"checks": ["tests"]})
        self.state_store.bind_issue(thread_id, "owner/repo", 42)
        self.state_store.write_artifact(
            "owner/repo#42",
            "issue.json",
            {
                "repo_full_name": "owner/repo",
                "number": 42,
                "title": "Ship scheduler",
                "body": "body",
                "url": "https://github.com/owner/repo/issues/42",
            },
        )
        self.client.github_client = MagicMock()
        self.client.github_client.update_issue_plan.side_effect = RuntimeError("plan update failed")

        class _Resp:
            async def defer(self, thinking: bool = False) -> None:
                del thinking

            async def send_message(self, content: str, *, ephemeral: bool = False) -> None:
                del content, ephemeral

        class _Chan(_FakeThread):
            jump_url = "https://discord.test/thread/321"

            def __init__(self, channel_id: int) -> None:
                super().__init__(channel_id)

        interaction = MagicMock()
        interaction.channel = _Chan(thread_id)
        interaction.response = _Resp()
        sent: list[str] = []
        self.client._ensure_managed_thread = lambda channel: thread_id  # type: ignore[method-assign]

        async def _send_followup_text(_interaction, content: str, *, ephemeral: bool = False) -> None:
            del _interaction, ephemeral
            sent.append(content)

        self.client._send_followup_text = _send_followup_text  # type: ignore[method-assign]

        await self.client._promote_approved_plan(interaction)

        self.assertEqual("promotion_failed", self.state_store.load_draft_meta(thread_id)["status"])
        self.assertTrue(sent)

    async def test_generate_plan_resets_remote_plan_to_drafted_for_bound_issue(self) -> None:
        thread_id = 321
        self.state_store.create_run(thread_id=thread_id, parent_message_id=1, channel_id=2)
        self.state_store.write_artifact(thread_id, "requirement_summary.json", {"goal": "ship"})
        self.state_store.bind_issue(thread_id, "owner/repo", 42)
        self.state_store.update_issue_meta("owner/repo#42", github_repo="owner/repo", issue_number="42")
        self.client.github_client = MagicMock()
        self.client._build_plan_artifacts = MagicMock(
            return_value={
                "plan": {"steps": ["one"]},
                "test_plan": {"checks": ["tests"]},
                "repo_profile": {"repo": "owner/repo"},
                "planning_workspace": {"base_branch": "main"},
                "planning_sessions": {},
            }
        )

        class _Resp:
            async def defer(self, thinking: bool = False) -> None:
                del thinking

        class _Chan(_FakeThread):
            def __init__(self, channel_id: int) -> None:
                super().__init__(channel_id)

        interaction = MagicMock()
        interaction.channel = _Chan(thread_id)
        interaction.response = _Resp()
        self.client._ensure_managed_thread = lambda channel: thread_id  # type: ignore[method-assign]

        async def _send_followup_text(_interaction, content: str, *, ephemeral: bool = False) -> None:
            del _interaction, content, ephemeral

        self.client._send_followup_text = _send_followup_text  # type: ignore[method-assign]

        await self.client._generate_plan(interaction, "owner/repo", alias_used=False)

        self.client.github_client.update_issue_plan.assert_called_with("owner/repo", 42, "Drafted")

    async def test_promote_approved_plan_adds_new_issue_to_project_before_updating_fields(self) -> None:
        thread_id = 321
        self.state_store.create_run(thread_id=thread_id, parent_message_id=1, channel_id=2)
        self.state_store.write_artifact(thread_id, "requirement_summary.json", {"goal": "ship"})
        self.state_store.write_artifact(thread_id, "plan.json", {"steps": ["one"]})
        self.state_store.write_artifact(thread_id, "test_plan.json", {"checks": ["tests"]})
        self.state_store.update_draft_meta(thread_id, github_repo="owner/repo")
        self.client.github_client = MagicMock()
        self.client.github_client.create_issue.return_value = MagicMock(
            repo_full_name="owner/repo",
            number=42,
            title="Ship scheduler",
            body="body",
            url="https://github.com/owner/repo/issues/42",
        )

        class _Resp:
            async def defer(self, thinking: bool = False) -> None:
                del thinking

            async def send_message(self, content: str, *, ephemeral: bool = False) -> None:
                del content, ephemeral

        class _Chan(_FakeThread):
            jump_url = "https://discord.test/thread/321"

            def __init__(self, channel_id: int) -> None:
                super().__init__(channel_id)

        interaction = MagicMock()
        interaction.channel = _Chan(thread_id)
        interaction.response = _Resp()
        self.client._ensure_managed_thread = lambda channel: thread_id  # type: ignore[method-assign]

        async def _send_followup_text(_interaction, content: str, *, ephemeral: bool = False) -> None:
            del _interaction, content, ephemeral

        self.client._send_followup_text = _send_followup_text  # type: ignore[method-assign]

        async def _scheduler_tick() -> None:
            return None

        self.client._scheduler_tick = _scheduler_tick  # type: ignore[method-assign]

        await self.client._promote_approved_plan(interaction)

        self.client.github_client.add_issue_to_project.assert_called_with("owner/repo", 42)

    async def test_promote_approved_plan_marks_promotion_failed_when_project_addition_fails(self) -> None:
        thread_id = 321
        self.state_store.create_run(thread_id=thread_id, parent_message_id=1, channel_id=2)
        self.state_store.write_artifact(thread_id, "requirement_summary.json", {"goal": "ship"})
        self.state_store.write_artifact(thread_id, "plan.json", {"steps": ["one"]})
        self.state_store.write_artifact(thread_id, "test_plan.json", {"checks": ["tests"]})
        self.state_store.update_draft_meta(thread_id, github_repo="owner/repo")
        self.client.github_client = MagicMock()
        self.client.github_client.create_issue.return_value = MagicMock(
            repo_full_name="owner/repo",
            number=42,
            title="Ship scheduler",
            body="body",
            url="https://github.com/owner/repo/issues/42",
        )
        self.client.github_client.add_issue_to_project.side_effect = RuntimeError("project unavailable")

        class _Resp:
            async def defer(self, thinking: bool = False) -> None:
                del thinking

            async def send_message(self, content: str, *, ephemeral: bool = False) -> None:
                del content, ephemeral

        class _Chan(_FakeThread):
            jump_url = "https://discord.test/thread/321"

            def __init__(self, channel_id: int) -> None:
                super().__init__(channel_id)

        interaction = MagicMock()
        interaction.channel = _Chan(thread_id)
        interaction.response = _Resp()
        self.client._ensure_managed_thread = lambda channel: thread_id  # type: ignore[method-assign]
        sent: list[str] = []

        async def _send_followup_text(_interaction, content: str, *, ephemeral: bool = False) -> None:
            del _interaction, ephemeral
            sent.append(content)

        self.client._send_followup_text = _send_followup_text  # type: ignore[method-assign]

        await self.client._promote_approved_plan(interaction)

        self.assertEqual("promotion_failed", self.state_store.load_draft_meta(thread_id)["status"])
        self.assertEqual("owner/repo#42", self.state_store.issue_key_for_thread(thread_id))
        self.assertEqual(
            42,
            self.state_store.load_artifact("owner/repo#42", "issue.json")["number"],
        )
        self.assertTrue(sent)

    async def test_promote_approved_plan_enqueues_directly_when_project_is_unconfigured(self) -> None:
        thread_id = 321
        self.state_store.create_run(thread_id=thread_id, parent_message_id=1, channel_id=2)
        self.state_store.write_artifact(thread_id, "requirement_summary.json", {"goal": "ship"})
        self.state_store.write_artifact(thread_id, "plan.json", {"steps": ["one"]})
        self.state_store.write_artifact(thread_id, "test_plan.json", {"checks": ["tests"]})
        self.state_store.update_draft_meta(thread_id, github_repo="owner/repo")
        self.client.github_client = MagicMock()
        self.client.github_client.create_issue.return_value = MagicMock(
            repo_full_name="owner/repo",
            number=42,
            title="Ship scheduler",
            body="body",
            url="https://github.com/owner/repo/issues/42",
        )
        interaction = _FakeInteraction(_FakeThread(thread_id))
        self.client._ensure_managed_thread = lambda channel: thread_id  # type: ignore[method-assign]
        sent: list[str] = []

        async def _send_followup_text(_interaction, content: str, *, ephemeral: bool = False) -> None:
            del _interaction, ephemeral
            sent.append(content)

        self.client._send_followup_text = _send_followup_text  # type: ignore[method-assign]
        enqueue_mock = AsyncMock(return_value=True)
        self.client.orchestrator.enqueue = enqueue_mock  # type: ignore[method-assign]

        await self.client._promote_approved_plan(interaction)

        enqueue_mock.assert_called_once()
        self.assertIn("queue", sent[0])

    async def test_reject_high_risk_approval_updates_bound_issue_state(self) -> None:
        thread_id = 321
        issue_key = "owner/repo#42"
        self.state_store.create_run(thread_id=thread_id, parent_message_id=1, channel_id=2)
        self.state_store.bind_issue(thread_id, "owner/repo", 42)
        self.state_store.update_issue_meta(issue_key, github_repo="owner/repo", issue_number="42")
        self.client.github_client = MagicMock()
        self.client.approval_coordinator.create_request(thread_id, "run-1", "rm -rf", "rm -rf tmp", "dangerous")
        interaction = MagicMock()
        interaction.channel = _FakeThread(thread_id)
        interaction.response = _FakeResponse()
        interaction.user = "reviewer"
        self.client._ensure_managed_thread = lambda channel: thread_id  # type: ignore[method-assign]

        await self.client._resolve_approval(interaction, approved=False)

        self.client.github_client.update_issue_state.assert_called_with("owner/repo", 42, "Blocked")

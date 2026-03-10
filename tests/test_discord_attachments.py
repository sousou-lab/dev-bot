from __future__ import annotations

import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from app.state_store import FileStateStore

try:
    from app.discord_adapter import DevBotClient, _json_safe_value
except ModuleNotFoundError:  # pragma: no cover - depends on local test env
    DevBotClient = None
    _json_safe_value = None


class FakeAttachment:
    def __init__(self, filename: str, content: str, size: int | None = None) -> None:
        self.filename = filename
        self._content = content.encode("utf-8")
        self.size = size if size is not None else len(self._content)
        self.url = f"https://cdn.discordapp.test/{filename}"

    async def read(self) -> bytes:
        return self._content


class FakeMessage:
    def __init__(self, content: str, attachments: list[FakeAttachment], message_id: int = 1) -> None:
        self.content = content
        self.attachments = attachments
        self.id = message_id


@unittest.skipIf(DevBotClient is None, "discord.py is not installed in the current interpreter")
class DiscordAttachmentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        settings = SimpleNamespace(
            discord_bot_token="token",
            discord_guild_id="",
            github_token="token",
            anthropic_api_key="key",
            requirements_channel_id="1",
            workspace_root="/tmp/dev-bot-workspaces",
            runs_root=self.tempdir.name,
            max_implementation_iterations=5,
            max_concurrent_runs=5,
            codex_bin="codex",
            codex_app_server_command="codex app-server",
            codex_model="gpt-5-codex",
            approval_timeout_seconds=900,
            claude_agent_max_buffer_size=5 * 1024 * 1024,
        )
        self.state_store = FileStateStore(self.tempdir.name)
        self.state_store.create_run(thread_id=100, parent_message_id=200, channel_id=300)
        self.client = DevBotClient(settings=settings, state_store=self.state_store)

    async def asyncTearDown(self) -> None:
        await self.client.close()
        self.tempdir.cleanup()

    async def test_parse_supported_attachments(self) -> None:
        message = FakeMessage(
            "添付を読んでください",
            [FakeAttachment("requirements.md", "# Title\ncontent"), FakeAttachment("config.json", '{"a":1}')],
            message_id=123,
        )

        parsed = await self.client._parse_message_inputs(message)
        payload = await self.client._materialize_message_payload(100, message, parsed)

        self.assertEqual("", parsed["error"])
        self.assertIn("[attachment:requirements.md]", payload)
        self.assertIn("[attachment-metadata]", payload)
        attachments_dir = self.state_store.attachments_dir(100)
        self.assertTrue(any(path.name.startswith("123_requirements") for path in attachments_dir.iterdir()))

    async def test_rejects_unsupported_extension(self) -> None:
        message = FakeMessage("", [FakeAttachment("spec.pdf", "ignored")])

        parsed = await self.client._parse_message_inputs(message)

        self.assertIn("非対応形式", parsed["error"])

    async def test_rejects_oversized_attachment(self) -> None:
        message = FakeMessage("", [FakeAttachment("large.txt", "x", size=3 * 1024 * 1024)])

        parsed = await self.client._parse_message_inputs(message)

        self.assertIn("2MB", parsed["error"])

    async def test_enqueue_run_for_thread_creates_issue_and_queues_run(self) -> None:
        self.state_store.write_artifact(100, "requirement_summary.json", {"goal": "Ship feature"})
        self.state_store.write_artifact(100, "plan.json", {"implementation_steps": ["step1"]})
        self.state_store.write_artifact(100, "test_plan.json", {"cases": [{"id": "TC-1"}]})
        self.client.github_client.create_issue = Mock(  # type: ignore[method-assign]
            return_value=SimpleNamespace(
                repo_full_name="owner/repo",
                number=12,
                title="Issue title",
                body="Issue body",
                url="https://example.test/issues/12",
            )
        )
        self.client.orchestrator.enqueue = AsyncMock(return_value=True)  # type: ignore[method-assign]

        issue = await self.client._enqueue_run_for_thread(thread_id=100, channel=None, repo_full_name="owner/repo")

        self.assertEqual(12, issue["number"])
        saved_issue = self.state_store.load_artifact(100, "issue.json")
        self.assertEqual(12, saved_issue["number"])
        self.client.orchestrator.enqueue.assert_awaited_once()


@unittest.skipIf(_json_safe_value is None, "discord.py is not installed in the current interpreter")
class DiscordAdapterHelpersTests(unittest.TestCase):
    def test_json_safe_value_decodes_bytes_recursively(self) -> None:
        payload = {
            "session_id": b"sess_123",
            "nested": [b"abc", {"value": b"xyz"}],
        }

        normalized = _json_safe_value(payload)

        self.assertEqual("sess_123", normalized["session_id"])
        self.assertEqual("abc", normalized["nested"][0])
        self.assertEqual("xyz", normalized["nested"][1]["value"])

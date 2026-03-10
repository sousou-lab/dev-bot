from __future__ import annotations

import tempfile
import unittest

from app.chat_inputs import chunk_message, ensure_new_thread_body, materialize_message_payload, parse_message_inputs
from app.state_store import FileStateStore


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


class ChatInputsTests(unittest.IsolatedAsyncioTestCase):
    async def test_parse_supported_attachments(self) -> None:
        message = FakeMessage(
            "添付を読んでください",
            [FakeAttachment("requirements.md", "# Title\ncontent"), FakeAttachment("config.json", '{"a":1}')],
            message_id=123,
        )

        parsed = await parse_message_inputs(message)

        self.assertEqual("", parsed["error"])
        self.assertIn("[attachment:requirements.md]", parsed["body"])
        self.assertEqual(2, len(parsed["attachments"]))

    async def test_rejects_unsupported_extension(self) -> None:
        parsed = await parse_message_inputs(FakeMessage("", [FakeAttachment("spec.pdf", "ignored")]))

        self.assertIn("非対応形式", parsed["error"])

    async def test_rejects_oversized_attachment(self) -> None:
        parsed = await parse_message_inputs(FakeMessage("", [FakeAttachment("large.txt", "x", size=3 * 1024 * 1024)]))

        self.assertIn("2MB", parsed["error"])

    async def test_new_thread_requires_body_or_attachment(self) -> None:
        parsed = await parse_message_inputs(FakeMessage("   ", []))
        updated = ensure_new_thread_body(parsed)

        self.assertIn("本文か対応添付ファイル", updated["error"])

    async def test_materialize_payload_writes_attachment_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state_store = FileStateStore(tmpdir)
            state_store.create_run(thread_id=100, parent_message_id=200, channel_id=300)
            parsed = await parse_message_inputs(
                FakeMessage(
                    "添付を読んでください", [FakeAttachment("requirements.md", "# Title\ncontent")], message_id=123
                )
            )

            payload = materialize_message_payload(
                thread_id=100,
                message_id=123,
                parsed=parsed,
                state_store=state_store,
            )

            self.assertIn("[attachment:requirements.md]", payload)
            self.assertIn("[attachment-metadata]", payload)
            attachments_dir = state_store.attachments_dir(100)
            self.assertTrue(any(path.name.startswith("123_requirements") for path in attachments_dir.iterdir()))


class ChunkMessageTests(unittest.TestCase):
    def test_chunk_message_splits_on_newline_when_possible(self) -> None:
        content = "a" * 10 + "\n" + "b" * 10

        chunks = chunk_message(content, max_length=12)

        self.assertEqual(["a" * 10, "b" * 10], chunks)

    def test_chunk_message_splits_long_single_line(self) -> None:
        chunks = chunk_message("x" * 25, max_length=10)

        self.assertEqual(["x" * 10, "x" * 10, "x" * 5], chunks)

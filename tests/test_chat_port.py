from __future__ import annotations

import unittest

from app.chat_port import ChatChannel, ChatPort
from app.testing.in_memory_adapter import InMemoryAdapter


class TestInMemoryAdapterProtocol(unittest.TestCase):
    def test_adapter_satisfies_chat_port_protocol(self) -> None:
        adapter = InMemoryAdapter()
        self.assertIsInstance(adapter, ChatPort)

    def test_channel_satisfies_chat_channel_protocol(self) -> None:
        adapter = InMemoryAdapter()
        channel = adapter.register_channel(100)
        self.assertIsInstance(channel, ChatChannel)


class TestInMemoryAdapterBehavior(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.adapter = InMemoryAdapter()
        self.adapter.register_channel(1)
        self.adapter.register_channel(2)

    async def test_send_message_stores_message(self) -> None:
        channel = self.adapter.get_channel(1)
        assert channel is not None
        await channel.send("hello")
        self.assertEqual(self.adapter.messages_for(1), ["hello"])

    async def test_get_channel_returns_none_for_unknown(self) -> None:
        self.assertIsNone(self.adapter.get_channel(999))

    async def test_messages_for_filters_by_channel(self) -> None:
        ch1 = self.adapter.get_channel(1)
        ch2 = self.adapter.get_channel(2)
        assert ch1 is not None and ch2 is not None
        await ch1.send("msg-ch1")
        await ch2.send("msg-ch2")
        self.assertEqual(self.adapter.messages_for(1), ["msg-ch1"])
        self.assertEqual(self.adapter.messages_for(2), ["msg-ch2"])

    async def test_assert_message_order_validates_sequence(self) -> None:
        channel = self.adapter.get_channel(1)
        assert channel is not None
        await channel.send("first")
        await channel.send("second")
        await channel.send("third")
        self.adapter.assert_message_order(1, ["first", "second", "third"])

    async def test_assert_message_order_raises_on_wrong_order(self) -> None:
        channel = self.adapter.get_channel(1)
        assert channel is not None
        await channel.send("first")
        await channel.send("second")
        with self.assertRaises(AssertionError):
            self.adapter.assert_message_order(1, ["second", "first"])

    async def test_assert_message_contains_raises_on_no_match(self) -> None:
        channel = self.adapter.get_channel(1)
        assert channel is not None
        await channel.send("hello world")
        with self.assertRaises(AssertionError):
            self.adapter.assert_message_contains(1, "nonexistent")

    async def test_assert_message_contains_succeeds(self) -> None:
        channel = self.adapter.get_channel(1)
        assert channel is not None
        await channel.send("hello world")
        self.adapter.assert_message_contains(1, "hello")

    async def test_clear_resets_all_state(self) -> None:
        channel = self.adapter.get_channel(1)
        assert channel is not None
        await channel.send("msg")
        self.adapter.clear()
        self.assertIsNone(self.adapter.get_channel(1))
        self.assertEqual(self.adapter.sent_messages, [])

    def test_channel_url_format(self) -> None:
        channel = self.adapter.get_channel(1)
        assert channel is not None
        self.assertIn("1", channel.channel_url)

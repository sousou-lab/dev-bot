from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SentMessage:
    channel_id: int
    content: str


class InMemoryChatChannel:
    def __init__(self, channel_id: int, adapter: InMemoryAdapter) -> None:
        self._channel_id = channel_id
        self._adapter = adapter

    @property
    def channel_id(self) -> int:
        return self._channel_id

    @property
    def channel_url(self) -> str:
        return f"https://test.local/channels/{self._channel_id}"

    async def send(self, content: str) -> None:
        self._adapter._messages.append(SentMessage(channel_id=self._channel_id, content=content))


@dataclass
class InMemoryAdapter:
    _channels: dict[int, InMemoryChatChannel] = field(default_factory=dict)
    _messages: list[SentMessage] = field(default_factory=list)

    def register_channel(self, channel_id: int) -> InMemoryChatChannel:
        channel = InMemoryChatChannel(channel_id, self)
        self._channels[channel_id] = channel
        return channel

    def get_channel(self, channel_id: int) -> InMemoryChatChannel | None:
        return self._channels.get(channel_id)

    @property
    def sent_messages(self) -> list[SentMessage]:
        return list(self._messages)

    def messages_for(self, channel_id: int) -> list[str]:
        return [m.content for m in self._messages if m.channel_id == channel_id]

    def assert_message_contains(self, channel_id: int, substring: str) -> None:
        messages = self.messages_for(channel_id)
        for msg in messages:
            if substring in msg:
                return
        raise AssertionError(f"No message in channel {channel_id} contains {substring!r}.\nMessages: {messages}")

    def assert_message_order(self, channel_id: int, substrings: list[str]) -> None:
        messages = self.messages_for(channel_id)
        last_index = -1
        for sub in substrings:
            found = False
            for i, msg in enumerate(messages):
                if sub in msg and i > last_index:
                    last_index = i
                    found = True
                    break
            if not found:
                raise AssertionError(
                    f"Expected substring {sub!r} after index {last_index} in channel {channel_id}.\n"
                    f"Messages: {messages}"
                )

    def clear(self) -> None:
        self._channels.clear()
        self._messages.clear()

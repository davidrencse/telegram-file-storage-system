"""Blob backends. Storage talks only to this protocol, so tests run against MemoryBackend
and never touch Telegram."""

from __future__ import annotations

import io
from collections.abc import Iterable
from typing import Protocol

from .errors import NotFound


class Backend(Protocol):
    async def upload(self, data: bytes, filename: str, caption: str = "") -> int:
        """Store a blob, return its message id."""
        ...

    async def download(self, message_id: int) -> bytes: ...

    async def delete(self, message_ids: Iterable[int]) -> None: ...

    async def find_latest(self, caption: str) -> int | None:
        """Newest message id whose caption equals `caption` exactly."""
        ...


class MemoryBackend:
    def __init__(self) -> None:
        self.messages: dict[int, tuple[bytes, str, str]] = {}
        self._next_id = 1
        self.fail_after: int | None = None  # test hook: raise on the Nth upload

    async def upload(self, data: bytes, filename: str, caption: str = "") -> int:
        if self.fail_after is not None:
            if self.fail_after == 0:
                raise ConnectionError("simulated upload failure")
            self.fail_after -= 1
        mid = self._next_id
        self._next_id += 1
        self.messages[mid] = (bytes(data), filename, caption)
        return mid

    async def download(self, message_id: int) -> bytes:
        try:
            return self.messages[message_id][0]
        except KeyError:
            raise NotFound(f"message {message_id}") from None

    async def delete(self, message_ids: Iterable[int]) -> None:
        for mid in message_ids:
            self.messages.pop(mid, None)

    async def find_latest(self, caption: str) -> int | None:
        ids = [mid for mid, (_, _, cap) in self.messages.items() if cap == caption]
        return max(ids, default=None)


class TelegramBackend:
    """Stores each blob as a document message in one chat (Saved Messages by default)."""

    def __init__(self, client, chat: str = "me") -> None:  # client: telethon.TelegramClient
        self.client = client
        self.chat = chat
        self._entity = None

    async def _peer(self):
        if self._entity is None:
            chat: str | int = self.chat
            if isinstance(chat, str) and chat.lstrip("-").isdigit():
                chat = int(chat)
            try:
                self._entity = await self.client.get_input_entity(chat)
            except ValueError:
                # Channel not in the (possibly in-memory) entity cache yet: fetching dialogs populates it.
                await self.client.get_dialogs()
                self._entity = await self.client.get_input_entity(chat)
        return self._entity

    async def upload(self, data: bytes, filename: str, caption: str = "") -> int:
        buf = io.BytesIO(data)
        handle = await self.client.upload_file(buf, file_size=len(data), file_name=filename, part_size_kb=512)
        msg = await self.client.send_file(await self._peer(), handle, caption=caption, force_document=True, silent=True)
        return msg.id

    async def download(self, message_id: int) -> bytes:
        msg = await self.client.get_messages(await self._peer(), ids=message_id)
        if msg is None or msg.media is None:
            raise NotFound(f"message {message_id} is missing from {self.chat}")
        data = await self.client.download_media(msg, file=bytes)
        if data is None:
            raise NotFound(f"message {message_id} has no downloadable media")
        return data

    async def delete(self, message_ids: Iterable[int]) -> None:
        ids = list(message_ids)
        peer = await self._peer()
        for i in range(0, len(ids), 100):  # API limit per call
            await self.client.delete_messages(peer, ids[i : i + 100], revoke=True)

    async def find_latest(self, caption: str) -> int | None:
        async for msg in self.client.iter_messages(await self._peer(), search=caption, limit=50):
            if msg.message == caption and msg.file is not None:
                return msg.id
        return None

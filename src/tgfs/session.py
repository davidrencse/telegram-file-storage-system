"""Wires config -> Telethon client -> Index -> Storage."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from .backend import TelegramBackend
from .config import Config
from .errors import TgfsError
from .index import Index
from .storage import META_SALT, Storage, open_cipher


def make_client(cfg: Config, shared: bool = False):
    """shared=True copies the auth key into memory instead of holding the session file open, so several
    tgfs processes (e.g. `mount` + `ui`) can run at once without SQLite "database is locked" errors."""
    from telethon import TelegramClient
    from telethon.sessions import SQLiteSession, StringSession

    if not cfg.api_id or not cfg.api_hash:
        raise TgfsError("missing api_id/api_hash — run `tgfs login` (get them at https://my.telegram.org)")
    cfg.ensure_home()
    session: str | StringSession = str(cfg.session_path)
    if shared:
        on_disk = SQLiteSession(str(cfg.session_path))
        try:
            session = StringSession(StringSession.save(on_disk))
        finally:
            on_disk.close()
    return TelegramClient(
        session,
        cfg.api_id,
        cfg.api_hash,
        flood_sleep_threshold=300,  # auto-sleep through FloodWait up to 5 min instead of raising
        device_model="tgfs",
    )


def secure_session_file(cfg: Config) -> None:
    session = cfg.session_path.with_suffix(".session")
    if session.exists():
        os.chmod(session, 0o600)


@asynccontextmanager
async def open_storage(
    cfg: Config,
    passphrase: Callable[[bool], str] | None = None,
    with_cipher: bool = True,
    shared: bool = False,
) -> AsyncIterator[Storage]:
    """`passphrase(first_time)` is called lazily, only when encryption is enabled. `first_time` is True when
    no key exists yet, i.e. whatever it returns becomes the permanent key — callers should confirm it."""
    client = make_client(cfg, shared=shared)
    await client.connect()
    index: Index | None = None
    try:
        if not await client.is_user_authorized():
            raise TgfsError("not logged in — run `tgfs login`")
        secure_session_file(cfg)
        index = Index(cfg.db_path)
        cipher = None
        if cfg.encrypt and with_cipher:
            if passphrase is None:
                raise TgfsError("encryption enabled but no passphrase source")
            first_time = index.get_meta(META_SALT) is None
            cipher = open_cipher(index, passphrase(first_time))
        yield Storage(index, TelegramBackend(client, cfg.chat), cfg.chunk_size, cipher)
    finally:
        if index is not None:
            index.close()
        await client.disconnect()

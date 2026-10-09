"""Configuration: env vars override `<home>/config.json`, which `tgfs login` writes."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

DEFAULT_CHUNK_SIZE = 64 * 1024 * 1024  # 64 MiB: well under Telegram's 2 GiB/file cap, cheap to cache
DEFAULT_PARALLEL = 2  # chunks in flight per file: overlaps disk/crypto with network, gentle on rate limits
MAX_PARALLEL = 8
MAX_CHUNK_SIZE = 2000 * 1024 * 1024  # Telegram's per-document cap (non-Premium), minus headroom


def default_home() -> Path:
    if env := os.environ.get("TGFS_HOME"):
        return Path(env).expanduser()
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "tgfs"


@dataclass
class Config:
    home: Path
    api_id: int | None = None
    api_hash: str | None = None
    # "me" = Saved Messages. Can also be a channel id (-100...) or @username.
    chat: str = "me"
    chunk_size: int = DEFAULT_CHUNK_SIZE
    encrypt: bool = False
    parallel: int = DEFAULT_PARALLEL

    @property
    def config_path(self) -> Path:
        return self.home / "config.json"

    @property
    def session_path(self) -> Path:
        # Telethon appends ".session". This file grants FULL access to the account.
        return self.home / "account"

    @property
    def db_path(self) -> Path:
        return self.home / "index.sqlite3"

    @property
    def cache_dir(self) -> Path:
        return self.home / "cache"

    @classmethod
    def load(cls, home: Path | None = None) -> Config:
        home = home or default_home()
        cfg = cls(home=home)
        if cfg.config_path.exists():
            data = json.loads(cfg.config_path.read_text())
            for key in ("api_id", "api_hash", "chat", "chunk_size", "encrypt", "parallel"):
                if key in data:
                    setattr(cfg, key, data[key])
        env = os.environ
        if v := env.get("TGFS_API_ID"):
            cfg.api_id = int(v)
        if v := env.get("TGFS_API_HASH"):
            cfg.api_hash = v
        if v := env.get("TGFS_CHAT"):
            cfg.chat = v
        if v := env.get("TGFS_CHUNK_SIZE"):
            cfg.chunk_size = int(v)
        if v := env.get("TGFS_PARALLEL"):
            cfg.parallel = int(v)
        cfg.validate()
        return cfg

    def validate(self) -> None:
        if not 0 < self.chunk_size <= MAX_CHUNK_SIZE:
            raise ValueError(f"chunk_size must be in 1..{MAX_CHUNK_SIZE} bytes, got {self.chunk_size}")
        if not 1 <= self.parallel <= MAX_PARALLEL:
            raise ValueError(f"parallel must be in 1..{MAX_PARALLEL}, got {self.parallel}")

    def ensure_home(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.cache_dir.mkdir(exist_ok=True, mode=0o700)

    def save(self) -> None:
        self.ensure_home()
        data = {k: v for k, v in asdict(self).items() if k != "home"}
        tmp = self.config_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.chmod(0o600)
        tmp.replace(self.config_path)

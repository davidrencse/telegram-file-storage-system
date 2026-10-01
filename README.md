# tgfs — Telegram as a file system

Store files in your own Telegram account. Files are chunked, optionally encrypted on your machine,
and kept as documents in *Saved Messages* (or a private channel). Use it via CLI or mount it with FUSE.

## Install

```bash
uv sync --all-extras          # needs libfuse3 for `mount` (Arch: pacman -S fuse3)
```

## Log in

1. Create an app at <https://my.telegram.org/apps> → copy `api_id` and `api_hash`.
2. `uv run tgfs login` → enter them, your phone number, the login code, and 2FA password if set.
   Choose encryption when asked (recommended). Optional: `--chat -100123…` to use a private channel.

> The session file (`~/.local/share/tgfs/account.session`) gives full access to your account.
> Keep it private. With encryption on, losing your passphrase means losing your data.

## Use

```bash
export TGFS_PASSPHRASE=...        # optional; otherwise prompted
tgfs put report.pdf /docs/        # upload
tgfs put -r ~/photos /            # upload directory
tgfs ls -l /docs
tgfs get /docs/report.pdf .       # download (sha256 verified)
tgfs mv /docs/report.pdf /archive/2026.pdf
tgfs rm -r /photos                # also deletes the Telegram messages
tgfs info                         # account, usage
tgfs mount ~/tg                   # FUSE mount; unmount with Ctrl-C or fusermount3 -u ~/tg
```

### Web UI

```bash
tgfs ui        # opens http://127.0.0.1:8765 in your browser
```

Drag and drop files or whole folders, create folders, rename, move, delete, download, and preview
images/PDFs/text/video. It only listens on your own machine and the link contains a one-time secret.
It can run at the same time as `tgfs mount`.

The index (names, layout) lives in `~/.local/share/tgfs/index.sqlite3` and is snapshotted to the chat
after each change. On a new machine: `tgfs login` then `tgfs restore`.

## Config

Env vars override `config.json`: `TGFS_HOME`, `TGFS_API_ID`, `TGFS_API_HASH`, `TGFS_CHAT`,
`TGFS_CHUNK_SIZE`, `TGFS_PASSPHRASE`.

## Develop

```bash
uv run pytest -q
uv run ruff check --fix src tests && uv run ruff format src tests
```

`scripts/validate.sh` runs the full pipeline (lint, tests, auditor e2e, hook smoke; `--live` adds a real
Claude run). Every Claude Code session in this repo is audited and redone in the background — see AGENTS.md §13.

See [AGENTS.md](AGENTS.md) for architecture, invariants and contributor/agent guidance.
# telegram-file-storage-system

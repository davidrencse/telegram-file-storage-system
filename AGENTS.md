# AGENTS.md — tgfs (Telegram File Storage)

Single source of truth for any AI agent (Claude Code, Codex, Cursor, …) working in this repo.
Read this fully before changing code. Keep it updated when architecture, commands, or invariants change.

## 1. What this project is

`tgfs` turns a **personal Telegram user account** into a file storage backend.

- Files are split into chunks (default 64 MiB), optionally encrypted client-side (AES-256-GCM),
  and uploaded as **document messages** into one chat (default: *Saved Messages* = `"me"`; can be a private channel).
- A local **SQLite index** maps the directory tree → files → ordered chunk message ids.
- Interfaces: a **CLI** (`tgfs put/get/ls/rm/mv/mkdir/stat/backup/restore/info`) and a **FUSE mount** (`tgfs mount`).
- Uses **MTProto via Telethon** (user account, not the Bot API) → 2 GiB per-message upload limit (4 GiB Premium),
  no 50 MB bot limit, unlimited total storage.

Owner: David Ren. Status: v0.1 working; tests green; kernel FUSE mount verified with in-memory backend.
Not yet verified against a live Telegram account in CI (needs real credentials) — see §9.

## 2. Tech stack

| Concern | Choice | Notes |
|---|---|---|
| Language | Python ≥ 3.12 | venv is 3.12 (`.python-version`); PEP 695 generics OK |
| Packaging | `uv` + `uv_build` | `src/` layout, entry point `tgfs = tgfs.cli:main` |
| Telegram | `telethon` 1.x | async MTProto client; `cryptg` extra speeds up its crypto |
| Crypto | `cryptography` | scrypt KDF (n=2^15,r=8,p=1) + AES-GCM |
| CLI | `typer` + `rich` | progress bars on stderr |
| FUSE | `mfusepy` (extra `fuse`) | libfuse3 via ctypes; fusepy-compatible `Operations` API |
| Web UI | `starlette` + `uvicorn` | JSON API + one static vanilla-JS page (`web/static/index.html`), no build step |
| Tests | `pytest` | no network; `MemoryBackend` fakes Telegram |
| Lint/format | `ruff` | line length 120, rules E,F,I,B,UP,SIM |

## 3. Commands

```bash
uv sync --all-extras              # install everything (fuse + fast + dev)
uv run pytest -q                  # full test suite (~0.5 s, no network)
uv run ruff check --fix src tests session_auditor && uv run ruff format src tests session_auditor
scripts/validate.sh               # full local pipeline (lint, tgfs, auditor unit+e2e, hook smoke); --live adds real claude
uv run tgfs --help                # CLI
uv run tgfs login                 # interactive: api_id/api_hash, phone, code, 2FA
uv run tgfs mount ~/tg            # FUSE mount (foreground); unmount: fusermount3 -u ~/tg
uv run tgfs ui                    # web UI at http://127.0.0.1:8765/#t=<token>; can run alongside mount
```

Always run **ruff + pytest** before declaring work done.

## 4. Layout & responsibilities

```
src/tgfs/
  errors.py    TgfsError hierarchy; each class carries an `errno` (FUSE maps it to OS errors)
  config.py    Config dataclass. Precedence: env vars > <home>/config.json > defaults. Paths for session/db/cache
  crypto.py    Cipher (AES-GCM, blob = nonce12||ct||tag16), derive_key (scrypt), key_check/verify
  index.py     Index (SQLite). Namespace + chunk map + meta. Pure metadata, no network
  backend.py   Backend Protocol; MemoryBackend (tests); TelegramBackend (Telethon)
  storage.py   Storage: chunking, encryption, upload/download, rollback, index backup/restore; open_cipher()
  session.py   make_client(), open_storage() async ctx manager: config → Telethon → Index → Storage
  fusefs.py    TgfsFuse(Operations): FUSE callbacks, LRU chunk cache, temp-file write buffering
  cli.py       Typer app; _run() wraps each command (opens storage, maps TgfsError → exit 1, auto index backup)
  web/app.py   create_app(): Starlette JSON API over Storage (ls/info/jobs/file PUT+GET/mkdir/mv/rm/backup),
               token + Host guard, debounced index backup, upload job progress
  web/static/index.html   the whole UI (vanilla JS/CSS, light+dark, drag-drop files & folders)
  system.py    OS glue via standard tools: Tailscale status (`tailscale status --json`), keyring (`secret-tool`),
               systemd user units for `tgfs autostart` (tgfs-ui.service, tgfs-mount.service)
tests/
  test_storage.py  index + storage behaviour incl. crypto, rollback, backup/restore
  test_fusefs.py   FUSE ops driven directly (no kernel mount)
  test_web.py      web API via Starlette TestClient over MemoryBackend
  test_cli.py      passphrase prompting (first-time confirmation)
  session_auditor/ auditor tests (unit, e2e with fake agent, opt-in live); see §13
session_auditor/   stdlib-only session auditor (not part of the tgfs package); see §13
scripts/validate.sh, .github/workflows/ci.yml   validation pipelines
```

Dependency direction (keep it acyclic): `cli → session → storage → {index, backend, crypto} → errors/config`.
`fusefs → storage`, `web → storage` (both front-ends; `cli` may import them). **`index.py` and `storage.py` must
never import Telethon** (keeps tests offline). Adding a module → update `ALLOWED_DEPS` in `session_auditor/checks.py`.

## 5. Data model

SQLite at `<home>/index.sqlite3` (`<home>` = `$TGFS_HOME` or `$XDG_DATA_HOME/tgfs` or `~/.local/share/tgfs`):

- `nodes(id, parent_id→nodes ON DELETE CASCADE, name, is_dir, size, mtime, sha256, UNIQUE(parent_id,name))`
  — root is `id=1`, `parent_id NULL`, `name ''`.
- `chunks(node_id→nodes CASCADE, idx, message_id, size /*plaintext*/, encrypted, PK(node_id,idx))`
- `meta(key, value)` — `kdf_salt` (16 B), `key_check` (encrypted constant to verify passphrase).

Telegram chat contents:
- chunk messages: document named `<random16hex>.bin`, no caption (never leak real names).
- index snapshot: document `tgfs-index.bin`, caption `#tgfs-index`; format
  `b"TGFSIDX1" + flag(0|1) + [salt16 if flag=1] + (sqlite bytes | AES-GCM(sqlite bytes))`. Only newest kept.

## 6. Invariants — do not break

1. **No orphan blobs.** Index mutators (`put_file`, `remove`, `move`) return orphaned message ids; `Storage`
   must delete them from Telegram. `Storage.put` deletes already-uploaded chunks if anything fails
   (including `BaseException` / Ctrl-C).
2. **Validate before upload.** `put` checks the destination parent exists *before* sending bytes.
3. **Integrity.** `read_chunk` checks plaintext length; `get` checks full-file sha256 and writes to
   `*.tgfs-part` then atomically renames. Never leave partial files at the destination.
4. **Encryption never downgrades.** If `config.encrypt` is on, any command that uploads the index must have the
   cipher (`_run(..., backup=True)` forces `with_cipher=True`). A plaintext index would leak all file names.
5. **Secrets.** `<home>/account.session` = full account takeover if leaked. Kept `0600`, home dir `0700`,
   gitignored. Never log/print api_hash, session contents, or passphrase. Passphrase is never stored.
6. **Index is authoritative.** Telegram only holds opaque blobs. Losing the index without a backup = losing
   names/layout. Mutating CLI commands auto-run `backup_index()`; FUSE backs up on unmount.
7. **Thread model.** One Telethon client lives on one asyncio loop. FUSE runs on its own threads and submits
   coroutines with `run_coroutine_threadsafe`. SQLite connection is shared (`check_same_thread=False`) and
   guarded by `Index._lock` (RLock). Never call Telethon from a non-loop thread directly.
8. **Paths** are always normalized through `index.split_path` / `join_path` (POSIX, `..` clamped at root).

## 7. Telegram specifics to remember

- Credentials: `api_id` / `api_hash` from https://my.telegram.org/apps (per developer, not secret-grade but private).
- Limits: 2 GiB per file (4 GiB Premium) → chunking keeps us far below. Rate limits surface as `FloodWaitError`;
  client uses `flood_sleep_threshold=300` so waits ≤5 min are slept automatically.
- `delete_messages` max 100 ids per call (batched in `TelegramBackend.delete`).
- Saved Messages is simplest; a private channel (`--chat -100…`) isolates storage from personal notes.
- Cloud chats are **not** E2E encrypted — Telegram can read unencrypted chunks. Recommend `encrypt=true`.
- Heavy automated use of a user account can trigger anti-spam limits; keep uploads sequential-ish.

## 8. Conventions

- Match surrounding style: type hints everywhere, `from __future__ import annotations`, dataclasses,
  small focused functions, comments only for *why*.
- New error cases → subclass `TgfsError` with a proper `errno`.
- New backend feature → add to `Backend` Protocol **and** `MemoryBackend` **and** `TelegramBackend`.
- Every behaviour change gets a test using `MemoryBackend`. FUSE changes: add to `tests/test_fusefs.py`;
  for kernel-level checks mount a `TgfsFuse` over `MemoryBackend` in a scratch dir (see §9).
- Schema change → bump via `meta` key `schema_version` + migration in `Index._init` (not yet needed; v1 implicit).
- Commits: Conventional Commits (`feat:`, `fix:`, …). Only commit when the user asks.

## 9. Testing notes

- Unit/integration: `uv run pytest -q`.
- Kernel FUSE smoke test (no Telegram): script that builds `Storage(Index(":memory:"), MemoryBackend(), 4096)`,
  runs `FUSE(TgfsFuse(...), mnt, foreground=True)` in background, then `cp`/`cmp`/`mv`/`rm -r` in the mount,
  then `fusermount3 -u`. Note kernel attr cache (~1 s) can briefly show stale mtimes.
- Live Telegram test: manual only (`tgfs login` then `put`/`get`/`cmp`). Never automate with the user's account
  without asking.

## 9a. Web UI rules

- `tgfs ui` binds **127.0.0.1 only**. Per-run random token: delivered in the URL *fragment* (`/#t=`), stored in
  `sessionStorage`, sent as `X-Tgfs-Token` (or `?t=` for download links). Host header must be local (anti DNS-rebinding).
- Downloads never render active content: `_preview_type` only inlines images (not SVG), audio, video, PDF, and
  text-as-`text/plain`; everything else is `attachment`. CSP `default-src 'none'` on every file response.
- Uploads stream to a temp file in `<home>/cache`, then `Storage.put`; progress is exposed at `/api/jobs`.
  409 on existing path unless `overwrite=1`; `mkdirs=1` creates parents (folder uploads).
- Index backup is debounced (`BACKUP_DELAY` = 3 s) and flushed on shutdown.
- `ui --tailscale` binds the Tailscale IPv4 (100.64/10) **and** 127.0.0.1, never 0.0.0.0; Tailscale IP + MagicDNS
  names are added to the Host allowlist. Token still required. `--keep-token` persists it in `<home>/ui-token` (0600)
  so phone bookmarks survive restarts.
- Passphrase sources, in order: `TGFS_PASSPHRASE` → keyring (`tgfs keyring-save`) → prompt. No TTY → actionable error.
- `ui` uses `open_storage(shared=True)` → in-memory copy of the session so it can run while `mount` holds the file.
- FUSE uploads happen in `flush` (close() waits for it), not `release` (async) — otherwise `cp` returns early.
- FUSE open-for-write handles live in `_pending` keyed by path: `rename` re-points them, `unlink` marks them
  `unlinked` (never uploaded), `open(O_TRUNC)` marks dirty (so `: > f` persists), `truncate` on a missing path is ENOENT.
- Passphrase: `cli._unlock()` verifies it against the local index (3 attempts) *before* connecting; `_Passphrase`
  remembers the verified value for the process. Never let a passphrase error surface as a traceback.

## 10. Known limitations / roadmap

- Uploads/downloads are sequential per file (simple, gentle on rate limits). Parallel chunk transfer = speedup.
- No dedup (same content uploaded twice = two copies). Could key chunks by sha256 with refcounts.
- FUSE writes buffer the whole file in `<home>/cache` and upload on `close()`; random writes to huge files are costly.
- No `gc` to find chat messages not referenced by the index (e.g. after a crash between upload and index commit).
- Single-writer: two machines mutating the same chat concurrently will diverge (last index backup wins).
- No symlinks, permissions, xattrs (chmod/chown are accepted no-ops).
- Web UI: no folder-as-zip download, no HTTP Range (video seeking in preview), no upload resume.
- Encrypted chunks are not bound to their file/position (no AEAD associated data). `get` catches swapped chunks via
  the file sha256, but FUSE/web reads don't. Exploiting it needs write access to your chat (= account compromise).
  Fix needs a per-file id in the schema → AAD = file_id‖idx.

## 11. Skills & agents to use for this project

Scouted from the available skill list. Use these; ignore the rest (Vercel/Next.js/marketplace skills are
**not relevant** — this is a local Python CLI, not a web app; `vercel:chat-sdk` targets bots, we use a user account).

| When | Skill / agent |
|---|---|
| Designing a new subsystem (dedup, parallel transfer, gc, multi-device sync) | `engineering:system-design`, `engineering:architecture` (write an ADR) |
| Planning tests for new behaviour | `engineering:testing-strategy` |
| Bug / stack trace / FloodWait / FUSE weirdness | `engineering:debug` |
| Before merging anything touching crypto, session, or deletion paths | `security-review`, `code-review` (or `engineering:code-review`) |
| After a feature lands, to tighten code | `simplify` |
| Running/trying the CLI or mount end-to-end | `run` |
| README / runbooks / docs updates | `engineering:documentation` |
| Commit messages | `caveman:caveman-commit` (Conventional Commits) |
| Locating code cheaply / small edits / diff review | agents `caveman:cavecrew-investigator`, `caveman:cavecrew-builder`, `caveman:cavecrew-reviewer` |
| Tech-debt pass | `engineering:tech-debt` |

## 12. Glossary

- **chunk** — ≤ `chunk_size` slice of a file, stored as one Telegram document message.
- **node** — row in `nodes`; a file or directory.
- **orphaned ids** — message ids no longer referenced by the index; must be deleted from Telegram.
- **home** — local state dir (config.json, account.session, index.sqlite3, cache/).

## 13. Session auditor (every Claude session here is audited)

`.claude/settings.json` registers `python3 -m session_auditor hook` on **SessionStart** and **SessionEnd**.

1. **SessionStart** snapshots the working tree as a git tree object (temp `GIT_INDEX_FILE`; the user's index/HEAD
   are never touched) → `.claude/audits/<sid>/session.json`.
2. **SessionEnd** snapshots again and spawns a detached `python3 -m session_auditor run <sid>`:
   transcript (`~/.claude/projects/…/<sid>.jsonl`) → digest; baseline→final diff; **process checks** (verification
   discipline, tests-for-src, unrequested commits, destructive cmds, live `tgfs`, secrets, retry loops, irrelevant
   skills, docs drift) and **tree checks** (§4 import direction, Telethon boundary, Backend parity, syntax);
   gates (`pytest --all-extras`, `ruff check .`, `ruff format --check .`) on the exported final tree.
3. If the session changed files, headless `claude -p` (model/budget in `.claude/auditor.json`, `--permission-mode
   dontAsk`, allow-list without git writes/network/`tgfs`, user-level settings skipped) runs in a scratch worktree at
   the **baseline** (`~/.cache/session-auditor/…` — never under `.claude/`, which headless Claude refuses to edit) with
   inputs in `.audit/`. It writes `critique.md/json`, redoes the task, runs gates, writes `redo_notes.md`.
4. The redo is committed to `refs/audits/<sid>/redo` (plus `…/baseline`, `…/original`; no branches), re-scored with
   the same tree checks + gates, and compared → verdict `redo-better | equivalent | original-better | redo-empty |
   agent-failed | agent-blocked | static-only`. Report: `.claude/audits/<sid>/report.md`.

Recursion guard: every spawned process gets `SESSION_AUDITOR_ACTIVE=1`; the hook exits immediately when it is set.
Hooks never fail a session (errors → `.claude/audits/hook.log`, exit 0).

```bash
python3 -m session_auditor list                    # verdicts + scores
python3 -m session_auditor show <sid>              # report
python3 -m session_auditor sweep                   # audit sessions whose SessionEnd never fired
python3 -m session_auditor audit --transcript X.jsonl --baseline <rev|empty> [--final <rev>] [--no-agent]
git diff refs/audits/<sid>/original refs/audits/<sid>/redo
AUDITOR_ENABLED=0 claude                           # opt out for one session; or "enabled": false in auditor.json
```

When changing AGENTS.md invariants, update `session_auditor/checks.py` (and its tests) to match.

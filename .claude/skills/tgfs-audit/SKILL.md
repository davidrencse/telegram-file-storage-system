---
name: tgfs-audit
description: Audit tgfs for speed, reliability, flexibility and security, then implement the highest-value fixes. Use when asked to "audit", "review the file system", "harden", or "speed up" tgfs.
---

# tgfs audit

Read `AGENTS.md` first; its §6 invariants are the acceptance criteria for every fix.

## Checklist

**Speed**
- Per-file transfers: are chunks pipelined (`Storage.parallel`, `iter_chunks`) or strictly sequential?
- Is CPU work (AES-GCM, sha256 of large buffers) off the event loop (`asyncio.to_thread`)?
- FUSE: LRU size vs chunk size; read-ahead on sequential reads.
- Web: downloads stream via `iter_chunks` (prefetch), never buffer a whole file.

**Reliability**
- No orphan blobs on *any* failure path, including in-flight parallel uploads (await, don't cancel).
- `get` writes `*.tgfs-part` + sha256 check + atomic rename.
- Config values validated at load (`Config.validate`), errors reported without traceback.

**Security**
- Encryption never downgrades: uploads of the index and *restores* (`require_encrypted`).
- Names from a restored index are untrusted (path traversal in `get_tree`).
- Secrets file modes (0600/0700), no secrets in logs; web token/Host guard, CSP on file responses.

**Flexibility**
- New knobs go in `Config` (json + `TGFS_*` env) with bounds checks and a test.

## Workflow
1. Read `src/tgfs/{storage,backend,crypto,config,fusefs}.py` and `web/app.py`.
2. List findings by severity; pick the ones fixable without schema changes.
3. Every change gets a `MemoryBackend` test (use a backend that `await`s to exercise concurrency).
4. `uv run ruff check . && uv run ruff format --check . && uv run pytest -q` before committing.
5. Update AGENTS.md (§2/§10) and `session_auditor/checks.py` if invariants or module deps change.

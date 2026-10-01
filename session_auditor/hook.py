"""Claude Code hook entry point (SessionStart / SessionEnd). Must be fast and must never break a session:
every failure is logged to `.claude/audits/hook.log` and the hook still exits 0."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import traceback
from pathlib import Path

from . import gitops
from .config import GUARD_ENV, AuditorConfig, audits_dir
from .pipeline import SessionState


def _log(repo: Path, msg: str) -> None:
    try:
        d = audits_dir(repo)
        d.mkdir(parents=True, exist_ok=True)
        with open(d / "hook.log", "a") as fh:
            fh.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {msg}\n")
    except OSError:
        pass


def _valid_sid(sid: str) -> bool:
    return bool(sid) and all(c.isalnum() or c in "-_" for c in sid) and len(sid) <= 128


def spawn_audit(repo: Path, sid: str) -> subprocess.Popen:
    """Detached so the user's session exits immediately; the audit runs in the background."""
    out = audits_dir(repo) / sid
    out.mkdir(parents=True, exist_ok=True)
    log = open(out / "run.log", "ab")  # noqa: SIM115 — handed to the detached child
    pkg_root = Path(__file__).resolve().parent.parent
    env = {**os.environ, GUARD_ENV: "1", "PYTHONPATH": str(pkg_root)}
    return subprocess.Popen(
        [sys.executable, "-m", "session_auditor", "run", sid],
        cwd=repo,
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
        env=env,
        start_new_session=True,
    )


def handle(payload: dict, *, spawn=spawn_audit) -> str:
    """Returns a short status string (used by tests and logged)."""
    if os.environ.get(GUARD_ENV):
        return "skip:guard"
    event = payload.get("hook_event_name", "")
    sid = str(payload.get("session_id", ""))
    if not _valid_sid(sid):
        return "skip:bad-session-id"
    repo = gitops.repo_root(Path(payload.get("cwd") or os.getcwd()))
    cfg = AuditorConfig.load(repo)
    if not cfg.enabled:
        return "skip:disabled"
    state_path = audits_dir(repo) / sid / "session.json"

    if event == "SessionStart":
        if state_path.exists():
            # resume / compact / clear-into-same-id: keep the original baseline
            return "start:kept-baseline"
        SessionState(
            session_id=sid,
            transcript_path=str(payload.get("transcript_path", "")),
            baseline_tree=gitops.snapshot_tree(repo),
            baseline_head=gitops.head_commit(repo),
            started_at=time.time(),
        ).save(state_path)
        return "start:baseline"

    if event == "SessionEnd":
        if state_path.exists():
            state = SessionState.load(state_path)
        else:
            # Hook was installed mid-session: best effort, baseline = HEAD (or empty repo).
            head = gitops.head_commit(repo)
            state = SessionState(
                sid, baseline_tree=gitops.resolve_tree(repo, head) if head else gitops.EMPTY_TREE, baseline_head=head
            )
            _log(repo, f"{sid}: no baseline recorded, falling back to HEAD")
        state.transcript_path = str(payload.get("transcript_path", "")) or state.transcript_path
        state.final_tree = gitops.snapshot_tree(repo)
        state.ended_at = time.time()
        state.end_reason = str(payload.get("reason", ""))
        state.save(state_path)
        spawn(repo, sid)
        return "end:spawned"

    return f"skip:event:{event}"


def main() -> int:
    raw = sys.stdin.read()
    repo = Path(os.getcwd())
    try:
        payload = json.loads(raw or "{}")
        repo = Path(payload.get("cwd") or repo)
        status = handle(payload)
        _log(repo, f"{payload.get('session_id', '?')}: {status}")
    except Exception:
        _log(repo, "hook error:\n" + traceback.format_exc())
    return 0

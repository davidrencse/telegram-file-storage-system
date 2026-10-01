from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from conftest import fast_config, write_config

from session_auditor import gitops
from session_auditor.config import GUARD_ENV
from session_auditor.hook import handle
from session_auditor.pipeline import SessionState

ROOT = Path(__file__).resolve().parents[2]


def payload(repo: Path, event: str, sid: str = "sess-1", **extra) -> dict:
    return {"hook_event_name": event, "session_id": sid, "cwd": str(repo), "transcript_path": "/t.jsonl", **extra}


def state(repo: Path, sid: str = "sess-1") -> SessionState:
    return SessionState.load(repo / ".claude/audits" / sid / "session.json")


def test_start_records_baseline_and_end_spawns(mini_repo):
    spawned = []
    assert handle(payload(mini_repo, "SessionStart"), spawn=lambda r, s: spawned.append(s)) == "start:baseline"
    st = state(mini_repo)
    assert st.baseline_tree == gitops.resolve_tree(mini_repo, "HEAD")
    assert st.baseline_head == gitops.head_commit(mini_repo)

    (mini_repo / "src/tgfs/new.py").write_text("x = 1\n")
    assert (
        handle(payload(mini_repo, "SessionEnd", reason="exit"), spawn=lambda r, s: spawned.append(s)) == "end:spawned"
    )
    st = state(mini_repo)
    assert st.final_tree != st.baseline_tree and st.end_reason == "exit"
    assert spawned == ["sess-1"]


def test_resume_keeps_original_baseline(mini_repo):
    handle(payload(mini_repo, "SessionStart"))
    first = state(mini_repo).baseline_tree
    (mini_repo / "a.py").write_text("")
    assert handle(payload(mini_repo, "SessionStart", source="resume")) == "start:kept-baseline"
    assert state(mini_repo).baseline_tree == first


def test_guard_disabled_and_bad_ids(mini_repo, monkeypatch):
    write_config(mini_repo, fast_config(enabled=False))
    assert handle(payload(mini_repo, "SessionStart")) == "skip:disabled"
    assert handle(payload(mini_repo, "SessionStart", sid="../../etc")) == "skip:bad-session-id"
    assert handle(payload(mini_repo, "SessionStart", sid="")) == "skip:bad-session-id"
    monkeypatch.setenv(GUARD_ENV, "1")
    assert handle(payload(mini_repo, "SessionStart")) == "skip:guard"
    assert not (mini_repo / ".claude/audits/sess-1").exists()


def test_end_without_start_falls_back_to_head(mini_repo):
    (mini_repo / "b.py").write_text("")
    assert handle(payload(mini_repo, "SessionEnd"), spawn=lambda r, s: None) == "end:spawned"
    st = state(mini_repo)
    assert st.baseline_tree == gitops.resolve_tree(mini_repo, "HEAD")
    assert "b.py" in gitops.list_files(mini_repo, st.final_tree)


def test_hook_process_never_fails(mini_repo, tmp_path):
    """Garbage on stdin must still exit 0 (a failing hook would disrupt the user's session)."""
    for stdin in (
        "not json",
        "",
        json.dumps({"hook_event_name": "SessionStart", "session_id": "x", "cwd": "/nonexistent"}),
    ):
        proc = subprocess.run(
            [sys.executable, "-m", "session_auditor", "hook"],
            input=stdin,
            text=True,
            cwd=mini_repo,
            env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(ROOT)},
            capture_output=True,
            timeout=30,
        )
        assert proc.returncode == 0, proc.stderr
    assert "hook error" in (mini_repo / ".claude/audits/hook.log").read_text()


def test_hook_command_from_settings_runs_under_system_python(mini_repo):
    """Exercise the exact command string from .claude/settings.json with $CLAUDE_PROJECT_DIR set."""
    settings = json.loads((ROOT / ".claude/settings.json").read_text())
    cmd = settings["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    # point the hook's `cd` at the auditor source but the payload cwd at the scratch repo
    proc = subprocess.run(
        ["bash", "-c", cmd],
        input=json.dumps(payload(mini_repo, "SessionStart", sid="settings-cmd")),
        text=True,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin", "CLAUDE_PROJECT_DIR": str(ROOT)},
        capture_output=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert state(mini_repo, "settings-cmd").baseline_tree

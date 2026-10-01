"""End-to-end: real git repo, real pytest/ruff gates, real subprocess agent (fake_agent.py), real hooks."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from conftest import TranscriptBuilder, fast_config, git, write_config

from session_auditor import gitops
from session_auditor.config import worktrees_dir
from session_auditor.hook import handle, spawn_audit
from session_auditor.pipeline import AuditBusy, SessionState, run_audit, session_lock

pytestmark = pytest.mark.e2e
ROOT = Path(__file__).resolve().parents[2]
BUGGY_JOIN = '\n\ndef join_path(parts: list[str]) -> str:\n    return "/".join(parts)\n'


def simulate_session(repo: Path, tmp_path: Path, sid: str = "s-e2e") -> SessionState:
    """A sloppy session: implements join_path (dropping the leading '/'), no tests, never runs pytest, commits."""
    base = gitops.snapshot_tree(repo)
    idx = repo / "src/tgfs/index.py"
    idx.write_text(idx.read_text() + BUGGY_JOIN)
    tb = TranscriptBuilder(sid, str(repo))
    tb.user("add a join_path(parts) helper to index.py that builds absolute paths")
    tb.tool("Read", {"file_path": str(idx)}, "…")
    tb.edit(str(idx), BUGGY_JOIN)
    tb.bash('git commit -am "feat: join_path"', "")
    tb.say("Done, all tests pass.")
    transcript = tb.write(tmp_path / f"{sid}.jsonl")
    return SessionState(
        sid,
        transcript_path=str(transcript),
        baseline_tree=base,
        baseline_head=gitops.head_commit(repo),
        final_tree=gitops.snapshot_tree(repo),
    )


def load_report(repo: Path, sid: str) -> dict:
    return json.loads((repo / ".claude/audits" / sid / "report.json").read_text())


def test_redo_beats_sloppy_session(mini_repo, tmp_path):
    st = simulate_session(mini_repo, tmp_path)
    head_before = git(mini_repo, "rev-parse", "HEAD")
    status_before = git(mini_repo, "status", "--porcelain")

    res = run_audit(mini_repo, st, fast_config("fix"))
    out = res.out_dir

    assert res.verdict == "redo-better", res.to_dict()
    assert res.agent.ok
    rules = {f.rule for f in res.process_findings}
    assert {"verify-missing", "tests-missing", "unrequested-commit", "unverified-claim"} <= rules
    assert res.original.validation.ok and res.original.validation.tests_passed == 1
    assert res.redo.validation.ok and res.redo.validation.tests_passed == 2
    assert res.critique["score"] == 4 and "process findings seen" in res.critique["summary"]
    for f in (
        "report.md",
        "report.json",
        "critique.md",
        "redo_notes.md",
        "original.diff",
        "redo.diff",
        "original_vs_redo.diff",
        "transcript.md",
        "findings.json",
        "agent.log",
    ):
        assert (out / f).exists(), f
    assert "test_join.py" in (out / "redo.diff").read_text()
    assert ".audit" not in (out / "redo.diff").read_text()  # inputs never leak into the redo commit
    assert "redo-better" in (out / "report.md").read_text()

    # refs exist and describe the three states
    assert gitops.resolve_tree(mini_repo, f"refs/audits/{st.session_id}/baseline") == st.baseline_tree
    assert gitops.resolve_tree(mini_repo, f"refs/audits/{st.session_id}/original") == st.final_tree
    redo_files = gitops.list_files(mini_repo, gitops.resolve_tree(mini_repo, f"refs/audits/{st.session_id}/redo"))
    assert "tests/test_join.py" in redo_files

    # user's checkout untouched; worktree cleaned up
    assert git(mini_repo, "rev-parse", "HEAD") == head_before
    assert git(mini_repo, "status", "--porcelain") == status_before
    assert not (worktrees_dir(mini_repo) / st.session_id).exists()
    assert "worktree" not in git(mini_repo, "worktree", "list").split("\n", 1)[-1]


def test_redo_that_breaks_tests_loses(mini_repo, tmp_path):
    res = run_audit(mini_repo, simulate_session(mini_repo, tmp_path), fast_config("break"))
    assert res.verdict == "original-better"
    assert res.redo.validation.tests_failed == 1


def test_agent_that_changes_nothing_is_flagged(mini_repo, tmp_path):
    assert run_audit(mini_repo, simulate_session(mini_repo, tmp_path), fast_config("noop")).verdict == "redo-empty"


def test_crashing_agent(mini_repo, tmp_path):
    res = run_audit(mini_repo, simulate_session(mini_repo, tmp_path), fast_config("crash"))
    assert res.verdict == "agent-failed"
    assert "boom" in (res.out_dir / "report.md").read_text()
    assert not (worktrees_dir(mini_repo) / "s-e2e").exists()


def test_escape_attempt_does_not_touch_user_repo(mini_repo, tmp_path):
    st = simulate_session(mini_repo, tmp_path)
    res = run_audit(mini_repo, st, fast_config("escape"))
    assert res.verdict == "redo-better"
    # fake agent writes ../../ESCAPED relative to the worktree, which lives outside the repo entirely
    assert "ESCAPED" not in git(mini_repo, "status", "--porcelain")


def test_static_only_mode_is_free(mini_repo, tmp_path):
    res = run_audit(mini_repo, simulate_session(mini_repo, tmp_path), fast_config("crash"), use_agent=False)
    assert res.verdict == "static-only" and res.agent is None and res.redo is None
    assert res.original.validation is not None


def test_readonly_session_skips_agent_unless_configured(mini_repo, tmp_path):
    tb = TranscriptBuilder("ro", str(mini_repo)).user("what does split_path do?").say("It splits.")
    tree = gitops.snapshot_tree(mini_repo)
    st = SessionState("ro", str(tb.write(tmp_path / "ro.jsonl")), tree, gitops.head_commit(mini_repo), tree)
    res = run_audit(mini_repo, st, fast_config("answer"), run_gates=False)
    assert res.verdict == "static-only" and any("no changes" in n for n in res.notes)
    res = run_audit(mini_repo, st, fast_config("answer", audit_readonly_sessions=True), run_gates=False)
    assert res.verdict == "equivalent"
    assert (res.out_dir / "redo_answer.md").read_text() == "The ideal answer.\n"


def test_rerun_overwrites_stale_outputs(mini_repo, tmp_path):
    st = simulate_session(mini_repo, tmp_path)
    run_audit(mini_repo, st, fast_config("fix"), run_gates=False)
    res = run_audit(mini_repo, st, fast_config("crash"), run_gates=False)
    assert res.verdict == "agent-failed"
    assert not (res.out_dir / "critique.md").exists()


def test_lock_prevents_concurrent_audits(mini_repo, tmp_path):
    st = simulate_session(mini_repo, tmp_path)
    with session_lock(mini_repo / ".claude/audits" / st.session_id), pytest.raises(AuditBusy):
        run_audit(mini_repo, st, fast_config("fix"))


def test_first_session_from_empty_repo(tmp_path):
    """Audit the session that created a project (baseline = empty tree), as for this repo's first session."""
    repo = tmp_path / "fresh"
    repo.mkdir()
    git(repo, "init", "-q")
    (repo / "hello.py").write_text("print('hi')\n")
    tb = TranscriptBuilder("genesis", str(repo)).user("write hello").tool("Write", {"file_path": "hello.py"}, "ok")
    st = SessionState(
        "genesis", str(tb.write(tmp_path / "g.jsonl")), gitops.EMPTY_TREE, None, gitops.snapshot_tree(repo)
    )
    res = run_audit(repo, st, fast_config("noop"), run_gates=False)
    assert res.changed_files == 1 and res.verdict == "redo-empty"


def test_cli_audit_and_list_and_show(mini_repo, tmp_path):
    st = simulate_session(mini_repo, tmp_path, sid="cli-sess")
    write_config(mini_repo, fast_config("fix"))
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    base = gitops.commit_tree(mini_repo, st.baseline_tree, "b")
    run = subprocess.run(
        [
            sys.executable,
            "-m",
            "session_auditor",
            "--repo",
            str(mini_repo),
            "audit",
            "--transcript",
            st.transcript_path,
            "--baseline",
            base,
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    assert run.returncode == 0, run.stderr
    assert "cli-sess: redo-better" in run.stdout
    ls = subprocess.run(
        [sys.executable, "-m", "session_auditor", "--repo", str(mini_repo), "list"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert "cli-sess" in ls.stdout and "redo-better" in ls.stdout
    show = subprocess.run(
        [sys.executable, "-m", "session_auditor", "--repo", str(mini_repo), "show", "cli-sess"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert "# Audit" in show.stdout


def test_full_hook_lifecycle_spawns_detached_audit(mini_repo, tmp_path):
    """SessionStart → edits → SessionEnd → background process → report.json appears."""
    write_config(mini_repo, fast_config("fix"))
    sid = "hooked-1"
    tb = TranscriptBuilder(sid, str(mini_repo)).user("add join_path")
    assert handle({"hook_event_name": "SessionStart", "session_id": sid, "cwd": str(mini_repo)}) == "start:baseline"

    idx = mini_repo / "src/tgfs/index.py"
    idx.write_text(idx.read_text() + BUGGY_JOIN)
    tb.edit(str(idx), BUGGY_JOIN).say("done")
    transcript = tb.write(tmp_path / f"{sid}.jsonl")

    procs: list[subprocess.Popen] = []
    status = handle(
        {"hook_event_name": "SessionEnd", "session_id": sid, "cwd": str(mini_repo), "transcript_path": str(transcript)},
        spawn=lambda r, s: procs.append(spawn_audit(r, s)),
    )
    assert status == "end:spawned"
    assert procs[0].wait(timeout=180) == 0, (mini_repo / f".claude/audits/{sid}/run.log").read_text()
    report = load_report(mini_repo, sid)
    assert report["verdict"] == "redo-better"
    assert {"verify-missing", "tests-missing"} <= {f["rule"] for f in report["process_findings"]}


def test_sweep_picks_up_missed_sessions(mini_repo, tmp_path):
    write_config(mini_repo, fast_config("fix"))
    st = simulate_session(mini_repo, tmp_path, sid="missed")
    st.save(mini_repo / ".claude/audits/missed/session.json")
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    t0 = time.monotonic()
    out = subprocess.run(
        [sys.executable, "-m", "session_auditor", "--repo", str(mini_repo), "sweep"],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    assert out.returncode == 0 and "missed: redo-better" in out.stdout, out.stderr
    again = subprocess.run(
        [sys.executable, "-m", "session_auditor", "--repo", str(mini_repo), "sweep"],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert "nothing pending" in again.stdout
    assert time.monotonic() - t0 < 120


FAKE_CLAUDE = """#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
sys.stdin.read()
args = sys.argv[1:]
assert args[args.index("--permission-mode") + 1] == "dontAsk"
mode = os.environ["FAKE_CLAUDE_MODE"]
denials = []
if mode == "blocked":
    denials = [{"tool_name": "Edit", "tool_input": {"file_path": "/x/src/tgfs/index.py"}}]
else:
    Path(".audit/critique.md").write_text("ok")
    Path(".audit/critique.json").write_text(json.dumps({"score": 7, "summary": "s", "issues": []}))
    Path("tests/test_more.py").write_text("def test_more():\\n    assert True\\n")
print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "done",
                  "total_cost_usd": 0.42, "permission_denials": denials}))
"""


@pytest.mark.parametrize(("mode", "verdict"), [("blocked", "agent-blocked"), ("works", "redo-better")])
def test_claude_agent_path_with_fake_binary(mini_repo, tmp_path, monkeypatch, mode, verdict):
    """Exercises the real `claude` argv + JSON parsing path offline via a stub binary on PATH."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "claude").write_text(FAKE_CLAUDE)
    (bindir / "claude").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_CLAUDE_MODE", mode)
    cfg = fast_config()
    cfg.agent, cfg.agent_command = "claude", []
    res = run_audit(mini_repo, simulate_session(mini_repo, tmp_path), cfg)
    assert res.verdict == verdict, res.to_dict()
    assert res.agent.cost_usd == 0.42
    report = (res.out_dir / "report.md").read_text()
    assert "$0.42" in report
    if mode == "blocked":
        assert res.agent.permission_denials == ["Edit: /x/src/tgfs/index.py"]
        assert "permission denials (1)" in report


def test_worktrees_never_inside_dot_claude(tmp_path, monkeypatch):
    monkeypatch.delenv("AUDITOR_WORKTREES")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    wt = worktrees_dir(tmp_path / "proj")
    assert ".claude" not in wt.parts and wt.is_relative_to(tmp_path / "cache" / "session-auditor")

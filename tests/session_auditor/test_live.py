"""Opt-in tests against real data / the real `claude` CLI.

- Real transcripts: parse every session log Claude Code has stored for this project (free, skipped if none).
- Live agent: `AUDITOR_LIVE=1 uv run pytest -m live` runs headless Claude on the mini repo (costs ≤ $1).
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest
from conftest import TranscriptBuilder, fast_config

from session_auditor import gitops
from session_auditor.checks import run_process_checks
from session_auditor.config import AuditorConfig
from session_auditor.pipeline import SessionState, run_audit
from session_auditor.transcript import parse_transcript, render_digest

ROOT = Path(__file__).resolve().parents[2]
PROJECT_LOGS = Path.home() / ".claude" / "projects" / str(ROOT).replace("/", "-")
REAL = sorted(PROJECT_LOGS.glob("*.jsonl")) if PROJECT_LOGS.is_dir() else []


@pytest.mark.skipif(not REAL, reason="no stored transcripts for this project")
@pytest.mark.parametrize("path", REAL, ids=[p.stem[:8] for p in REAL])
def test_real_transcripts_parse(path):
    rec = parse_transcript(path)
    assert rec.malformed_lines == 0
    assert rec.prompts, "every real session has at least one human prompt"
    assert all(c.id for c in rec.tool_calls)
    answered = [c for c in rec.tool_calls if c.result is not None]
    assert len(answered) >= len(rec.tool_calls) - 5  # only in-flight calls at snapshot time lack results
    digest = render_digest(rec)
    assert rec.prompts[0].text[:40] in digest
    run_process_checks(rec, [])  # must not raise on real data


@pytest.mark.live
@pytest.mark.skipif(os.environ.get("AUDITOR_LIVE") != "1", reason="set AUDITOR_LIVE=1 to call the real claude CLI")
@pytest.mark.skipif(shutil.which("claude") is None, reason="claude CLI not installed")
def test_live_claude_redo(mini_repo, tmp_path):
    base = gitops.snapshot_tree(mini_repo)
    idx = mini_repo / "src/tgfs/index.py"
    idx.write_text(idx.read_text() + '\n\ndef join_path(parts: list[str]) -> str:\n    return "/".join(parts)\n')
    tb = TranscriptBuilder("live", str(mini_repo)).user("add join_path(parts) returning an absolute path")
    tb.edit(str(idx)).say("Done.")
    st = SessionState(
        "live",
        str(tb.write(tmp_path / "live.jsonl")),
        base,
        gitops.head_commit(mini_repo),
        gitops.snapshot_tree(mini_repo),
    )
    cfg = fast_config()
    cfg.agent, cfg.agent_command = "claude", []
    cfg.model = os.environ.get("AUDITOR_MODEL", "sonnet")
    cfg.effort, cfg.max_budget_usd, cfg.timeout_s = "low", 1.0, 900
    cfg.allowed_tools = [*AuditorConfig().allowed_tools, "Bash(python -m pytest *)", "Bash(python3 -m pytest *)"]
    res = run_audit(mini_repo, st, cfg)
    assert res.agent.ok, res.agent.error
    assert (res.out_dir / "critique.md").exists()
    assert res.critique is not None and 0 <= res.critique["score"] <= 10
    assert res.redo_changed_files >= 1
    assert res.verdict in {"redo-better", "equivalent"}, res.to_dict()

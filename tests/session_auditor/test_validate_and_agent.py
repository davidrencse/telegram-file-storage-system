from __future__ import annotations

import json
import sys

import pytest
from conftest import fast_config

from session_auditor import gitops
from session_auditor.agent import agent_env, claude_argv, run_agent
from session_auditor.config import GUARD_ENV, AuditorConfig
from session_auditor.prompt import build_prompt
from session_auditor.validate import parse_pytest_counts, run_commands, validate_tree


@pytest.mark.parametrize(
    ("out", "counts"),
    [
        ("25 passed in 0.5s", (25, 0)),
        ("1 failed, 24 passed, 2 skipped in 1s", (24, 1)),
        ("2 errors in 0.1s", (0, 2)),
        ("1 failed, 1 error", (0, 2)),
        ("no tests ran", (0, 0)),
    ],
)
def test_parse_pytest_counts(out, counts):
    assert parse_pytest_counts(out) == counts


def test_run_commands_ok_fail_missing_timeout(tmp_path):
    val = run_commands(
        tmp_path,
        [
            [sys.executable, "-c", "print('3 passed'); import os; assert os.environ['SESSION_AUDITOR_ACTIVE']=='1'"],
            [sys.executable, "-c", "raise SystemExit(1)"],
            ["definitely-not-a-binary-xyz"],
            [sys.executable, "-c", "import time; time.sleep(5)"],
        ],
        "t",
        timeout_s=1,
    )
    assert [c.returncode for c in val.commands] == [0, 1, 127, 124]
    assert not val.ok and val.failures == 3


def test_validate_tree_runs_real_pytest(mini_repo):
    tree = gitops.snapshot_tree(mini_repo)
    val = validate_tree(mini_repo, tree, fast_config().validate_commands, "orig")
    assert val.ok, val.to_dict()
    assert val.tests_passed == 1 and val.tests_failed == 0


def test_claude_argv_is_sandboxed():
    cfg = AuditorConfig()
    argv = claude_argv(cfg)
    assert argv[:2] == ["claude", "-p"]
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    assert argv[argv.index("--max-budget-usd") + 1] == "5.0"
    allow = json.loads(argv[argv.index("--settings") + 1])["permissions"]["allow"]
    assert "Bash(uv run pytest *)" in allow
    assert not any(
        a.startswith(("Bash(git commit", "Bash(tgfs", "Bash(uv run tgfs", "WebFetch", "Bash(*")) for a in allow
    )
    assert "--no-session-persistence" in argv
    assert "user" not in argv[argv.index("--setting-sources") + 1]


def test_agent_env_strips_secrets(monkeypatch, tmp_path):
    monkeypatch.setenv("TGFS_PASSPHRASE", "hunter2")
    monkeypatch.setenv("TGFS_API_HASH", "abc")
    env = agent_env(tmp_path)
    assert "TGFS_PASSPHRASE" not in env and "TGFS_API_HASH" not in env
    assert env[GUARD_ENV] == "1" and env["TGFS_HOME"].startswith(str(tmp_path))


def test_run_agent_error_paths(tmp_path):
    assert "empty" in run_agent(AuditorConfig(agent="command"), "", tmp_path, tmp_path / "l").error
    assert "unknown" in run_agent(AuditorConfig(agent="gpt"), "", tmp_path, tmp_path / "l").error
    res = run_agent(fast_config("crash"), "p", tmp_path, tmp_path / "log")
    assert not res.ok and res.returncode == 1 and "boom" in res.error
    slow = AuditorConfig(
        agent="command", agent_command=[sys.executable, "-c", "import time; time.sleep(5)"], timeout_s=1
    )
    assert run_agent(slow, "", tmp_path, tmp_path / "l").returncode == 124


def test_prompt_mentions_every_input_and_output():
    p = build_prompt("abc")
    for name in (
        "transcript.md",
        "original.diff",
        "findings.json",
        "validation.json",
        "AGENTS.final.md",
        "critique.md",
        "critique.json",
        "redo_notes.md",
        "redo_answer.md",
        "uv run pytest",
    ):
        assert name in p
    assert "abc" in p


def test_config_file_and_env_override(tmp_path, monkeypatch):
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude/auditor.json").write_text(json.dumps({"model": "sonnet", "max_budget_usd": 1}))
    monkeypatch.setenv("AUDITOR_MAX_BUDGET_USD", "0.5")
    monkeypatch.setenv("AUDITOR_ENABLED", "off")
    cfg = AuditorConfig.load(tmp_path)
    assert (cfg.model, cfg.max_budget_usd, cfg.enabled) == ("sonnet", 0.5, False)
    (tmp_path / ".claude/auditor.json").write_text(json.dumps({"modle": "typo"}))
    with pytest.raises(ValueError, match="modle"):
        AuditorConfig.load(tmp_path)


def test_repo_config_is_valid():
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    cfg = AuditorConfig.load(root)
    assert cfg.agent == "claude" and cfg.max_budget_usd > 0
    settings = json.loads((root / ".claude/settings.json").read_text())
    for event in ("SessionStart", "SessionEnd"):
        cmd = settings["hooks"][event][0]["hooks"][0]["command"]
        assert "session_auditor hook" in cmd

from __future__ import annotations

import pytest

from session_auditor import checks
from session_auditor.checks import Finding, run_process_checks, run_tree_checks, severity_score, sort_findings
from session_auditor.gitops import Change
from session_auditor.transcript import parse_transcript

SRC = [Change("M", "src/tgfs/index.py")]
SRC_AND_TEST = SRC + [Change("M", "tests/test_index.py")]


def rules(findings: list[Finding]) -> set[str]:
    return {f.rule for f in findings}


def record(tmp_path, tb):
    return parse_transcript(tb.write(tmp_path / "t.jsonl"))


# --------------------------------------------------------------------------- verification


def test_verify_missing_when_code_changed_without_tests_run(tmp_path, tb):
    tb.user("fix").edit("/r/src/tgfs/index.py")
    f = checks.check_verification(record(tmp_path, tb), SRC)
    assert [x.rule for x in f] == ["verify-missing", "verify-missing"]
    assert {x.severity for x in f} == {"high", "medium"}


def test_verify_stale_when_edit_after_last_run(tmp_path, tb):
    tb.user("fix").bash("uv run pytest -q", "5 passed").bash("uv run ruff check src", "All checks passed!")
    tb.edit("/r/src/tgfs/index.py")
    assert rules(checks.check_verification(record(tmp_path, tb), SRC)) == {"verify-stale"}


def test_verify_stale_counts_bash_edits(tmp_path, tb):
    tb.bash("uv run pytest -q", "5 passed").bash("ruff check .", "ok").bash("sed -i s/a/b/ src/tgfs/index.py")
    assert rules(checks.check_verification(record(tmp_path, tb), SRC)) == {"verify-stale"}


def test_verify_failing_detected_from_output(tmp_path, tb):
    tb.edit("/r/src/tgfs/index.py").bash("uv run pytest -q", "1 failed, 4 passed").bash("ruff check", "ok")
    assert rules(checks.check_verification(record(tmp_path, tb), SRC)) == {"verify-failing"}


def test_verify_clean(tmp_path, tb):
    tb.edit("/r/src/tgfs/index.py").bash("uv run pytest -q", "5 passed").bash("uv run ruff check src", "ok")
    assert checks.check_verification(record(tmp_path, tb), SRC) == []


def test_verify_ignores_docs_only_changes(tmp_path, tb):
    tb.edit("/r/README.md")
    assert checks.check_verification(record(tmp_path, tb), [Change("M", "README.md")]) == []


# --------------------------------------------------------------------------- behaviour rules


def test_unverified_claim(tmp_path, tb):
    tb.edit("/r/src/tgfs/x.py").say("Done — all tests pass.")
    assert rules(checks.check_unverified_claim(record(tmp_path, tb), SRC)) == {"unverified-claim"}


def test_claim_backed_by_run_is_fine(tmp_path, tb):
    tb.bash("pytest", "3 passed").say("All tests pass.")
    assert checks.check_unverified_claim(record(tmp_path, tb), SRC) == []


def test_tests_missing(tmp_path, tb):
    rec = record(tmp_path, tb)
    assert rules(checks.check_tests_for_src(rec, SRC)) == {"tests-missing"}
    assert checks.check_tests_for_src(rec, SRC_AND_TEST) == []
    assert checks.check_tests_for_src(rec, [Change("D", "src/tgfs/old.py")]) == []


@pytest.mark.parametrize(
    ("prompt", "expected"), [("add feature", {"unrequested-commit"}), ("add feature and commit it", set())]
)
def test_commit_requires_request(tmp_path, tb, prompt, expected):
    tb.user(prompt).bash('git commit -m "feat: x"')
    assert rules(checks.check_commits(record(tmp_path, tb), [])) == expected


@pytest.mark.parametrize(
    ("cmd", "hit"),
    [
        ("rm -rf build", True),
        ("rm -fr /tmp/x", True),
        ("git reset --hard HEAD~1", True),
        ("git clean -fdx", True),
        ("git checkout -- .", True),
        ("git push origin main --force", True),
        ("rm file.txt", False),
        ("git reset HEAD file", False),
        ("ls -rf", False),
    ],
)
def test_destructive(tmp_path, tb, cmd, hit):
    tb.bash(cmd)
    assert bool(checks.check_destructive(record(tmp_path, tb), [])) is hit


@pytest.mark.parametrize(
    ("cmd", "hit"),
    [("uv run tgfs put a.txt /", True), ("tgfs login", True), ("uv run tgfs --help", False), ("tgfsx put", False)],
)
def test_live_telegram(tmp_path, tb, cmd, hit):
    tb.user("improve upload").bash(cmd)
    assert bool(checks.check_live_telegram(record(tmp_path, tb), [])) is hit


def test_live_telegram_allowed_when_asked(tmp_path, tb):
    tb.user("try it against my live account").bash("tgfs put a /")
    assert checks.check_live_telegram(record(tmp_path, tb), []) == []


def test_secrets(tmp_path, tb):
    tb.tool("Read", {"file_path": "/home/u/.local/share/tgfs/account.session"})
    tb.bash('echo "$TGFS_PASSPHRASE"')
    tb.tool("Read", {"file_path": "/r/.claude/auditor.json"})
    f = checks.check_secrets(record(tmp_path, tb), [Change("A", "config.json")])
    assert rules(f) == {"secret-access", "secret-leak", "secret-in-tree"}
    assert sum(x.rule == "secret-access" for x in f) == 1


def test_retry_loop(tmp_path, tb):
    for _ in range(3):
        tb.bash("make build", "err", is_error=True)
    tb.bash("make ok", "err", is_error=True)
    f = checks.check_tool_loops(record(tmp_path, tb), [])
    assert [x.evidence for x in f] == ["make build"]


def test_irrelevant_skill(tmp_path, tb):
    tb.tool("Skill", {"skill": "vercel:nextjs"}).tool("Skill", {"skill": "engineering:debug"})
    assert [f.evidence for f in checks.check_irrelevant_skills(record(tmp_path, tb), [])] == ["vercel:nextjs"]


def test_docs_drift(tmp_path, tb):
    rec = record(tmp_path, tb)
    assert rules(checks.check_docs_drift(rec, [Change("A", "src/tgfs/dedup.py")])) == {"docs-drift"}
    assert checks.check_docs_drift(rec, [Change("A", "src/tgfs/dedup.py"), Change("M", "AGENTS.md")]) == []
    assert checks.check_docs_drift(rec, [Change("M", "src/tgfs/index.py")]) == []


def test_exemplary_session_has_no_process_findings(tmp_path, tb):
    tb.user("add join_path").edit("/r/src/tgfs/index.py").edit("/r/tests/test_index.py")
    tb.bash("uv run ruff check --fix src tests", "All checks passed!").bash("uv run pytest -q", "26 passed")
    tb.say("Added join_path with tests; 26 passed.")
    assert run_process_checks(record(tmp_path, tb), SRC_AND_TEST) == []


# --------------------------------------------------------------------------- tree rules


def reader(files: dict[str, str]):
    return files.get


GOOD_BACKEND = """
from typing import Protocol
class Backend(Protocol):
    async def upload(self, d): ...
    async def delete(self, ids): ...
class MemoryBackend:
    async def upload(self, d): ...
    async def delete(self, ids): ...
    def _private(self): ...
class TelegramBackend:
    async def upload(self, d): ...
    async def delete(self, ids): ...
"""


def test_backend_parity_ok_and_broken():
    assert checks.check_backend_parity(reader({"src/tgfs/backend.py": GOOD_BACKEND})) == []
    broken = GOOD_BACKEND.replace(
        "class TelegramBackend:\n    async def upload(self, d): ...\n", "class TelegramBackend:\n"
    )
    broken += "\n    def other(self): ...\n"
    f = checks.check_backend_parity(reader({"src/tgfs/backend.py": broken}))
    assert len(f) == 1 and "TelegramBackend" in f[0].message and "upload" in f[0].message
    gone = GOOD_BACKEND.split("class TelegramBackend")[0]
    assert "missing" in checks.check_backend_parity(reader({"src/tgfs/backend.py": gone}))[0].message


def test_telethon_boundary_and_import_direction():
    files = {
        "src/tgfs/index.py": "import telethon\nfrom .storage import Storage\n",
        "src/tgfs/storage.py": "from telethon.tl import types\nfrom .index import Index\nfrom . import errors\n",
        "src/tgfs/session.py": "from .storage import Storage\nfrom tgfs.config import Config\n",
    }
    f = checks.check_import_direction(reader(files))
    assert rules(f) == {"telethon-boundary", "import-direction"}
    assert sum(x.rule == "telethon-boundary" for x in f) == 2
    assert any("index.py imports ['storage']" in x.message for x in f)
    assert not any("session.py" in x.message for x in f)


def test_syntax_error_detected():
    f = run_tree_checks(reader({"src/tgfs/a.py": "def (:\n", "docs/x.py": "def (:\n"}), ["src/tgfs/a.py", "docs/x.py"])
    assert [x.rule for x in f] == ["syntax-error"]


def test_real_repo_tree_is_clean():
    """The actual project must satisfy its own invariants."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]

    def read(p: str):
        path = root / p
        return path.read_text() if path.exists() else None

    paths = [str(p.relative_to(root)) for p in (root / "src").rglob("*.py")]
    assert run_tree_checks(read, paths) == []


def test_sort_and_score():
    fs = [Finding("b", "low", ""), Finding("a", "critical", ""), Finding("c", "high", "")]
    assert [f.severity for f in sort_findings(fs)] == ["critical", "high", "low"]
    assert severity_score(fs) == 121


DENIED = (
    "The server-side auto mode classifier gave no verdict (error), so auto mode cannot determine the safety of Bash."
)


def test_blocked_tool_calls_do_not_count_as_runs(tmp_path, tb):
    """Regression from a real transcript: a harness-blocked `pytest` was reported as a failing test run."""
    tb.bash("uv run pytest -q", "5 passed").edit("/r/src/tgfs/index.py")
    tb.bash("uv run ruff check . && uv run pytest -q", DENIED, is_error=True)
    f = checks.check_verification(record(tmp_path, tb), SRC)
    assert {(x.rule, x.severity) for x in f} == {("verify-stale", "high"), ("verify-missing", "medium")}


def test_real_command_failure_still_counts(tmp_path, tb):
    tb.edit("/r/src/tgfs/index.py").bash("uv run pytest -q", "Exit code 1\n1 failed, 3 passed", is_error=True)
    tb.bash("ruff check .", "All checks passed!")
    assert rules(checks.check_verification(record(tmp_path, tb), SRC)) == {"verify-failing"}


def test_commit_plumbing_is_not_a_commit(tmp_path, tb):
    """Regression from this repo's own self-audit: `git commit-tree` was flagged as an unrequested commit."""
    tb.user("build it").bash("C=$(git commit-tree $TREE -m tmp)").bash("git commit-graph write")
    assert checks.check_commits(record(tmp_path, tb), []) == []


def test_rm_rf_on_scratch_is_low(tmp_path, tb):
    tb.bash("rm -rf /tmp/claude-1000/x/scratchpad/live").bash("rm -rf src").bash("git reset --hard  # in /tmp/")
    sev = [f.severity for f in checks.check_destructive(record(tmp_path, tb), [])]
    assert sev == ["low", "high", "high"]


def test_heredoc_bodies_are_not_commands(tmp_path, tb):
    tb.user("x").bash("python3 - <<'EOF'\nprint('rm -rf / ; git push --force')\nEOF\necho done")
    tb.bash("cat > f.sh <<EOF\ngit commit -m x\nEOF")
    rec = record(tmp_path, tb)
    assert checks.check_destructive(rec, []) == [] and checks.check_commits(rec, []) == []
    tb.bash("cat <<EOF\nhi\nEOF\nrm -rf src")
    assert len(checks.check_destructive(record(tmp_path, tb), [])) == 1

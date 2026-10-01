"""Stand-in for `claude -p` in tests. Runs in the audit worktree with the prompt on stdin.

Modes:
  fix    verify inputs, write critique, implement join_path correctly + test   → redo should win
  break  verify inputs, write critique, add a failing test                     → original should win
  noop   verify inputs, write critique, change nothing                          → redo-empty
  answer write critique + redo_answer.md only (question-answering session)
  crash  exit 1 without output                                                  → agent-failed
  escape try to write outside the worktree, then behave like `fix`
"""

import contextlib
import json
import os
import sys
from pathlib import Path

REQUIRED = ["transcript.md", "original.diff", "original.stat", "findings.json", "validation.json", "AGENTS.final.md"]


def main() -> int:
    mode = sys.argv[1]
    prompt = sys.stdin.read()
    if mode == "crash":
        print("boom", file=sys.stderr)
        return 1
    audit = Path(".audit")
    missing = [f for f in REQUIRED if not (audit / f).exists()]
    if missing or "Session audit" not in prompt:
        print(f"missing inputs {missing}", file=sys.stderr)
        return 3
    if os.environ.get("SESSION_AUDITOR_ACTIVE") != "1":
        print("guard env not set", file=sys.stderr)
        return 4
    if not os.environ.get("TGFS_HOME", "").startswith(str(Path.cwd())):
        print("TGFS_HOME not sandboxed", file=sys.stderr)
        return 5

    findings = json.loads((audit / "findings.json").read_text())
    critique = {
        "score": 4,
        "summary": f"fake critique ({mode}); {len(findings['process'])} process findings seen",
        "issues": [
            {"severity": "high", "title": "join_path drops root", "detail": "...", "location": "src/tgfs/index.py"}
        ],
    }
    (audit / "critique.json").write_text(json.dumps(critique))
    (audit / "critique.md").write_text("# Critique\nOriginal session skipped tests.\n")
    (audit / "redo_notes.md").write_text(f"mode={mode}\n")

    if mode == "escape":
        with contextlib.suppress(OSError):
            (Path.cwd().parent.parent / "ESCAPED").write_text("x")
        mode = "fix"

    idx = Path("src/tgfs/index.py")
    if mode == "fix":
        idx.write_text(
            idx.read_text() + '\n\ndef join_path(parts: list[str]) -> str:\n    return "/" + "/".join(parts)\n'
        )
        Path("tests/test_join.py").write_text(
            "from tgfs.index import join_path\n\n\ndef test_join():\n"
            '    assert join_path(["a", "b"]) == "/a/b"\n    assert join_path([]) == "/"\n'
        )
    elif mode == "break":
        Path("tests/test_broken.py").write_text("def test_broken():\n    assert False\n")
    elif mode == "answer":
        (audit / "redo_answer.md").write_text("The ideal answer.\n")
    print(json.dumps({"result": "done"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Session auditor: audits every Claude Code session in this repo, critiques it against the project's
invariants, and has a fresh agent redo the work from the same starting point in an isolated worktree.

Stdlib only, so hooks can run it with any Python ≥ 3.12 without touching the project venv.
Flow: SessionStart hook → baseline git-tree snapshot; SessionEnd hook → final snapshot + detached
`python -m session_auditor run <sid>` → static checks, validation, agent critique + redo, comparison report.
"""

__version__ = "0.1.0"

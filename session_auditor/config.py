"""Auditor config: `<repo>/.claude/auditor.json` overrides defaults; `AUDITOR_*` env vars override both."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

# Set in the environment of every process the auditor spawns. Hooks see it and exit, so the auditing
# agent's own session is never audited (no infinite recursion).
GUARD_ENV = "SESSION_AUDITOR_ACTIVE"

DEFAULT_VALIDATE = [
    ["uv", "run", "--frozen", "--all-extras", "pytest", "-q", "-p", "no:cacheprovider", "-rs"],
    # `.` not explicit dirs: trees from before a directory existed must still validate.
    ["uv", "run", "--frozen", "ruff", "check", "--no-cache", "."],
    ["uv", "run", "--frozen", "ruff", "format", "--check", "--no-cache", "."],
]

# Tools the redo agent may use. Deliberately no network, no git writes, no `tgfs` (live Telegram account).
DEFAULT_ALLOWED_TOOLS = [
    "Read",
    "Edit",
    "Write",
    "Glob",
    "Grep",
    "Bash(uv run pytest *)",
    "Bash(uv run ruff *)",
    "Bash(uv sync *)",
    "Bash(git diff *)",
    "Bash(git status *)",
    "Bash(git log *)",
    "Bash(ls *)",
    "Bash(wc *)",
    "Bash(cd *)",
]


@dataclass
class AuditorConfig:
    enabled: bool = True
    # "claude" = headless Claude Code; "command" = run `agent_command` (tests, other agents).
    agent: str = "claude"
    agent_command: list[str] = field(default_factory=list)
    model: str = "opus"
    effort: str = "high"
    max_budget_usd: float = 5.0
    timeout_s: int = 2400
    allowed_tools: list[str] = field(default_factory=lambda: list(DEFAULT_ALLOWED_TOOLS))
    validate_commands: list[list[str]] = field(default_factory=lambda: [list(c) for c in DEFAULT_VALIDATE])
    validate_timeout_s: int = 600
    # Skip the (paid) agent step for sessions that changed nothing unless this is on.
    audit_readonly_sessions: bool = False
    keep_worktree: bool = False
    max_digest_chars: int = 80_000

    @classmethod
    def load(cls, repo: Path) -> AuditorConfig:
        cfg = cls()
        path = repo / ".claude" / "auditor.json"
        if path.is_file():
            data = json.loads(path.read_text())
            known = {f.name for f in fields(cls)}
            unknown = set(data) - known
            if unknown:
                raise ValueError(f"{path}: unknown keys {sorted(unknown)}")
            for k, v in data.items():
                setattr(cfg, k, v)
        env_map = {
            "AUDITOR_ENABLED": ("enabled", lambda s: s.lower() not in {"0", "false", "no", "off"}),
            "AUDITOR_MODEL": ("model", str),
            "AUDITOR_MAX_BUDGET_USD": ("max_budget_usd", float),
            "AUDITOR_TIMEOUT_S": ("timeout_s", int),
        }
        for env, (attr, conv) in env_map.items():
            if env in os.environ:
                setattr(cfg, attr, conv(os.environ[env]))
        return cfg

    def to_dict(self) -> dict:
        return asdict(self)


def audits_dir(repo: Path) -> Path:
    return repo / ".claude" / "audits"


def worktrees_dir(repo: Path) -> Path:
    """Scratch worktrees live outside the repo: Claude Code treats any path under `.claude/` as protected config
    and denies headless edits there, and keeping them out of the tree keeps project tooling from scanning them."""
    base = Path(os.environ.get("AUDITOR_WORKTREES") or Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")))
    digest = hashlib.sha256(str(repo.resolve()).encode()).hexdigest()[:10]
    root = base if os.environ.get("AUDITOR_WORKTREES") else base / "session-auditor"
    return root / f"{repo.name}-{digest}"

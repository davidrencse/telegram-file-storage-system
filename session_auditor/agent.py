"""Agents that perform critique + redo inside a worktree. The prompt is passed on stdin."""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .config import GUARD_ENV, AuditorConfig
from .prompt import SYSTEM_PROMPT


@dataclass
class AgentResult:
    returncode: int
    seconds: float
    cost_usd: float | None = None
    result_text: str = ""
    permission_denials: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.error

    def to_dict(self) -> dict:
        d = asdict(self)
        d["ok"] = self.ok
        return d


def claude_argv(cfg: AuditorConfig) -> list[str]:
    # Permissions via --settings rather than --allowedTools: rules contain spaces and the flag is variadic.
    settings = {"permissions": {"allow": cfg.allowed_tools}}
    return [
        "claude",
        "-p",
        "--output-format",
        "json",
        "--model",
        cfg.model,
        "--effort",
        cfg.effort,
        "--max-budget-usd",
        str(cfg.max_budget_usd),
        "--permission-mode",
        "dontAsk",
        "--settings",
        json.dumps(settings),
        # Skip user-level hooks/styles (e.g. terse output modes); project hooks see GUARD_ENV and no-op.
        "--setting-sources",
        "project,local",
        "--no-session-persistence",
        "--append-system-prompt",
        SYSTEM_PROMPT,
    ]


def agent_env(worktree: Path) -> dict[str, str]:
    env = {**os.environ, GUARD_ENV: "1", "TGFS_HOME": str(worktree / ".audit" / "tgfs-home-sandbox")}
    for k in ("TGFS_API_ID", "TGFS_API_HASH", "TGFS_PASSPHRASE", "VIRTUAL_ENV"):
        env.pop(k, None)
    return env


def run_agent(cfg: AuditorConfig, prompt: str, worktree: Path, log_path: Path) -> AgentResult:
    if cfg.agent == "claude":
        argv = claude_argv(cfg)
    elif cfg.agent == "command":
        if not cfg.agent_command:
            return AgentResult(2, 0.0, error="agent=command but agent_command is empty")
        argv = list(cfg.agent_command)
    else:
        return AgentResult(2, 0.0, error=f"unknown agent kind {cfg.agent!r}")

    start = time.monotonic()
    try:
        proc = subprocess.run(
            argv,
            cwd=worktree,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=cfg.timeout_s,
            env=agent_env(worktree),
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        return AgentResult(124, round(time.monotonic() - start, 1), error=f"timeout after {cfg.timeout_s}s")
    except FileNotFoundError as exc:
        return AgentResult(127, 0.0, error=str(exc))
    seconds = round(time.monotonic() - start, 1)
    log_path.write_text(f"$ {argv[0]} …\n--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}")

    res = AgentResult(proc.returncode, seconds, result_text=proc.stdout[-20_000:])
    if cfg.agent == "claude":
        try:
            data = json.loads(proc.stdout)
            res.cost_usd = data.get("total_cost_usd")
            res.result_text = str(data.get("result", ""))[-20_000:]
            for d in data.get("permission_denials") or []:
                inp = d.get("tool_input") or {}
                res.permission_denials.append(
                    f"{d.get('tool_name')}: {inp.get('command') or inp.get('file_path') or ''}"[:200]
                )
            if data.get("is_error"):
                res.error = str(data.get("subtype") or "claude reported is_error")
        except json.JSONDecodeError:
            res.error = res.error or f"non-JSON output from claude: {proc.stderr[-500:]}"
    if proc.returncode != 0 and not res.error:
        res.error = proc.stderr[-1000:] or f"exit {proc.returncode}"
    return res

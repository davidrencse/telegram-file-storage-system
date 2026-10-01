"""Run the project's quality gates against a tree and summarise the results."""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import gitops
from .config import GUARD_ENV

_PYTEST_COUNTS = re.compile(r"(\d+) (passed|failed|errors?|skipped|xfailed|xpassed)")


@dataclass
class CommandResult:
    argv: list[str]
    returncode: int
    seconds: float
    output: str  # tail only

    @property
    def ok(self) -> bool:
        return self.returncode == 0


@dataclass
class Validation:
    label: str
    commands: list[CommandResult] = field(default_factory=list)
    tests_passed: int = 0
    tests_failed: int = 0

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.commands)

    @property
    def failures(self) -> int:
        return sum(not c.ok for c in self.commands)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["ok"] = self.ok
        return d


def parse_pytest_counts(output: str) -> tuple[int, int]:
    passed = failed = 0
    for n, kind in _PYTEST_COUNTS.findall(output):
        if kind == "passed":
            passed = int(n)
        elif kind in ("failed", "error", "errors"):
            failed += int(n)
    return passed, failed


def run_commands(workdir: Path, commands: list[list[str]], label: str, timeout_s: int = 600) -> Validation:
    val = Validation(label)
    env = {**os.environ, GUARD_ENV: "1", "TGFS_HOME": str(workdir / ".tgfs-home-sandbox")}
    env.pop("VIRTUAL_ENV", None)  # let uv build a venv for *this* tree, not reuse the caller's
    for argv in commands:
        start = time.monotonic()
        try:
            proc = subprocess.run(
                argv, cwd=workdir, capture_output=True, text=True, timeout=timeout_s, env=env, errors="replace"
            )
            rc, output = proc.returncode, (proc.stdout + proc.stderr)
        except subprocess.TimeoutExpired as exc:
            rc, output = 124, f"timeout after {timeout_s}s\n{exc.stdout or ''}"
        except FileNotFoundError as exc:
            rc, output = 127, str(exc)
        res = CommandResult(list(argv), rc, round(time.monotonic() - start, 2), output[-4000:])
        val.commands.append(res)
        if any("pytest" in a for a in argv):
            p, f = parse_pytest_counts(output)
            val.tests_passed += p
            val.tests_failed += f
    return val


def validate_tree(repo: Path, tree: str, commands: list[list[str]], label: str, timeout_s: int = 600) -> Validation:
    with tempfile.TemporaryDirectory(prefix=f"auditor-{label}-") as tmp:
        work = Path(tmp) / "tree"
        gitops.export_tree(repo, tree, work)
        return run_commands(work, commands, label, timeout_s)

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
import uuid
from pathlib import Path

import pytest

from session_auditor.config import GUARD_ENV, AuditorConfig

FIXTURES = Path(__file__).parent / "fixtures"
FAKE_AGENT = FIXTURES / "fake_agent.py"


class TranscriptBuilder:
    """Writes JSONL in the same shape Claude Code uses (see ~/.claude/projects/<proj>/<sid>.jsonl)."""

    def __init__(self, session_id: str | None = None, cwd: str = "/repo") -> None:
        self.session_id = session_id or str(uuid.uuid4())
        self.cwd = cwd
        self.lines: list[dict] = [
            {"type": "permission-mode", "permissionMode": "auto", "sessionId": self.session_id},
        ]
        self._n = 0

    def _base(self, kind: str, **extra) -> dict:
        self._n += 1
        return {
            "type": kind,
            "uuid": f"u{self._n}",
            "isSidechain": False,
            "sessionId": self.session_id,
            "cwd": self.cwd,
            "timestamp": f"2026-09-30T19:{self._n // 60:02d}:{self._n % 60:02d}.000Z",
            **extra,
        }

    def user(self, text: str, meta: bool = False) -> TranscriptBuilder:
        entry = self._base("user", message={"role": "user", "content": text})
        if meta:
            entry["isMeta"] = True
        self.lines.append(entry)
        return self

    def say(self, text: str) -> TranscriptBuilder:
        self.lines.append(
            self._base("assistant", message={"role": "assistant", "content": [{"type": "text", "text": text}]})
        )
        return self

    def tool(self, name: str, input: dict, result: str = "", is_error: bool = False) -> TranscriptBuilder:
        tid = f"toolu_{self._n + 1:04d}"
        block = {"type": "tool_use", "id": tid, "name": name, "input": input}
        self.lines.append(self._base("assistant", message={"role": "assistant", "content": [block]}))
        res = {"type": "tool_result", "tool_use_id": tid, "content": result, "is_error": is_error}
        self.lines.append(self._base("user", message={"role": "user", "content": [res]}))
        return self

    def bash(self, command: str, result: str = "", is_error: bool = False) -> TranscriptBuilder:
        return self.tool("Bash", {"command": command, "description": "x"}, result, is_error)

    def edit(self, path: str, new: str = "x = 1") -> TranscriptBuilder:
        return self.tool("Edit", {"file_path": path, "old_string": "a", "new_string": new}, "ok")

    def write(self, path: Path) -> Path:
        path.write_text("\n".join(json.dumps(line) for line in self.lines) + "\n")
        return path


@pytest.fixture
def tb() -> TranscriptBuilder:
    return TranscriptBuilder()


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


MINI_FILES = {
    "AGENTS.md": "# AGENTS.md — mini tgfs\nRun `pytest` and `ruff check` before finishing. Every change needs tests.\n",
    "pyproject.toml": textwrap.dedent(
        """\
        [project]
        name = "tgfs"
        version = "0.0.0"
        requires-python = ">=3.12"

        [tool.pytest.ini_options]
        pythonpath = ["src"]
        testpaths = ["tests"]

        [tool.ruff]
        line-length = 120
        """
    ),
    ".gitignore": "__pycache__/\n.venv/\n.claude/audits/\n*.session\n",
    "src/tgfs/__init__.py": "",
    "src/tgfs/errors.py": "class TgfsError(Exception):\n    errno = 5\n",
    "src/tgfs/backend.py": textwrap.dedent(
        """\
        from typing import Protocol


        class Backend(Protocol):
            async def upload(self, data: bytes) -> int: ...

            async def download(self, message_id: int) -> bytes: ...


        class MemoryBackend:
            async def upload(self, data: bytes) -> int:
                return 1

            async def download(self, message_id: int) -> bytes:
                return b""


        class TelegramBackend:
            async def upload(self, data: bytes) -> int:
                return 1

            async def download(self, message_id: int) -> bytes:
                return b""
        """
    ),
    "src/tgfs/index.py": textwrap.dedent(
        """\
        from .errors import TgfsError


        def split_path(path: str) -> list[str]:
            if "\\0" in path:
                raise TgfsError("nul")
            return [p for p in path.split("/") if p and p != "."]
        """
    ),
    "tests/test_index.py": textwrap.dedent(
        """\
        from tgfs.index import split_path


        def test_split():
            assert split_path("/a//b/") == ["a", "b"]
        """
    ),
}


@pytest.fixture
def mini_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    for rel, content in MINI_FILES.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init"], check=True
    )
    return repo


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch, tmp_path):
    monkeypatch.delenv(GUARD_ENV, raising=False)
    monkeypatch.setenv("AUDITOR_WORKTREES", str(tmp_path / "worktrees"))


def fast_config(mode: str = "fix", **overrides) -> AuditorConfig:
    cfg = AuditorConfig(
        agent="command",
        agent_command=[sys.executable, str(FAKE_AGENT), mode],
        validate_commands=[
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
            [sys.executable, "-m", "ruff", "check", "--no-cache", "."],
        ],
        timeout_s=60,
        validate_timeout_s=120,
    )
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


def write_config(repo: Path, cfg: AuditorConfig) -> None:
    (repo / ".claude").mkdir(exist_ok=True)
    (repo / ".claude" / "auditor.json").write_text(json.dumps(cfg.to_dict()))

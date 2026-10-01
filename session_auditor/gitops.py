"""Git plumbing for snapshots. Never touches the user's index, HEAD, branches, or working tree:
snapshots use a throwaway GIT_INDEX_FILE, results are stored under `refs/audits/<sid>/*`."""

from __future__ import annotations

import os
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
_IDENTITY = {
    "GIT_AUTHOR_NAME": "session-auditor",
    "GIT_AUTHOR_EMAIL": "session-auditor@localhost",
    "GIT_COMMITTER_NAME": "session-auditor",
    "GIT_COMMITTER_EMAIL": "session-auditor@localhost",
}


class GitError(RuntimeError):
    pass


def git(repo: Path, *args: str, env: dict | None = None, input: bytes | None = None, check: bool = True) -> str:
    return git_bytes(repo, *args, env=env, input=input, check=check).decode("utf-8", "replace")


def git_bytes(repo: Path, *args: str, env: dict | None = None, input: bytes | None = None, check: bool = True) -> bytes:
    full_env = {**os.environ, **(env or {})}
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, env=full_env, input=input)
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {proc.stderr.decode(errors='replace').strip()}")
    return proc.stdout


def repo_root(path: Path) -> Path:
    return Path(git(path, "rev-parse", "--show-toplevel").strip())


def head_commit(repo: Path) -> str | None:
    out = git(repo, "rev-parse", "--verify", "-q", "HEAD", check=False).strip()
    return out or None


def snapshot_tree(repo: Path) -> str:
    """Tree object of the working directory as-is (tracked + untracked, honouring .gitignore)."""
    with tempfile.TemporaryDirectory(prefix="auditor-idx-") as tmp:
        env = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
        if head_commit(repo):
            git(repo, "read-tree", "HEAD", env=env)  # warm start: stat info avoids rehashing everything
        git(repo, "add", "-A", "--", ".", env=env)
        return git(repo, "write-tree", env=env).strip()


def resolve_tree(repo: Path, rev: str) -> str:
    if rev == "empty":
        return EMPTY_TREE
    return git(repo, "rev-parse", f"{rev}^{{tree}}").strip()


@dataclass(frozen=True)
class Change:
    status: str  # A, M, D, R…
    path: str


def changed_files(repo: Path, a: str, b: str) -> list[Change]:
    out = git(repo, "diff", "--name-status", "--no-renames", a, b)
    changes = []
    for line in out.splitlines():
        if "\t" in line:
            status, path = line.split("\t", 1)
            changes.append(Change(status[0], path))
    return changes


def diff(repo: Path, a: str, b: str, stat: bool = False) -> str:
    args = ["diff", "--no-color", "--no-renames"]
    if stat:
        args.append("--stat")
    return git(repo, *args, a, b)


def read_file(repo: Path, tree: str, path: str) -> str | None:
    out = subprocess.run(["git", "-C", str(repo), "show", f"{tree}:{path}"], capture_output=True)
    return out.stdout.decode("utf-8", "replace") if out.returncode == 0 else None


def list_files(repo: Path, tree: str) -> list[str]:
    return git(repo, "ls-tree", "-r", "--name-only", tree).splitlines()


def commit_tree(repo: Path, tree: str, message: str, parent: str | None = None) -> str:
    args = ["commit-tree", tree, "-m", message]
    if parent:
        args += ["-p", parent]
    return git(repo, *args, env=_IDENTITY).strip()


def update_ref(repo: Path, ref: str, sha: str) -> None:
    git(repo, "update-ref", ref, sha)


def export_tree(repo: Path, tree: str, dest: Path) -> None:
    """Materialise a tree into a plain directory (no .git) — used for validation runs."""
    dest.mkdir(parents=True, exist_ok=True)
    if tree == EMPTY_TREE:
        return
    data = git_bytes(repo, "archive", "--format=tar", tree)
    with tarfile.open(fileobj=BytesIO(data)) as tar:
        tar.extractall(dest, filter="data")


def add_worktree(repo: Path, path: Path, commit: str) -> None:
    git(repo, "worktree", "add", "--detach", "--force", str(path), commit)


def remove_worktree(repo: Path, path: Path) -> None:
    git(repo, "worktree", "remove", "--force", str(path), check=False)
    git(repo, "worktree", "prune", check=False)


def commit_all(worktree: Path, message: str) -> str:
    git(worktree, "add", "-A")
    git(worktree, "commit", "-q", "--allow-empty", "--no-verify", "-m", message, env=_IDENTITY)
    return git(worktree, "rev-parse", "HEAD").strip()

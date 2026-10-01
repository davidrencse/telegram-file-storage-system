"""SQLite metadata index: directory tree + which Telegram messages hold each file's chunks.

The index is the source of truth for the namespace. Telegram only stores opaque
chunk blobs; losing the index means losing file names, so `tgfs backup` uploads
a copy of it to Telegram as well.

Mutating methods that drop chunks return the orphaned message ids. The caller
(Storage) is responsible for deleting those messages from Telegram.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .errors import (
    AlreadyExists,
    DirectoryNotEmpty,
    InvalidOperation,
    IsADirectory,
    NotADirectory,
    NotFound,
)

ROOT_ID = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    id        INTEGER PRIMARY KEY,
    parent_id INTEGER REFERENCES nodes(id) ON DELETE CASCADE,
    name      TEXT    NOT NULL,
    is_dir    INTEGER NOT NULL,
    size      INTEGER NOT NULL DEFAULT 0,
    mtime     REAL    NOT NULL,
    sha256    TEXT,
    UNIQUE (parent_id, name)
);
CREATE TABLE IF NOT EXISTS chunks (
    node_id    INTEGER NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    idx        INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    size       INTEGER NOT NULL,  -- plaintext bytes
    encrypted  INTEGER NOT NULL,
    PRIMARY KEY (node_id, idx)
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value BLOB
);
"""


@dataclass(frozen=True)
class Node:
    id: int
    parent_id: int | None
    name: str
    is_dir: bool
    size: int
    mtime: float
    sha256: str | None


@dataclass(frozen=True)
class Chunk:
    idx: int
    message_id: int
    size: int
    encrypted: bool


def split_path(path: str) -> list[str]:
    """Normalize a remote path into components. '/a/./b/../c/' -> ['a', 'c']."""
    parts: list[str] = []
    for part in path.replace("\\", "/").split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if parts:
                parts.pop()
            continue
        parts.append(part)
    return parts


def join_path(parts: Sequence[str]) -> str:
    return "/" + "/".join(parts)


def _node(row: sqlite3.Row) -> Node:
    return Node(
        id=row["id"],
        parent_id=row["parent_id"],
        name=row["name"],
        is_dir=bool(row["is_dir"]),
        size=row["size"],
        mtime=row["mtime"],
        sha256=row["sha256"],
    )


class Index:
    def __init__(self, path: Path | str) -> None:
        # One connection shared across threads (FUSE is multithreaded); the RLock serializes access.
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._init()

    def _init(self) -> None:
        with self._lock:
            self._db.execute("PRAGMA foreign_keys = ON")
            self._db.executescript(SCHEMA)
            self._db.execute(
                "INSERT OR IGNORE INTO nodes (id, parent_id, name, is_dir, mtime) VALUES (?, NULL, '', 1, ?)",
                (ROOT_ID, time.time()),
            )

    def close(self) -> None:
        with self._lock:
            self._db.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield self._db
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            self._db.execute("COMMIT")

    # ---- meta ---------------------------------------------------------------

    def get_meta(self, key: str) -> bytes | None:
        with self._lock:
            row = self._db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return None if row is None else row["value"]

    def set_meta(self, key: str, value: bytes) -> None:
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))

    # ---- lookup -------------------------------------------------------------

    def get(self, node_id: int) -> Node:
        with self._lock:
            row = self._db.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()
        if row is None:
            raise NotFound(f"node {node_id}")
        return _node(row)

    def _child(self, parent_id: int, name: str) -> Node | None:
        row = self._db.execute("SELECT * FROM nodes WHERE parent_id = ? AND name = ?", (parent_id, name)).fetchone()
        return None if row is None else _node(row)

    def resolve(self, path: str) -> Node | None:
        with self._lock:
            node = self.get(ROOT_ID)
            for part in split_path(path):
                if not node.is_dir:
                    return None
                child = self._child(node.id, part)
                if child is None:
                    return None
                node = child
            return node

    def require(self, path: str) -> Node:
        node = self.resolve(path)
        if node is None:
            raise NotFound(join_path(split_path(path)))
        return node

    def require_dir(self, path: str) -> Node:
        node = self.require(path)
        if not node.is_dir:
            raise NotADirectory(join_path(split_path(path)))
        return node

    def children(self, node_id: int) -> list[Node]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM nodes WHERE parent_id = ? ORDER BY is_dir DESC, name", (node_id,)
            ).fetchall()
        return [_node(r) for r in rows]

    def chunks(self, node_id: int) -> list[Chunk]:
        with self._lock:
            rows = self._db.execute(
                "SELECT idx, message_id, size, encrypted FROM chunks WHERE node_id = ? ORDER BY idx",
                (node_id,),
            ).fetchall()
        return [Chunk(r["idx"], r["message_id"], r["size"], bool(r["encrypted"])) for r in rows]

    def path_of(self, node_id: int) -> str:
        parts: list[str] = []
        with self._lock:
            node = self.get(node_id)
            while node.parent_id is not None:
                parts.append(node.name)
                node = self.get(node.parent_id)
        return join_path(parts[::-1])

    def usage(self) -> tuple[int, int, int]:
        """(files, directories excluding root, total bytes)."""
        with self._lock:
            row = self._db.execute(
                "SELECT SUM(is_dir = 0) AS files, SUM(is_dir = 1) - 1 AS dirs, "
                "COALESCE(SUM(CASE WHEN is_dir = 0 THEN size END), 0) AS bytes FROM nodes"
            ).fetchone()
        return row["files"] or 0, row["dirs"] or 0, row["bytes"]

    def all_message_ids(self) -> set[int]:
        with self._lock:
            return {r[0] for r in self._db.execute("SELECT message_id FROM chunks")}

    # ---- mutation -----------------------------------------------------------

    def _parent_and_name(self, path: str) -> tuple[Node, str]:
        parts = split_path(path)
        if not parts:
            raise InvalidOperation("operation not allowed on root")
        parent = self.resolve(join_path(parts[:-1]))
        if parent is None:
            raise NotFound(join_path(parts[:-1]))
        if not parent.is_dir:
            raise NotADirectory(join_path(parts[:-1]))
        return parent, parts[-1]

    def _subtree_message_ids(self, node_id: int) -> list[int]:
        rows = self._db.execute(
            """
            WITH RECURSIVE sub(id) AS (
                SELECT ?
                UNION ALL
                SELECT n.id FROM nodes n JOIN sub ON n.parent_id = sub.id
            )
            SELECT c.message_id FROM chunks c JOIN sub ON c.node_id = sub.id
            """,
            (node_id,),
        ).fetchall()
        return [r[0] for r in rows]

    def _touch(self, db: sqlite3.Connection, node_id: int) -> None:
        db.execute("UPDATE nodes SET mtime = ? WHERE id = ?", (time.time(), node_id))

    def mkdir(self, path: str, parents: bool = False, exist_ok: bool = False) -> Node:
        with self._tx() as db:
            parts = split_path(path)
            if not parts:
                if exist_ok or parents:
                    return self.get(ROOT_ID)
                raise AlreadyExists("/")
            node = self.get(ROOT_ID)
            for i, part in enumerate(parts):
                last = i == len(parts) - 1
                child = self._child(node.id, part)
                if child is not None:
                    if not child.is_dir:
                        raise NotADirectory(join_path(parts[: i + 1]))
                    if last and not (exist_ok or parents):
                        raise AlreadyExists(join_path(parts))
                    node = child
                    continue
                if not last and not parents:
                    raise NotFound(join_path(parts[: i + 1]))
                cur = db.execute(
                    "INSERT INTO nodes (parent_id, name, is_dir, mtime) VALUES (?, ?, 1, ?)",
                    (node.id, part, time.time()),
                )
                self._touch(db, node.id)
                node = self.get(cur.lastrowid)
            return node

    def put_file(
        self,
        path: str,
        size: int,
        sha256: str,
        chunks: Sequence[Chunk],
        mtime: float | None = None,
    ) -> tuple[Node, list[int]]:
        """Create or replace a file. Returns (node, message ids of replaced chunks)."""
        mtime = time.time() if mtime is None else mtime
        with self._tx() as db:
            parent, name = self._parent_and_name(path)
            existing = self._child(parent.id, name)
            orphaned: list[int] = []
            if existing is not None:
                if existing.is_dir:
                    raise IsADirectory(path)
                orphaned = [c.message_id for c in self.chunks(existing.id)]
                db.execute("DELETE FROM chunks WHERE node_id = ?", (existing.id,))
                db.execute(
                    "UPDATE nodes SET size = ?, sha256 = ?, mtime = ? WHERE id = ?",
                    (size, sha256, mtime, existing.id),
                )
                node_id = existing.id
            else:
                cur = db.execute(
                    "INSERT INTO nodes (parent_id, name, is_dir, size, mtime, sha256) VALUES (?, ?, 0, ?, ?, ?)",
                    (parent.id, name, size, mtime, sha256),
                )
                node_id = cur.lastrowid
            db.executemany(
                "INSERT INTO chunks (node_id, idx, message_id, size, encrypted) VALUES (?, ?, ?, ?, ?)",
                [(node_id, c.idx, c.message_id, c.size, int(c.encrypted)) for c in chunks],
            )
            self._touch(db, parent.id)
            return self.get(node_id), orphaned

    def remove(self, path: str, recursive: bool = False) -> list[int]:
        """Delete a file or directory. Returns message ids that are now orphaned."""
        with self._tx() as db:
            node = self.require(path)
            if node.id == ROOT_ID:
                raise InvalidOperation("cannot remove root")
            if node.is_dir and not recursive and self.children(node.id):
                raise DirectoryNotEmpty(path)
            orphaned = self._subtree_message_ids(node.id)
            db.execute("DELETE FROM nodes WHERE id = ?", (node.id,))
            self._touch(db, node.parent_id)
            return orphaned

    def move(self, src: str, dst: str) -> list[int]:
        """Rename/move. If dst is a directory, move into it. Overwrites a dst file.
        Returns message ids orphaned by an overwrite."""
        with self._tx() as db:
            node = self.require(src)
            if node.id == ROOT_ID:
                raise InvalidOperation("cannot move root")
            target = self.resolve(dst)
            if target is not None and target.is_dir and target.id != node.id:
                new_parent, new_name = target, node.name
                target = self._child(new_parent.id, new_name)
            else:
                new_parent, new_name = self._parent_and_name(dst)

            # Refuse to move a directory into its own subtree.
            cursor: Node | None = new_parent
            while cursor is not None:
                if cursor.id == node.id:
                    raise InvalidOperation("cannot move a directory into itself")
                cursor = None if cursor.parent_id is None else self.get(cursor.parent_id)

            orphaned: list[int] = []
            if target is not None and target.id != node.id:
                if target.is_dir:
                    if not node.is_dir:
                        raise IsADirectory(dst)
                    if self.children(target.id):
                        raise DirectoryNotEmpty(dst)
                elif node.is_dir:
                    raise NotADirectory(dst)
                orphaned = self._subtree_message_ids(target.id)
                db.execute("DELETE FROM nodes WHERE id = ?", (target.id,))

            db.execute(
                "UPDATE nodes SET parent_id = ?, name = ? WHERE id = ?",
                (new_parent.id, new_name, node.id),
            )
            self._touch(db, new_parent.id)
            if node.parent_id != new_parent.id:
                self._touch(db, node.parent_id)
            return orphaned

    def set_mtime(self, node_id: int, mtime: float) -> None:
        with self._lock:
            self._db.execute("UPDATE nodes SET mtime = ? WHERE id = ?", (mtime, node_id))

    # ---- backup -------------------------------------------------------------

    def serialize(self) -> bytes:
        with self._lock:
            return self._db.serialize()

    def restore(self, data: bytes) -> None:
        """Replace the whole index with a serialized snapshot."""
        src = sqlite3.connect(":memory:")
        try:
            src.deserialize(data)
            ok = src.execute("PRAGMA integrity_check").fetchone()[0]
            if ok != "ok":
                raise InvalidOperation(f"index snapshot is corrupt: {ok}")
            with self._lock:
                src.backup(self._db)
                self._init()
        finally:
            src.close()

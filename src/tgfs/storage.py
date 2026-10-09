"""High-level file operations: split files into chunks, (optionally) encrypt, push to
the backend, and record everything in the index."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import secrets
from collections import deque
from collections.abc import AsyncIterator, Callable
from pathlib import Path

from .backend import Backend
from .config import DEFAULT_CHUNK_SIZE, DEFAULT_PARALLEL
from .crypto import SALT_SIZE, Cipher, new_salt
from .errors import IntegrityError, InvalidOperation, IsADirectory, NotFound, PassphraseRequired
from .index import Chunk, Index, Node, join_path, split_path

Progress = Callable[[int], None]

INDEX_CAPTION = "#tgfs-index"
INDEX_MAGIC = b"TGFSIDX1"
META_SALT = "kdf_salt"
META_CHECK = "key_check"


def open_cipher(index: Index, passphrase: str, create: bool = True) -> Cipher:
    """Build the cipher for this index, creating salt + key check on first use."""
    salt = index.get_meta(META_SALT)
    if salt is None:
        if not create:
            raise PassphraseRequired("encryption has not been initialised for this index")
        salt = new_salt()
        cipher = Cipher.from_passphrase(passphrase, salt)
        index.set_meta(META_SALT, salt)
        index.set_meta(META_CHECK, cipher.key_check())
        return cipher
    cipher = Cipher.from_passphrase(passphrase, salt)
    check = index.get_meta(META_CHECK)
    if check is not None and not cipher.verify(check):
        raise IntegrityError("wrong passphrase")
    return cipher


class Storage:
    def __init__(
        self,
        index: Index,
        backend: Backend,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        cipher: Cipher | None = None,
        parallel: int = DEFAULT_PARALLEL,
    ) -> None:
        if chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        if parallel <= 0:
            raise ValueError("parallel must be positive")
        self.index = index
        self.backend = backend
        self.chunk_size = chunk_size
        self.cipher = cipher
        # Chunks in flight per file. Peak memory ~ (parallel + 1) * chunk_size.
        self.parallel = parallel

    # ---- chunk I/O ----------------------------------------------------------

    async def _upload_chunk(self, idx: int, data: bytes) -> Chunk:
        # AES-GCM over a 64 MiB chunk takes a while: keep the event loop (FUSE, web UI) responsive.
        blob = await asyncio.to_thread(self.cipher.encrypt, data) if self.cipher else data
        # Random blob name: never leak real file names into the chat.
        mid = await self.backend.upload(blob, f"{secrets.token_hex(8)}.bin")
        return Chunk(idx=idx, message_id=mid, size=len(data), encrypted=self.cipher is not None)

    async def read_chunk(self, chunk: Chunk) -> bytes:
        blob = await self.backend.download(chunk.message_id)
        if chunk.encrypted:
            if self.cipher is None:
                raise PassphraseRequired("file is encrypted; set TGFS_PASSPHRASE")
            blob = await asyncio.to_thread(self.cipher.decrypt, blob)
        if len(blob) != chunk.size:
            raise IntegrityError(f"chunk {chunk.message_id}: expected {chunk.size} bytes, got {len(blob)}")
        return blob

    async def _delete_messages(self, message_ids: list[int]) -> None:
        if message_ids:
            await self.backend.delete(message_ids)

    # ---- namespace ----------------------------------------------------------

    def stat(self, path: str) -> Node:
        return self.index.require(path)

    def ls(self, path: str = "/") -> list[Node]:
        node = self.index.require(path)
        return self.index.children(node.id) if node.is_dir else [node]

    def mkdir(self, path: str, parents: bool = False) -> Node:
        return self.index.mkdir(path, parents=parents, exist_ok=parents)

    async def rm(self, path: str, recursive: bool = False) -> int:
        orphaned = self.index.remove(path, recursive=recursive)
        await self._delete_messages(orphaned)
        return len(orphaned)

    async def mv(self, src: str, dst: str) -> None:
        await self._delete_messages(self.index.move(src, dst))

    def _file_target(self, remote: str, name: str) -> str:
        """`put a.txt /docs/` or `put a.txt /docs` (existing dir) -> /docs/a.txt."""
        node = self.index.resolve(remote)
        if remote.endswith("/") or (node is not None and node.is_dir):
            return join_path([*split_path(remote), name])
        return join_path(split_path(remote))

    # ---- upload -------------------------------------------------------------

    async def put(self, local: Path | str, remote: str, progress: Progress | None = None) -> Node:
        local = Path(local)
        if local.is_dir():
            raise IsADirectory(f"{local} is a directory (use put_tree / -r)")
        target = self._file_target(remote, local.name)
        parts = split_path(target)
        if not parts:
            raise InvalidOperation("cannot overwrite root")
        self.index.require_dir(join_path(parts[:-1]))  # fail before uploading anything

        digest = hashlib.sha256()
        size = 0
        chunks: list[Chunk] = []
        pending: set[asyncio.Task[Chunk]] = set()
        try:
            with local.open("rb") as f:
                idx = 0
                while data := f.read(self.chunk_size):
                    digest.update(data)
                    size += len(data)
                    while len(pending) >= self.parallel:
                        await self._reap(pending, chunks, progress)
                    pending.add(asyncio.create_task(self._upload_chunk(idx, data)))
                    idx += 1
            while pending:
                await self._reap(pending, chunks, progress)
            chunks.sort(key=lambda c: c.idx)
            node, orphaned = self.index.put_file(target, size, digest.hexdigest(), chunks, mtime=local.stat().st_mtime)
        except BaseException:
            # Never leave unreferenced blobs behind in the chat. In-flight uploads are awaited, not
            # cancelled: a cancelled send may still land in the chat and we would lose its id.
            results = await asyncio.gather(*pending, return_exceptions=True)
            chunks.extend(r for r in results if isinstance(r, Chunk))
            await self._delete_messages([c.message_id for c in chunks])
            raise
        await self._delete_messages(orphaned)
        return node

    @staticmethod
    async def _reap(pending: set[asyncio.Task[Chunk]], chunks: list[Chunk], progress: Progress | None) -> None:
        """Wait for at least one upload; record every finished chunk before re-raising any failure."""
        done, _ = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
        error: BaseException | None = None
        for task in done:
            pending.discard(task)
            if (exc := task.exception()) is not None:
                error = error or exc
                continue
            chunks.append(task.result())
            if progress:
                progress(task.result().size)
        if error is not None:
            raise error

    async def put_tree(self, local_dir: Path | str, remote_dir: str, progress: Progress | None = None) -> int:
        """Upload a directory recursively into remote_dir/<local_dir.name>. Returns file count."""
        local_dir = Path(local_dir)
        base = split_path(self._file_target(remote_dir, local_dir.name))
        self.index.mkdir(join_path(base), parents=True, exist_ok=True)
        count = 0
        for root, dirs, files in os.walk(local_dir):
            dirs.sort()
            rel = Path(root).relative_to(local_dir).parts
            here = join_path([*base, *rel])
            self.index.mkdir(here, parents=True, exist_ok=True)
            for name in sorted(files):
                path = Path(root) / name
                if path.is_symlink() or not path.is_file():
                    continue
                await self.put(path, join_path([*base, *rel, name]), progress)
                count += 1
        return count

    # ---- download -----------------------------------------------------------

    async def get(self, remote: str, local: Path | str, progress: Progress | None = None) -> Path:
        node = self.index.require(remote)
        if node.is_dir:
            raise IsADirectory(f"{remote} is a directory (use get_tree / -r)")
        local = Path(local)
        if local.is_dir():
            local = local / node.name
        part = local.with_name(local.name + ".tgfs-part")
        digest = hashlib.sha256()
        try:
            with part.open("wb") as f:
                async with contextlib.aclosing(self.iter_chunks(node)) as stream:
                    async for data in stream:
                        digest.update(data)
                        f.write(data)
                        if progress:
                            progress(len(data))
            if node.sha256 and digest.hexdigest() != node.sha256:
                raise IntegrityError(f"{remote}: checksum mismatch")
            os.utime(part, (node.mtime, node.mtime))
            part.replace(local)
        except BaseException:
            part.unlink(missing_ok=True)
            raise
        return local

    async def iter_chunks(self, node: Node) -> AsyncIterator[bytes]:
        """Yield a file's plaintext chunks in order, prefetching up to `parallel` ahead."""
        todo = iter(self.index.chunks(node.id))
        window: deque[asyncio.Task[bytes]] = deque()
        try:
            for chunk in todo:
                window.append(asyncio.create_task(self.read_chunk(chunk)))
                if len(window) >= self.parallel:
                    break
            while window:
                data = await window.popleft()
                if (nxt := next(todo, None)) is not None:
                    window.append(asyncio.create_task(self.read_chunk(nxt)))
                yield data
        finally:
            for task in window:
                task.cancel()
            await asyncio.gather(*window, return_exceptions=True)

    async def get_tree(self, remote_dir: str, local_dir: Path | str, progress: Progress | None = None) -> int:
        node = self.index.require_dir(remote_dir)
        dest = Path(local_dir) / (node.name or "tgfs-root")
        return await self._get_tree(node, dest, progress)

    async def _get_tree(self, node: Node, dest: Path, progress: Progress | None) -> int:
        dest.mkdir(parents=True, exist_ok=True)
        count = 0
        for child in self.index.children(node.id):
            # Names can come from a restored (untrusted) index: never let one escape `dest`.
            if not child.name or "/" in child.name or "\\" in child.name or child.name in (".", ".."):
                raise InvalidOperation(f"refusing unsafe file name {child.name!r} in {self.index.path_of(node.id)}")
            if child.is_dir:
                count += await self._get_tree(child, dest / child.name, progress)
            else:
                await self.get(self.index.path_of(child.id), dest / child.name, progress)
                count += 1
        return count

    # ---- index backup -------------------------------------------------------

    async def backup_index(self) -> int:
        """Upload a snapshot of the index to the chat; keeps only the newest one."""
        previous = await self.backend.find_latest(INDEX_CAPTION)
        data = self.index.serialize()
        if self.cipher:
            salt = self.index.get_meta(META_SALT)
            assert salt is not None and len(salt) == SALT_SIZE
            blob = INDEX_MAGIC + b"\x01" + salt + self.cipher.encrypt(data)
        else:
            blob = INDEX_MAGIC + b"\x00" + data
        mid = await self.backend.upload(blob, "tgfs-index.bin", caption=INDEX_CAPTION)
        if previous is not None and previous != mid:
            await self.backend.delete([previous])
        return mid

    async def restore_index(self, passphrase: str | None = None, require_encrypted: bool = False) -> int:
        """Replace the local index with the newest snapshot from the chat.

        `require_encrypted` refuses a plaintext snapshot: someone with write access to the chat could
        otherwise plant one and silently switch an encrypted setup to attacker-chosen metadata."""
        mid = await self.backend.find_latest(INDEX_CAPTION)
        if mid is None:
            raise NotFound("no index backup found in chat")
        blob = await self.backend.download(mid)
        if not blob.startswith(INDEX_MAGIC):
            raise IntegrityError("index backup has unknown format")
        body = blob[len(INDEX_MAGIC) :]
        if body[:1] == b"\x01":
            if not passphrase:
                raise PassphraseRequired("index backup is encrypted; passphrase required")
            salt, payload = body[1 : 1 + SALT_SIZE], body[1 + SALT_SIZE :]
            cipher = Cipher.from_passphrase(passphrase, salt)
            data = cipher.decrypt(payload)
            self.index.restore(data)
            self.cipher = cipher
        elif body[:1] == b"\x00":
            if require_encrypted:
                raise IntegrityError("index backup is not encrypted but encryption is enabled; refusing")
            self.index.restore(body[1:])
        else:
            raise IntegrityError("index backup has unknown format")
        return mid

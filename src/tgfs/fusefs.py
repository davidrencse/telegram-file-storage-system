"""FUSE mount. Reads stream chunks from Telegram through a small LRU cache; writes are
buffered into a local temp file and uploaded as a whole when the file is closed.

FUSE callbacks run on FUSE's worker threads; all Telegram I/O is submitted to the
asyncio loop that owns the Telethon client (running in its own thread)."""

from __future__ import annotations

import asyncio
import errno
import functools
import os
import stat
import tempfile
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

from mfusepy import FuseOSError, Operations

from .errors import IsADirectory, NotADirectory, TgfsError
from .index import Node, join_path, split_path
from .storage import Storage


def _fuse_errors(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except TgfsError as e:
            raise FuseOSError(e.errno) from e
        except FuseOSError:
            raise
        except OSError as e:
            raise FuseOSError(e.errno or errno.EIO) from e
        except Exception as e:  # never let an exception escape into ctypes
            raise FuseOSError(errno.EIO) from e

    return wrapper


@dataclass
class Handle:
    path: str
    temp: Path | None = None  # set for writable handles
    fd: int | None = None
    dirty: bool = False
    loaded: bool = True  # False until existing remote content is copied into temp (lazy: `touch` stays cheap)
    unlinked: bool = False  # deleted while open: keep serving the fd, never upload
    lock: threading.Lock = field(default_factory=threading.Lock)


class TgfsFuse(Operations):
    use_ns = True  # st_*time and utimens use integer nanoseconds

    def __init__(self, storage: Storage, loop: asyncio.AbstractEventLoop, cache_dir: Path, cache_chunks: int = 8):
        self.s = storage
        self.loop = loop
        self.cache_dir = cache_dir
        self.cache_chunks = cache_chunks
        self._cache: OrderedDict[int, bytes] = OrderedDict()
        self._cache_lock = threading.Lock()
        self._handles: dict[int, Handle] = {}
        self._pending: dict[str, Handle] = {}  # normalized path -> open writable handle
        self._next_fh = 1
        self._lock = threading.Lock()
        self._uid, self._gid = os.getuid(), os.getgid()

    # ---- helpers ------------------------------------------------------------

    def _run(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result()

    @staticmethod
    def _norm(path: str) -> str:
        return join_path(split_path(path))

    def _file(self, path: str) -> Node:
        node = self.s.index.require(path)
        if node.is_dir:
            raise IsADirectory(path)
        return node

    def _chunk_bytes(self, chunk) -> bytes:
        with self._cache_lock:
            data = self._cache.get(chunk.message_id)
            if data is not None:
                self._cache.move_to_end(chunk.message_id)
                return data
        data = self._run(self.s.read_chunk(chunk))
        with self._cache_lock:
            self._cache[chunk.message_id] = data
            while len(self._cache) > self.cache_chunks:
                self._cache.popitem(last=False)
        return data

    def _read_remote(self, node: Node, size: int, offset: int) -> bytes:
        out = bytearray()
        start = 0
        end = min(offset + size, node.size)
        for chunk in self.s.index.chunks(node.id):
            stop = start + chunk.size
            if stop > offset and start < end:
                data = self._chunk_bytes(chunk)
                out += data[max(offset - start, 0) : min(end - start, chunk.size)]
            if stop >= end:
                break
            start = stop
        return bytes(out)

    def _new_handle(self, path: str, writable: bool, preload: bool) -> int:
        h = Handle(path=self._norm(path))
        if writable:
            fd, name = tempfile.mkstemp(dir=self.cache_dir, prefix="w-")
            h.fd, h.temp = fd, Path(name)
            h.loaded = not preload
        with self._lock:
            fh = self._next_fh
            self._next_fh += 1
            self._handles[fh] = h
            if writable:
                self._pending[h.path] = h
        return fh

    def _ensure_loaded(self, h: Handle) -> None:
        """Copy the remote file into the handle's temp file. Caller holds h.lock."""
        if h.loaded:
            return
        node = self._file(h.path)
        offset = 0
        for chunk in self.s.index.chunks(node.id):
            data = self._chunk_bytes(chunk)
            os.pwrite(h.fd, data, offset)
            offset += len(data)
        h.loaded = True

    def _commit(self, h: Handle) -> None:
        if h.dirty and h.temp is not None and not h.unlinked:
            self._run(self.s.put(h.temp, h.path))
            h.dirty = False

    # ---- metadata -----------------------------------------------------------

    @_fuse_errors
    def getattr(self, path, fh=None):
        pending = self._pending.get(self._norm(path))
        if pending is not None and pending.fd is not None and pending.loaded:
            st = os.fstat(pending.fd)
            ns = st.st_mtime_ns
            return {
                "st_mode": stat.S_IFREG | 0o644, "st_nlink": 1, "st_size": st.st_size,
                "st_mtime": ns, "st_ctime": ns, "st_atime": ns,
                "st_uid": self._uid, "st_gid": self._gid,
            }  # fmt: skip
        node = self.s.index.require(path)
        ns = int(node.mtime * 1e9)
        mode = (stat.S_IFDIR | 0o755) if node.is_dir else (stat.S_IFREG | 0o644)
        return {
            "st_mode": mode, "st_nlink": 2 if node.is_dir else 1, "st_size": node.size,
            "st_mtime": ns, "st_ctime": ns, "st_atime": ns,
            "st_uid": self._uid, "st_gid": self._gid,
        }  # fmt: skip

    @_fuse_errors
    def readdir(self, path, fh):
        node = self.s.index.require_dir(path)
        names = {c.name for c in self.s.index.children(node.id)}
        prefix = self._norm(path).rstrip("/") + "/"
        names |= {p[len(prefix) :] for p in list(self._pending) if p.startswith(prefix) and "/" not in p[len(prefix) :]}
        return [".", "..", *sorted(names)]

    @_fuse_errors
    def mkdir(self, path, mode):
        self.s.mkdir(path)
        return 0

    @_fuse_errors
    def rmdir(self, path):
        if not self.s.index.require(path).is_dir:
            raise NotADirectory(path)
        self._run(self.s.rm(path))
        return 0

    @_fuse_errors
    def unlink(self, path):
        with self._lock:
            h = self._pending.pop(self._norm(path), None)
        if h is not None:
            with h.lock:
                h.unlinked = True
        if h is None or self.s.index.resolve(path) is not None:
            self._file(path)
            self._run(self.s.rm(path))
        return 0

    @_fuse_errors
    def rename(self, old, new):
        old_n, new_n = self._norm(old), self._norm(new)
        h = self._pending.get(old_n)
        if h is None:
            self._run(self.s.mv(old, new))
            return 0
        # Open for writing: re-point the handle so its eventual upload lands at the new name.
        with h.lock:
            if self.s.index.resolve(old_n) is not None:  # already flushed once
                self._run(self.s.mv(old_n, new_n))
            with self._lock:
                self._pending.pop(old_n, None)
                self._pending[new_n] = h
            h.path = new_n
        return 0

    @_fuse_errors
    def utimens(self, path, times=None):
        node = self.s.index.resolve(path)
        if node is not None:
            self.s.index.set_mtime(node.id, times[1] / 1e9 if times else time.time())
        return 0

    def chmod(self, path, mode):
        return 0

    def chown(self, path, uid, gid):
        return 0

    def statfs(self, path):
        # Telegram storage is effectively unlimited; report 1 PiB free.
        blocks = 2**50 // 4096
        return {"f_bsize": 4096, "f_frsize": 4096, "f_blocks": blocks, "f_bfree": blocks,
                "f_bavail": blocks, "f_files": 2**32, "f_ffree": 2**32, "f_namemax": 255}  # fmt: skip

    # ---- file I/O -----------------------------------------------------------

    @_fuse_errors
    def create(self, path, mode, fi=None):
        self.s.index.require_dir(join_path(split_path(path)[:-1]))
        fh = self._new_handle(path, writable=True, preload=False)
        self._handles[fh].dirty = True  # an empty new file must still be persisted
        return fh

    @_fuse_errors
    def open(self, path, flags):
        if self._norm(path) not in self._pending:  # a file still being written has no index entry yet
            self._file(path)
        writable = (flags & os.O_ACCMODE) != os.O_RDONLY
        truncating = writable and bool(flags & os.O_TRUNC)
        fh = self._new_handle(path, writable, preload=writable and not truncating)
        if truncating:
            self._handles[fh].dirty = True  # `: > file` must persist the now-empty file even with no write()
        return fh

    @_fuse_errors
    def read(self, path, size, offset, fh):
        h = self._handles.get(fh)
        if h is not None and h.fd is not None and h.loaded:
            with h.lock:
                return os.pread(h.fd, size, offset)
        pending = self._pending.get(self._norm(path))
        if pending is not None and pending.loaded:
            with pending.lock:
                return os.pread(pending.fd, size, offset)
        return self._read_remote(self._file(path), size, offset)

    @_fuse_errors
    def write(self, path, data, offset, fh):
        h = self._handles[fh]
        if h.fd is None:
            raise FuseOSError(errno.EBADF)
        with h.lock:
            self._ensure_loaded(h)
            n = os.pwrite(h.fd, data, offset)
            h.dirty = True
        return n

    @_fuse_errors
    def truncate(self, path, length, fh=None):
        h = self._handles.get(fh) if fh else self._pending.get(self._norm(path))
        if h is not None and h.fd is not None:
            with h.lock:
                self._ensure_loaded(h)
                os.ftruncate(h.fd, length)
                h.dirty = True
            return 0
        self._file(path)  # truncate(2) on a missing path is ENOENT, never an implicit create
        tmp = self._new_handle(path, writable=True, preload=length > 0)
        try:
            h = self._handles[tmp]
            self._ensure_loaded(h)
            os.ftruncate(h.fd, length)
            h.dirty = True
        finally:
            self.release(path, tmp)
        return 0

    @_fuse_errors
    def fsync(self, path, datasync, fh):
        h = self._handles.get(fh)
        if h is not None:
            with h.lock:
                self._commit(h)
        return 0

    @_fuse_errors
    def flush(self, path, fh):
        # close() waits for flush but NOT for release, so the upload must happen here: otherwise `cp`
        # returns before the data is on Telegram and upload errors never reach the caller.
        h = self._handles.get(fh)
        if h is not None and h.fd is not None:
            with h.lock:
                self._commit(h)
        return 0

    @_fuse_errors
    def release(self, path, fh):
        with self._lock:
            h = self._handles.pop(fh, None)
        if h is None or h.fd is None:
            return 0
        try:
            with h.lock:
                self._commit(h)
        finally:
            os.close(h.fd)
            h.temp.unlink(missing_ok=True)
            with self._lock:
                if self._pending.get(h.path) is h:
                    del self._pending[h.path]
        return 0

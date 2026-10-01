"""Drive the FUSE Operations directly (no kernel mount) against MemoryBackend."""

import asyncio
import errno
import os
import threading

import pytest

mfusepy = pytest.importorskip("mfusepy")  # noqa: F841 — also skips when libfuse is missing

from mfusepy import FuseOSError  # noqa: E402

from tgfs.backend import MemoryBackend  # noqa: E402
from tgfs.fusefs import TgfsFuse  # noqa: E402
from tgfs.index import Index  # noqa: E402
from tgfs.storage import Storage  # noqa: E402


@pytest.fixture
def fs(tmp_path):
    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()
    index = Index(tmp_path / "i.sqlite3")
    storage = Storage(index, MemoryBackend(), chunk_size=10)
    cache = tmp_path / "cache"
    cache.mkdir()
    yield TgfsFuse(storage, loop, cache, cache_chunks=2)
    loop.call_soon_threadsafe(loop.stop)
    t.join()
    index.close()


def test_create_write_read(fs):
    fh = fs.create("/a.txt", 0o644)
    assert "a.txt" in fs.readdir("/", None)  # visible before release
    fs.write("/a.txt", b"hello world, this spans chunks", 0, fh)
    assert fs.getattr("/a.txt")["st_size"] == 30
    fs.release("/a.txt", fh)

    fh = fs.open("/a.txt", os.O_RDONLY)
    assert fs.read("/a.txt", 5, 6, fh) == b"world"
    assert fs.read("/a.txt", 100, 25, fh) == b"hunks"
    assert fs.read("/a.txt", 10, 100, fh) == b""
    fs.release("/a.txt", fh)


def test_empty_file_persists(fs):
    fs.release("/empty", fs.create("/empty", 0o644))
    assert fs.getattr("/empty")["st_size"] == 0


def test_append_and_truncate(fs):
    fs.release("/f", fs.create("/f", 0o644))
    fh = fs.open("/f", os.O_WRONLY)
    fs.write("/f", b"0123456789abcdef", 0, fh)
    fs.release("/f", fh)

    fh = fs.open("/f", os.O_RDWR)
    fs.write("/f", b"XY", 16, fh)
    fs.release("/f", fh)
    fs.truncate("/f", 4)
    fh = fs.open("/f", os.O_RDONLY)
    assert fs.read("/f", 100, 0, fh) == b"0123"
    fs.release("/f", fh)


def test_open_for_write_is_lazy(fs):
    fh = fs.create("/big", 0o644)
    fs.write("/big", b"x" * 35, 0, fh)
    fs.release("/big", fh)
    backend = fs.s.backend
    downloads = []
    orig = backend.download
    backend.download = lambda mid: downloads.append(mid) or orig(mid)

    fh = fs.open("/big", os.O_WRONLY)  # e.g. `touch`: no data should move
    assert fs.getattr("/big")["st_size"] == 35
    fs.release("/big", fh)
    assert downloads == []

    fh = fs.open("/big", os.O_WRONLY)
    fs.write("/big", b"Y", 34, fh)  # partial overwrite forces the preload
    fs.release("/big", fh)
    fh = fs.open("/big", os.O_RDONLY)
    assert fs.read("/big", 100, 30, fh) == b"xxxxY"
    fs.release("/big", fh)


def test_flush_commits_before_release(fs):
    fh = fs.create("/c", 0o644)
    fs.write("/c", b"data", 0, fh)
    fs.flush("/c", fh)  # what close() waits on
    assert fs.s.index.resolve("/c").size == 4
    uploads = len(fs.s.backend.messages)
    fs.release("/c", fh)
    assert len(fs.s.backend.messages) == uploads  # nothing re-uploaded


def test_read_while_being_written(fs):
    w = fs.create("/p", 0o644)
    fs.write("/p", b"partial", 0, w)
    r = fs.open("/p", os.O_RDONLY)
    assert fs.read("/p", 100, 0, r) == b"partial"
    fs.release("/p", r)
    fs.release("/p", w)


def test_dirs_rename_unlink(fs):
    fs.mkdir("/d", 0o755)
    fs.release("/d/x", fs.create("/d/x", 0o644))
    with pytest.raises(FuseOSError) as e:
        fs.rmdir("/d")
    assert e.value.errno == errno.ENOTEMPTY
    fs.rename("/d/x", "/y")
    fs.rmdir("/d")
    fs.unlink("/y")
    assert fs.readdir("/", None) == [".", ".."]
    with pytest.raises(FuseOSError) as e:
        fs.getattr("/y")
    assert e.value.errno == errno.ENOENT


# ---- regressions found in audit ------------------------------------------------


def _write(fs, path, data):
    fh = fs.create(path, 0o644)
    fs.write(path, data, 0, fh)
    fs.flush(path, fh)
    fs.release(path, fh)


def _read_all(fs, path):
    fh = fs.open(path, os.O_RDONLY)
    try:
        return fs.read(path, 1 << 20, 0, fh)
    finally:
        fs.release(path, fh)


def test_open_with_o_trunc_and_no_write_truncates(fs):
    # `: > file` in a shell: open(O_WRONLY|O_TRUNC), close. File must end up empty.
    _write(fs, "/t", b"old content")
    fh = fs.open("/t", os.O_WRONLY | os.O_TRUNC)
    fs.flush("/t", fh)
    fs.release("/t", fh)
    assert _read_all(fs, "/t") == b""


def test_truncate_missing_file_is_enoent(fs):
    with pytest.raises(FuseOSError) as e:
        fs.truncate("/ghost", 0)
    assert e.value.errno == errno.ENOENT
    assert fs.s.index.resolve("/ghost") is None


def test_rename_while_open_for_writing(fs):
    # Editors/downloaders: create tmp, write, rename to final name, then close.
    fh = fs.create("/f.part", 0o644)
    fs.write("/f.part", b"payload", 0, fh)
    fs.rename("/f.part", "/f")
    fs.flush("/f", fh)
    fs.release("/f", fh)
    assert fs.s.index.resolve("/f.part") is None
    assert _read_all(fs, "/f") == b"payload"


def test_unlink_while_open_for_writing(fs):
    fh = fs.create("/tmpfile", 0o644)
    fs.write("/tmpfile", b"x", 0, fh)
    fs.unlink("/tmpfile")
    fs.flush("/tmpfile", fh)
    fs.release("/tmpfile", fh)
    assert fs.s.index.resolve("/tmpfile") is None
    assert "tmpfile" not in fs.readdir("/", None)

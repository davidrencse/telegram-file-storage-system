import asyncio
import os

import pytest

from tgfs.backend import MemoryBackend
from tgfs.errors import (
    AlreadyExists,
    DirectoryNotEmpty,
    IntegrityError,
    InvalidOperation,
    NotFound,
    PassphraseRequired,
)
from tgfs.index import Index, split_path
from tgfs.storage import Storage, open_cipher

CHUNK = 1000


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def index(tmp_path):
    idx = Index(tmp_path / "index.sqlite3")
    yield idx
    idx.close()


@pytest.fixture
def backend():
    return MemoryBackend()


@pytest.fixture
def storage(index, backend):
    return Storage(index, backend, chunk_size=CHUNK)


def write(path, data: bytes):
    path.write_bytes(data)
    return path


def test_split_path():
    assert split_path("/a/./b/../c/") == ["a", "c"]
    assert split_path("") == []
    assert split_path("/..") == []


@pytest.mark.parametrize("size", [0, 1, CHUNK - 1, CHUNK, CHUNK + 1, CHUNK * 3 + 7])
def test_roundtrip_chunk_boundaries(storage, backend, tmp_path, size):
    data = os.urandom(size)
    src = write(tmp_path / "src.bin", data)
    node = run(storage.put(src, "/f.bin"))
    assert node.size == size
    assert len(backend.messages) == -(-size // CHUNK)
    out = run(storage.get("/f.bin", tmp_path / "out.bin"))
    assert out.read_bytes() == data


def test_put_into_directory_and_ls(storage, tmp_path):
    storage.mkdir("/docs")
    src = write(tmp_path / "a.txt", b"hello")
    run(storage.put(src, "/docs"))
    run(storage.put(src, "/docs/b.txt"))
    assert [n.name for n in storage.ls("/docs")] == ["a.txt", "b.txt"]


def test_put_missing_parent_uploads_nothing(storage, backend, tmp_path):
    src = write(tmp_path / "a.txt", b"x" * 5000)
    with pytest.raises(NotFound):
        run(storage.put(src, "/nope/a.txt"))
    assert backend.messages == {}


def test_overwrite_deletes_old_chunks(storage, backend, tmp_path):
    run(storage.put(write(tmp_path / "a", b"1" * 2500), "/a"))
    old = set(backend.messages)
    run(storage.put(write(tmp_path / "a", b"2" * 1500), "/a"))
    assert not old & set(backend.messages)
    assert len(backend.messages) == 2


def test_failed_upload_rolls_back(storage, backend, tmp_path):
    backend.fail_after = 2
    with pytest.raises(ConnectionError):
        run(storage.put(write(tmp_path / "a", b"x" * 5000), "/a"))
    assert backend.messages == {}
    assert storage.index.resolve("/a") is None


def test_rm_recursive_deletes_messages(storage, backend, tmp_path):
    storage.mkdir("/d/e", parents=True)
    run(storage.put(write(tmp_path / "a", b"x" * 2500), "/d/e/a"))
    run(storage.put(write(tmp_path / "b", b"y"), "/d/b"))
    with pytest.raises(DirectoryNotEmpty):
        run(storage.rm("/d"))
    assert run(storage.rm("/d", recursive=True)) == 4
    assert backend.messages == {}
    assert storage.index.resolve("/d") is None


def test_mv_rename_into_dir_and_overwrite(storage, backend, tmp_path):
    storage.mkdir("/dir")
    run(storage.put(write(tmp_path / "a", b"aaa"), "/a"))
    run(storage.put(write(tmp_path / "b", b"bbb"), "/b"))
    run(storage.mv("/a", "/dir"))
    assert storage.index.resolve("/dir/a") is not None
    run(storage.mv("/b", "/dir/a"))  # overwrite: a's chunk must be deleted
    assert len(backend.messages) == 1
    out = run(storage.get("/dir/a", tmp_path / "out"))
    assert out.read_bytes() == b"bbb"


def test_mv_dir_into_itself_rejected(storage):
    storage.mkdir("/a/b", parents=True)
    with pytest.raises(InvalidOperation):
        run(storage.mv("/a", "/a/b"))


def test_mkdir_conflicts(storage):
    storage.mkdir("/x")
    with pytest.raises(AlreadyExists):
        storage.mkdir("/x")
    storage.mkdir("/x", parents=True)  # like mkdir -p


def test_put_tree_get_tree(storage, tmp_path):
    src = tmp_path / "proj"
    (src / "sub").mkdir(parents=True)
    write(src / "top.txt", b"top")
    write(src / "sub" / "deep.bin", os.urandom(3500))
    assert run(storage.put_tree(src, "/")) == 2
    assert run(storage.get_tree("/proj", tmp_path / "restore")) == 2
    assert (tmp_path / "restore/proj/sub/deep.bin").read_bytes() == (src / "sub/deep.bin").read_bytes()


def test_encryption_hides_plaintext(index, backend, tmp_path):
    storage = Storage(index, backend, chunk_size=CHUNK, cipher=open_cipher(index, "hunter2"))
    data = b"top secret " * 300
    run(storage.put(write(tmp_path / "s", data), "/s"))
    assert all(b"top secret" not in blob for blob, _, _ in backend.messages.values())
    assert run(storage.get("/s", tmp_path / "o")).read_bytes() == data

    with pytest.raises(IntegrityError):
        open_cipher(index, "wrong")
    plain = Storage(index, backend, chunk_size=CHUNK)
    with pytest.raises(PassphraseRequired):
        run(plain.get("/s", tmp_path / "o2"))


def test_tampered_chunk_detected(storage, backend, tmp_path):
    run(storage.put(write(tmp_path / "a", b"abc"), "/a"))
    mid = next(iter(backend.messages))
    backend.messages[mid] = (b"abd", "x", "")
    with pytest.raises(IntegrityError):
        run(storage.get("/a", tmp_path / "o"))
    assert not (tmp_path / "o").exists()


@pytest.mark.parametrize("passphrase", [None, "pw"])
def test_index_backup_restore(tmp_path, backend, passphrase):
    idx = Index(tmp_path / "one.sqlite3")
    cipher = open_cipher(idx, passphrase) if passphrase else None
    s1 = Storage(idx, backend, chunk_size=CHUNK, cipher=cipher)
    s1.mkdir("/keep")
    run(s1.put(write(tmp_path / "f", b"payload"), "/keep/f"))
    run(s1.backup_index())
    first = run(backend.find_latest("#tgfs-index"))
    run(s1.backup_index())  # older snapshot gets pruned
    assert first not in backend.messages

    # Fresh machine: empty index, same chat.
    idx2 = Index(tmp_path / "two.sqlite3")
    s2 = Storage(idx2, backend, chunk_size=CHUNK)
    if passphrase:
        with pytest.raises(PassphraseRequired):
            run(s2.restore_index())
    run(s2.restore_index(passphrase))
    assert run(s2.get("/keep/f", tmp_path / "out")).read_bytes() == b"payload"
    idx.close()
    idx2.close()


def test_get_tree_refuses_unsafe_names_from_index(storage, tmp_path):
    # A restored index comes from the chat; a tampered one must not write outside the target dir.
    storage.mkdir("/d")
    run(storage.put(write(tmp_path / "a", b"x"), "/d/ok"))
    node = storage.index.resolve("/d/ok")
    with storage.index._lock:
        storage.index._db.execute("UPDATE nodes SET name = '..' WHERE id = ?", (node.id,))
    with pytest.raises(InvalidOperation):
        run(storage.get_tree("/d", tmp_path / "out"))


class SlowBackend(MemoryBackend):
    """Yields on every call so parallel transfers really interleave; records peak concurrency."""

    def __init__(self) -> None:
        super().__init__()
        self.active = self.peak = 0

    async def _tick(self) -> None:
        self.active += 1
        self.peak = max(self.peak, self.active)
        await asyncio.sleep(0.001)
        self.active -= 1

    async def upload(self, data, filename, caption=""):
        await self._tick()
        return await super().upload(data, filename, caption)

    async def download(self, message_id):
        await self._tick()
        return await super().download(message_id)


@pytest.mark.parametrize("parallel", [1, 3])
def test_parallel_roundtrip_keeps_chunk_order(index, tmp_path, parallel):
    backend = SlowBackend()
    s = Storage(index, backend, chunk_size=CHUNK, parallel=parallel)
    payload = os.urandom(CHUNK * 7 + 13)
    run(s.put(write(tmp_path / "a", payload), "/a"))
    assert [c.idx for c in index.chunks(s.stat("/a").id)] == list(range(8))
    assert backend.peak == parallel
    backend.peak = 0
    assert run(s.get("/a", tmp_path / "out")).read_bytes() == payload
    assert backend.peak == parallel


def test_parallel_failure_rolls_back_in_flight_chunks(index, tmp_path):
    backend = SlowBackend()
    backend.fail_after = 4
    s = Storage(index, backend, chunk_size=CHUNK, parallel=3)
    with pytest.raises(ConnectionError):
        run(s.put(write(tmp_path / "a", b"x" * CHUNK * 9), "/a"))
    assert backend.messages == {}
    assert index.resolve("/a") is None


def test_restore_refuses_plaintext_backup_when_encryption_required(tmp_path, backend):
    idx = Index(tmp_path / "one.sqlite3")
    run(Storage(idx, backend, chunk_size=CHUNK).backup_index())
    with pytest.raises(IntegrityError):
        run(Storage(idx, backend, chunk_size=CHUNK).restore_index("pw", require_encrypted=True))
    idx.close()


def test_storage_rejects_bad_parallel(index, backend):
    with pytest.raises(ValueError):
        Storage(index, backend, chunk_size=CHUNK, parallel=0)

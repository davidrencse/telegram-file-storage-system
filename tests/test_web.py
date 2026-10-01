import os
from contextlib import asynccontextmanager

import pytest
from starlette.testclient import TestClient

from tgfs.backend import MemoryBackend
from tgfs.index import Index
from tgfs.storage import Storage
from tgfs.web import app as webapp

TOKEN = "test-token"
H = {"X-Tgfs-Token": TOKEN}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(webapp, "BACKUP_DELAY", 0.01)
    backend = MemoryBackend()
    index = Index(tmp_path / "i.sqlite3")
    storage = Storage(index, backend, chunk_size=100)

    @asynccontextmanager
    async def opener():
        yield storage

    app = webapp.create_app(opener, TOKEN, meta={"chat": "me"}, cache_dir=tmp_path)
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        yield client, storage, backend
    index.close()


def test_requires_token_and_local_host(env):
    client, _, _ = env
    assert client.get("/api/ls").status_code == 401
    assert client.get("/api/ls", headers={"X-Tgfs-Token": "nope"}).status_code == 401
    assert client.get("/api/ls", headers={**H, "Host": "evil.example:8765"}).status_code == 403
    assert client.get("/api/ls", headers=H).status_code == 200
    assert client.get("/").status_code == 200  # the page itself holds no data


def test_upload_list_download(env):
    client, _, backend = env
    data = os.urandom(350)
    r = client.put("/api/file", params={"path": "/a/b/x.bin", "mkdirs": "1"}, content=data, headers=H)
    assert r.status_code == 200, r.text
    assert len(backend.messages) == 4  # 350 bytes / 100-byte chunks
    assert [e["name"] for e in client.get("/api/ls", params={"path": "/a/b"}, headers=H).json()["entries"]] == ["x.bin"]
    got = client.get("/api/file", params={"path": "/a/b/x.bin", "t": TOKEN})
    assert got.content == data
    assert "attachment" in got.headers["content-disposition"]
    info = client.get("/api/info", headers=H).json()
    assert (info["files"], info["bytes"]) == (1, 350)


def test_upload_conflict_and_overwrite(env):
    client, _, backend = env
    client.put("/api/file", params={"path": "/f"}, content=b"one", headers=H)
    assert client.put("/api/file", params={"path": "/f"}, content=b"two", headers=H).status_code == 409
    r = client.put("/api/file", params={"path": "/f", "overwrite": "1"}, content=b"two", headers=H)
    assert r.status_code == 200
    assert client.get("/api/file", params={"path": "/f"}, headers=H).content == b"two"
    assert len(backend.messages) == 1  # old chunk deleted


def test_upload_missing_parent_404(env):
    client, _, backend = env
    r = client.put("/api/file", params={"path": "/nope/f"}, content=b"x", headers=H)
    assert r.status_code == 404
    assert backend.messages == {}


def test_mkdir_mv_rm(env):
    client, storage, backend = env
    assert client.post("/api/mkdir", json={"path": "/docs"}, headers=H).status_code == 200
    assert client.post("/api/mkdir", json={"path": "/docs"}, headers=H).status_code == 409
    client.put("/api/file", params={"path": "/docs/a.txt"}, content=b"hi", headers=H)
    assert client.post("/api/mv", json={"src": "/docs/a.txt", "dst": "/docs/b.txt"}, headers=H).status_code == 200
    assert storage.index.resolve("/docs/b.txt") is not None
    assert client.post("/api/rm", json={"paths": ["/docs"]}, headers=H).json() == {"chunks": 1}
    assert storage.index.resolve("/docs") is None
    assert not [m for m in backend.messages.values() if m[2] != "#tgfs-index"]


def test_preview_is_safe(env):
    client, _, _ = env
    for name, body in [("/p.html", b"<script>alert(1)</script>"), ("/p.svg", b"<svg/>"), ("/n.txt", b"hi")]:
        client.put("/api/file", params={"path": name}, content=body, headers=H)
    html = client.get("/api/file", params={"path": "/p.html", "inline": "1"}, headers=H)
    assert html.headers["content-disposition"].startswith("attachment")
    svg = client.get("/api/file", params={"path": "/p.svg", "inline": "1"}, headers=H)
    assert svg.headers["content-disposition"].startswith("attachment")
    txt = client.get("/api/file", params={"path": "/n.txt", "inline": "1"}, headers=H)
    assert txt.headers["content-disposition"].startswith("inline")
    assert txt.headers["content-type"].startswith("text/plain")
    assert "default-src 'none'" in txt.headers["content-security-policy"]


def test_changes_trigger_index_backup(env):
    client, _, backend = env
    client.post("/api/mkdir", json={"path": "/x"}, headers=H)
    client.post("/api/backup", headers=H)
    assert any(cap == "#tgfs-index" for _, _, cap in backend.messages.values())


def test_unicode_filename_download(env):
    client, _, _ = env
    client.put("/api/file", params={"path": "/résumé 简历.pdf"}, content=b"%PDF", headers=H)
    r = client.get("/api/file", params={"path": "/résumé 简历.pdf"}, headers=H)
    assert r.status_code == 200
    assert "filename*=UTF-8''r%C3%A9sum%C3%A9%20%E7%AE%80%E5%8E%86.pdf" in r.headers["content-disposition"]


def test_tailscale_host_allowed_only_when_configured(tmp_path):
    storage = Storage(Index(tmp_path / "t.sqlite3"), MemoryBackend())

    @asynccontextmanager
    async def opener():
        yield storage

    app = webapp.create_app(opener, TOKEN, allowed_hosts={"100.101.102.103", "pc.tail1234.ts.net"})
    with TestClient(app, base_url="http://100.101.102.103:8765") as client:
        assert client.get("/api/ls", headers=H).status_code == 200
        assert client.get("/api/ls", headers={**H, "Host": "PC.tail1234.ts.net:8765"}).status_code == 200
        assert client.get("/api/ls", headers={**H, "Host": "192.168.1.5:8765"}).status_code == 403
        assert client.get("/api/ls", headers={"Host": "100.101.102.103:8765"}).status_code == 401  # still needs token

"""Local web UI: a small JSON API over Storage plus a single static page.

Security model: binds to 127.0.0.1 only; every /api request must carry the random per-run token
(header `X-Tgfs-Token`, or `?t=` for plain download links); the Host header must be local, which blocks
DNS-rebinding attacks from websites open in the same browser."""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
import mimetypes
import os
import tempfile
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from ..errors import (
    AlreadyExists,
    DirectoryNotEmpty,
    IntegrityError,
    InvalidOperation,
    IsADirectory,
    NotADirectory,
    NotFound,
    PassphraseRequired,
    TgfsError,
)
from ..index import Node, join_path, split_path
from ..storage import Storage

log = logging.getLogger("tgfs.web")
STATIC = Path(__file__).parent / "static"
LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]"}
BACKUP_DELAY = 3.0  # seconds of quiet before snapshotting the index after changes

_STATUS: dict[type[TgfsError], int] = {
    NotFound: 404,
    AlreadyExists: 409,
    DirectoryNotEmpty: 409,
    IsADirectory: 400,
    NotADirectory: 400,
    InvalidOperation: 400,
    PassphraseRequired: 403,
    IntegrityError: 502,
}


def _entry(node: Node, parent: str) -> dict[str, Any]:
    return {
        "name": node.name,
        "path": join_path([*split_path(parent), node.name]),
        "is_dir": node.is_dir,
        "size": node.size,
        "mtime": node.mtime,
    }


def _hostname(host: str) -> str:
    """Strip the port: 'localhost:8765' -> 'localhost', '[::1]:8765' -> '[::1]'."""
    if host.startswith("["):
        return host[: host.find("]") + 1]
    return host.split(":", 1)[0]


def _preview_type(name: str, want_inline: bool) -> tuple[str, bool]:
    """Pick (content type, inline?). Only passive types render in the browser; HTML, SVG, XML etc. are
    always downloaded so a stored file can never execute script against this API."""
    media = mimetypes.guess_type(name)[0] or "application/octet-stream"
    if not want_inline:
        return media, False
    if media.startswith(("text/", "application/json")) or name.lower().endswith((".md", ".log")):
        if media in ("text/html", "text/xml"):
            return media, False
        return "text/plain; charset=utf-8", True
    if media == "image/svg+xml":
        return media, False
    if media.startswith(("image/", "audio/", "video/")) or media == "application/pdf":
        return media, True
    return media, False


def _disposition(name: str, inline: bool) -> str:
    kind = "inline" if inline else "attachment"
    ascii_name = name.encode("ascii", "replace").decode().replace('"', "_")
    return f"{kind}; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name)}"


class _State:
    def __init__(self) -> None:
        self.storage: Storage | None = None
        self.jobs: dict[str, dict[str, Any]] = {}
        self.meta: dict[str, Any] = {}
        self.dirty = False
        self.backup_task: asyncio.Task | None = None

    @property
    def s(self) -> Storage:
        assert self.storage is not None
        return self.storage

    def changed(self) -> None:
        """Debounce index snapshots: bulk uploads trigger one backup, not hundreds."""
        self.dirty = True
        if self.backup_task is not None:
            self.backup_task.cancel()
        self.backup_task = asyncio.create_task(self._backup_later())

    async def _backup_later(self) -> None:
        await asyncio.sleep(BACKUP_DELAY)
        try:
            await self.flush_backup()
        except Exception:  # retried on the next change / at shutdown; never crash the server
            log.exception("index backup failed")

    async def flush_backup(self) -> None:
        if self.dirty:
            self.dirty = False
            try:
                await self.s.backup_index()
            except Exception:
                self.dirty = True
                raise


def create_app(
    open_storage: Callable[[], AbstractAsyncContextManager[Storage]],
    token: str,
    meta: dict[str, Any] | None = None,
    cache_dir: Path | None = None,
    on_ready: Callable[[], None] | None = None,
    allowed_hosts: set[str] | None = None,
) -> Starlette:
    """`allowed_hosts`: extra Host header values to accept besides localhost (e.g. the Tailscale IP / name)."""
    hosts = LOCAL_HOSTS | {h.lower().rstrip(".") for h in allowed_hosts or ()}
    state = _State()
    state.meta = dict(meta or {})
    tmp_dir = cache_dir or Path(tempfile.gettempdir())

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        async with open_storage() as storage:
            state.storage = storage
            client = getattr(storage.backend, "client", None)
            if client is not None:
                me = await client.get_me()
                state.meta["account"] = f"{me.first_name or ''} (@{me.username})" if me.username else me.first_name
            if on_ready:
                on_ready()
            try:
                yield
            finally:
                if state.backup_task is not None:
                    state.backup_task.cancel()
                await state.flush_backup()

    def guard(handler):
        async def wrapped(request: Request) -> Response:
            if _hostname(request.headers.get("host", "")).lower().rstrip(".") not in hosts:
                return JSONResponse({"error": "forbidden host"}, 403)
            given = request.headers.get("x-tgfs-token") or request.query_params.get("t") or ""
            if not hmac.compare_digest(given, token):
                return JSONResponse({"error": "bad or missing token"}, 401)
            try:
                return await handler(request)
            except TgfsError as e:
                return JSONResponse({"error": str(e), "kind": type(e).__name__}, _STATUS.get(type(e), 500))
            except KeyError as e:
                return JSONResponse({"error": f"missing parameter {e}"}, 400)

        return wrapped

    async def index_page(request: Request) -> Response:
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})

    @guard
    async def info(request: Request) -> Response:
        files, dirs, size = state.s.index.usage()
        return JSONResponse({**state.meta, "files": files, "dirs": dirs, "bytes": size,
                             "encrypted": state.s.cipher is not None})  # fmt: skip

    @guard
    async def ls(request: Request) -> Response:
        path = join_path(split_path(request.query_params.get("path", "/")))
        node = state.s.index.require_dir(path)
        entries = [_entry(n, path) for n in state.s.index.children(node.id)]
        return JSONResponse({"path": path, "entries": entries})

    @guard
    async def jobs(request: Request) -> Response:
        return JSONResponse(state.jobs)

    @guard
    async def upload(request: Request) -> Response:
        path = join_path(split_path(request.query_params["path"]))
        parts = split_path(path)
        if not parts:
            raise InvalidOperation("missing file name")
        parent = join_path(parts[:-1])
        if request.query_params.get("mkdirs"):
            state.s.mkdir(parent, parents=True)
        state.s.index.require_dir(parent)  # fail fast, before receiving the body
        existing = state.s.index.resolve(path)
        if existing is not None and not request.query_params.get("overwrite"):
            raise AlreadyExists(path)

        job_id = request.query_params.get("job") or os.urandom(6).hex()
        job = state.jobs[job_id] = {"path": path, "phase": "receiving", "done": 0, "total": 0}
        fd, tmp = tempfile.mkstemp(dir=tmp_dir, prefix="ui-")
        try:
            with os.fdopen(fd, "wb") as f:
                async for part in request.stream():
                    f.write(part)
                    job["done"] += len(part)
            job.update(phase="telegram", done=0, total=os.path.getsize(tmp))

            def tick(n: int) -> None:
                job["done"] += n

            node = await state.s.put(tmp, path, tick)
            state.changed()
            return JSONResponse(_entry(node, parent))
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp)
            state.jobs.pop(job_id, None)

    @guard
    async def download(request: Request) -> Response:
        node = state.s.index.require(request.query_params["path"])
        if node.is_dir:
            raise IsADirectory(request.query_params["path"])
        chunks = state.s.index.chunks(node.id)
        storage = state.s

        async def body() -> AsyncIterator[bytes]:
            for chunk in chunks:
                yield await storage.read_chunk(chunk)

        media, inline = _preview_type(node.name, bool(request.query_params.get("inline")))
        headers = {
            "Content-Length": str(node.size),
            "Content-Disposition": _disposition(node.name, inline),
            "X-Content-Type-Options": "nosniff",
            # Defence in depth: nothing served here may run scripts in our origin (it could call the API).
            "Content-Security-Policy": "default-src 'none'; img-src 'self'; media-src 'self'; "
            "object-src 'self'; style-src 'unsafe-inline'",
        }
        return StreamingResponse(body(), media_type=media, headers=headers)

    @guard
    async def mkdir(request: Request) -> Response:
        body = await request.json()
        node = state.s.mkdir(body["path"], parents=bool(body.get("parents")))
        state.changed()
        return JSONResponse({"id": node.id})

    @guard
    async def move(request: Request) -> Response:
        body = await request.json()
        await state.s.mv(body["src"], body["dst"])
        state.changed()
        return JSONResponse({"ok": True})

    @guard
    async def remove(request: Request) -> Response:
        body = await request.json()
        removed = 0
        for path in body["paths"]:
            removed += await state.s.rm(path, recursive=True)
        state.changed()
        return JSONResponse({"chunks": removed})

    @guard
    async def backup(request: Request) -> Response:
        state.dirty = True
        await state.flush_backup()
        return JSONResponse({"ok": True})

    routes = [
        Route("/", index_page),
        Route("/api/info", info),
        Route("/api/ls", ls),
        Route("/api/jobs", jobs),
        Route("/api/file", upload, methods=["PUT"]),
        Route("/api/file", download, methods=["GET"]),
        Route("/api/mkdir", mkdir, methods=["POST"]),
        Route("/api/mv", move, methods=["POST"]),
        Route("/api/rm", remove, methods=["POST"]),
        Route("/api/backup", backup, methods=["POST"]),
    ]
    return Starlette(routes=routes, lifespan=lifespan)

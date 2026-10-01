"""`tgfs` command line."""

from __future__ import annotations

import asyncio
import datetime as dt
import getpass
import os
import socket
import threading
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.progress import BarColumn, DownloadColumn, Progress, TransferSpeedColumn
from rich.table import Table

from . import system
from .config import Config
from .errors import IntegrityError, TgfsError
from .index import Index
from .session import make_client, open_storage, secure_session_file
from .storage import META_SALT, Storage, open_cipher

app = typer.Typer(help="Store files in your Telegram account.", no_args_is_help=True)
console = Console()
err = Console(stderr=True)


class _Passphrase:
    """Asks at most once per process (remembers the answer); `forget()` after a wrong one."""

    def __init__(self) -> None:
        self.value: str | None = None
        self.source = ""  # "env" | "keyring" | "prompt"

    def forget(self) -> None:
        self.value = None

    def __call__(self, first_time: bool = False) -> str:
        if self.value is None:
            self.value = self._ask(first_time)
        return self.value

    def _ask(self, first_time: bool) -> str:
        if pw := os.environ.get("TGFS_PASSPHRASE"):
            self.source = "env"
            return pw
        if not first_time and (pw := system.keyring_get()):
            self.source = "keyring"
            return pw
        self.source = "prompt"
        try:
            return self._prompt(first_time)
        except EOFError:  # no terminal, e.g. running as a systemd service
            raise TgfsError("no passphrase available — run `tgfs keyring-save` once in a terminal") from None

    @staticmethod
    def _prompt(first_time: bool) -> str:
        if not first_time:
            return getpass.getpass("tgfs passphrase: ")
        # This passphrase becomes the permanent key: a typo here would lock the data forever.
        err.print("No encryption key yet — choose a passphrase. [bold]It cannot be recovered or changed.[/bold]")
        pw = getpass.getpass("new tgfs passphrase: ")
        if len(pw) < 8:
            raise TgfsError("passphrase must be at least 8 characters")
        if getpass.getpass("repeat passphrase: ") != pw:
            raise TgfsError("passphrases do not match")
        return pw


_passphrase = _Passphrase()
PASSPHRASE_ATTEMPTS = 3


def _unlock(cfg: Config) -> None:
    """Check the passphrase against the local index *before* connecting to Telegram, re-asking on a typo.
    Everything that opens storage afterwards reuses the verified value."""
    if not cfg.encrypt:
        return
    cfg.ensure_home()
    index = Index(cfg.db_path)
    try:
        for attempt in range(1, PASSPHRASE_ATTEMPTS + 1):
            first_time = index.get_meta(META_SALT) is None
            try:
                open_cipher(index, _passphrase(first_time))
                return
            except IntegrityError:
                source = _passphrase.source
                _passphrase.forget()
                if source == "env":
                    raise TgfsError("TGFS_PASSPHRASE is set to the wrong passphrase") from None
                if source == "keyring":
                    raise TgfsError("the passphrase saved in your keyring is wrong — run `tgfs keyring-forget`, "
                                    "then `tgfs keyring-save`") from None  # fmt: skip
                if attempt == PASSPHRASE_ATTEMPTS:
                    raise TgfsError(f"wrong passphrase ({PASSPHRASE_ATTEMPTS} attempts)") from None
                err.print("[red]Wrong passphrase[/red] — try again.")
    finally:
        index.close()


def _fail(e: Exception) -> typer.Exit:
    label = type(e).__name__ if isinstance(e, TgfsError) and type(e) is not TgfsError else "error"
    err.print(f"[red]{label}:[/red] {e}")
    return typer.Exit(1)


def _run[T](fn: Callable[[Storage], Awaitable[T]], *, backup: bool = False, with_cipher: bool = True) -> T:
    """Open storage, run fn, optionally snapshot the index to Telegram afterwards."""
    with_cipher = with_cipher or backup  # an encrypted setup must never upload a plaintext index

    cfg = Config.load()

    async def main() -> T:
        async with open_storage(cfg, _passphrase, with_cipher=with_cipher) as storage:
            result = await fn(storage)
            if backup:
                await storage.backup_index()
            return result

    try:
        if with_cipher:
            _unlock(cfg)
        return asyncio.run(main())
    except (TgfsError, OSError) as e:  # OSError: e.g. local destination directory missing / not writable
        raise _fail(e) from None


def _human(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    raise AssertionError


def _progress(total: int) -> Progress:
    p = Progress("[progress.description]{task.description}", BarColumn(), DownloadColumn(),
                 TransferSpeedColumn(), console=err, transient=True)  # fmt: skip
    p.add_task("transfer", total=total)
    return p


def _local_size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file() and not p.is_symlink())


# ---- account ------------------------------------------------------------------


@app.command()
def login(
    chat: Annotated[str | None, typer.Option(help='Storage chat: "me" (Saved Messages), channel id, or @name')] = None,
    encrypt: Annotated[bool | None, typer.Option(help="Encrypt chunks client-side (AES-256-GCM)")] = None,
) -> None:
    """Log in with your Telegram account (phone + code + optional 2FA password)."""
    cfg = Config.load()
    if not cfg.api_id or not cfg.api_hash:
        console.print("Create an app at [link]https://my.telegram.org/apps[/link] to get api_id / api_hash.")
        cfg.api_id = typer.prompt("api_id", type=int)
        cfg.api_hash = typer.prompt("api_hash", hide_input=True)
    if chat is not None:
        cfg.chat = chat
    if encrypt is None and not cfg.config_path.exists():
        encrypt = typer.confirm("Encrypt files before upload?", default=True)
    if encrypt is not None:
        cfg.encrypt = encrypt
    cfg.save()

    async def main() -> str:
        client = make_client(cfg)
        await client.start()  # interactive: phone, login code, 2FA password
        try:
            me = await client.get_me()
            return f"{me.first_name or ''} (@{me.username or '-'}, id {me.id})"
        finally:
            await client.disconnect()

    who = asyncio.run(main())
    secure_session_file(cfg)
    console.print(f"[green]Logged in as[/green] {who}. Session: {cfg.session_path}.session (keep private!)")
    if cfg.encrypt:
        console.print("Encryption on. Set TGFS_PASSPHRASE or you'll be prompted. [bold]Lose it = lose data.[/bold]")


@app.command()
def logout() -> None:
    """Terminate the Telegram session and delete the local session file."""
    cfg = Config.load()

    async def main() -> None:
        client = make_client(cfg)
        await client.connect()
        try:
            if await client.is_user_authorized():
                await client.log_out()
        finally:
            await client.disconnect()

    asyncio.run(main())
    cfg.session_path.with_suffix(".session").unlink(missing_ok=True)
    console.print("Logged out. Local index kept at " + str(cfg.db_path))


@app.command()
def info() -> None:
    """Show account, storage chat and usage."""
    cfg = Config.load()

    async def fn(s: Storage) -> None:
        me = await s.backend.client.get_me()
        files, dirs, size = s.index.usage()
        console.print(f"account : {me.first_name} (@{me.username or '-'})")
        console.print(f"chat    : {cfg.chat}")
        console.print(f"home    : {cfg.home}")
        console.print(f"encrypt : {cfg.encrypt}   chunk: {_human(cfg.chunk_size)}")
        console.print(f"usage   : {files} files, {dirs} dirs, {_human(size)}")

    _run(fn, with_cipher=False)


# ---- files ----------------------------------------------------------------------


@app.command()
def ls(
    path: Annotated[str, typer.Argument()] = "/",
    long: Annotated[bool, typer.Option("-l", "--long")] = False,
) -> None:
    """List a directory."""

    async def fn(s: Storage) -> None:
        nodes = s.ls(path)
        if not long:
            for n in nodes:
                console.print(f"[bold blue]{n.name}/[/]" if n.is_dir else n.name, highlight=False)
            return
        t = Table(box=None, show_header=False)
        for n in nodes:
            when = dt.datetime.fromtimestamp(n.mtime).strftime("%Y-%m-%d %H:%M")
            t.add_row("d" if n.is_dir else "-", "" if n.is_dir else _human(n.size), when,
                      f"[bold blue]{n.name}/[/]" if n.is_dir else n.name)  # fmt: skip
        console.print(t)

    _run(fn, with_cipher=False)


@app.command()
def put(
    sources: Annotated[list[Path], typer.Argument(exists=True, help="Local files/dirs")],
    dest: Annotated[str, typer.Argument(help="Remote file or directory")],
    recursive: Annotated[bool, typer.Option("-r", "--recursive")] = False,
) -> None:
    """Upload files (and directories with -r)."""

    async def fn(s: Storage) -> None:
        if len(sources) > 1 and not (s.index.resolve(dest) or dest.endswith("/")):
            s.mkdir(dest, parents=True)
        with _progress(sum(_local_size(p) for p in sources)) as bar:
            tick = lambda n: bar.advance(bar.task_ids[0], n)  # noqa: E731
            for src in sources:
                if src.is_dir():
                    if not recursive:
                        err.print(f"skip {src}: is a directory (use -r)")
                        continue
                    n = await s.put_tree(src, dest, tick)
                    console.print(f"{src}/ -> {dest} ({n} files)")
                else:
                    node = await s.put(src, dest, tick)
                    console.print(f"{src} -> {s.index.path_of(node.id)}")

    _run(fn, backup=True)


@app.command()
def get(
    source: Annotated[str, typer.Argument(help="Remote path")],
    dest: Annotated[Path, typer.Argument(help="Local file or directory")] = Path("."),
    recursive: Annotated[bool, typer.Option("-r", "--recursive")] = False,
) -> None:
    """Download a file (or a directory with -r)."""

    async def fn(s: Storage) -> None:
        node = s.stat(source)
        if node.is_dir:
            if not recursive:
                raise TgfsError(f"{source} is a directory (use -r)")
            total = sum(c.size for c in _walk_files(s, node.id))
            with _progress(total) as bar:
                n = await s.get_tree(source, dest, lambda k: bar.advance(bar.task_ids[0], k))
            console.print(f"{source} -> {dest} ({n} files)")
        else:
            with _progress(node.size) as bar:
                out = await s.get(source, dest, lambda k: bar.advance(bar.task_ids[0], k))
            console.print(f"{source} -> {out}")

    _run(fn)


def _walk_files(s: Storage, node_id: int):
    for child in s.index.children(node_id):
        if child.is_dir:
            yield from _walk_files(s, child.id)
        else:
            yield child


@app.command()
def rm(
    paths: Annotated[list[str], typer.Argument()],
    recursive: Annotated[bool, typer.Option("-r", "--recursive")] = False,
) -> None:
    """Delete files/directories (also deletes their messages from Telegram)."""

    async def fn(s: Storage) -> None:
        for p in paths:
            n = await s.rm(p, recursive=recursive)
            console.print(f"removed {p} ({n} chunks)")

    _run(fn, backup=True, with_cipher=False)


@app.command()
def mkdir(
    path: str,
    parents: Annotated[bool, typer.Option("-p", "--parents")] = False,
) -> None:
    """Create a directory."""

    async def fn(s: Storage) -> None:
        s.mkdir(path, parents=parents)

    _run(fn, backup=True, with_cipher=False)


@app.command()
def mv(src: str, dst: str) -> None:
    """Move or rename."""

    async def fn(s: Storage) -> None:
        await s.mv(src, dst)

    _run(fn, backup=True, with_cipher=False)


@app.command()
def stat(path: str) -> None:
    """Show metadata and chunk layout."""

    async def fn(s: Storage) -> None:
        n = s.stat(path)
        console.print(f"path   : {s.index.path_of(n.id)}\ntype   : {'dir' if n.is_dir else 'file'}")
        if not n.is_dir:
            chunks = s.index.chunks(n.id)
            console.print(f"size   : {n.size} ({_human(n.size)})\nsha256 : {n.sha256}")
            console.print(f"chunks : {len(chunks)} (msg ids {[c.message_id for c in chunks]})")
            console.print(f"crypt  : {any(c.encrypted for c in chunks)}")
        console.print(f"mtime  : {dt.datetime.fromtimestamp(n.mtime).isoformat(timespec='seconds')}")

    _run(fn, with_cipher=False)


# ---- index ----------------------------------------------------------------------


@app.command()
def backup() -> None:
    """Upload a snapshot of the index to Telegram (done automatically after changes)."""
    mid = _run(lambda s: s.backup_index())
    console.print(f"index snapshot stored as message {mid}")


@app.command()
def restore(force: Annotated[bool, typer.Option("--force")] = False) -> None:
    """Replace the local index with the newest snapshot from Telegram (new machine / lost disk)."""
    cfg = Config.load()
    if cfg.db_path.exists() and not force:
        typer.confirm(f"Overwrite local index {cfg.db_path}?", abort=True)

    async def fn(s: Storage) -> int:
        return await s.restore_index(_passphrase() if cfg.encrypt else None)

    mid = _run(fn, with_cipher=False)
    console.print(f"restored index from message {mid}")


# ---- mount ----------------------------------------------------------------------


@app.command()
def mount(
    mountpoint: Annotated[Path, typer.Argument(exists=True, file_okay=False)],
    debug: Annotated[bool, typer.Option()] = False,
) -> None:
    """Mount as a FUSE file system (foreground; Ctrl-C or `fusermount3 -u` to unmount)."""
    try:
        from mfusepy import FUSE

        from .fusefs import TgfsFuse
    except (ImportError, OSError) as e:
        err.print(f"[red]FUSE unavailable:[/red] {e}. Install with `uv sync --extra fuse` and libfuse3.")
        raise typer.Exit(1) from None

    cfg = Config.load()
    try:
        _unlock(cfg)
    except TgfsError as e:
        raise _fail(e) from None
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True, name="tgfs-asyncio")
    thread.start()
    cm = open_storage(cfg, _passphrase)  # any passphrase prompt happens here, before FUSE takes over
    try:
        storage = asyncio.run_coroutine_threadsafe(cm.__aenter__(), loop).result()
    except TgfsError as e:
        loop.call_soon_threadsafe(loop.stop)
        raise _fail(e) from None
    try:
        console.print(f"mounted {cfg.chat} at {mountpoint} — Ctrl-C to unmount")
        FUSE(TgfsFuse(storage, loop, cfg.cache_dir), str(mountpoint), foreground=True,
             nothreads=False, debug=debug, fsname="tgfs")  # fmt: skip
    finally:
        asyncio.run_coroutine_threadsafe(storage.backup_index(), loop).result()
        asyncio.run_coroutine_threadsafe(cm.__aexit__(None, None, None), loop).result()
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)


def _bind_local(port: int | None, host: str = "127.0.0.1") -> socket.socket:
    """Bind `host`: the requested port, else the first free one in 8765-8784, else any free port."""
    candidates = [port] if port else [*range(8765, 8785), 0]
    for p in candidates:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind((host, p))
        except OSError:
            sock.close()
            continue
        sock.listen(128)
        return sock
    raise TgfsError(f"port {port} on {host} is already in use — try another with --port, or omit it to auto-pick")


def _ui_token(cfg: Config, keep: bool) -> str:
    """Random per run; with `keep`, reused across restarts so phone bookmarks keep working."""
    import secrets

    path = cfg.home / "ui-token"
    if keep and path.exists():
        return path.read_text().strip()
    token = secrets.token_urlsafe(24)
    if keep:
        cfg.ensure_home()
        path.write_text(token)
        path.chmod(0o600)
    return token


@app.command()
def ui(
    port: Annotated[int | None, typer.Option(help="Port (default: first free one from 8765)")] = None,
    browser: Annotated[bool, typer.Option("--browser/--no-browser", help="Open the browser automatically")] = True,
    tailscale: Annotated[bool, typer.Option(help="Also serve on your Tailscale address (your devices only)")] = False,
    keep_token: Annotated[bool, typer.Option(help="Reuse the same link across restarts (for bookmarks)")] = False,
) -> None:
    """Open the web UI (this computer only, or your Tailscale devices with --tailscale). Can run alongside `mount`."""
    import webbrowser

    import uvicorn

    from .web.app import create_app

    cfg = Config.load()
    sockets: list[socket.socket] = []
    tailnet = None
    try:
        _unlock(cfg)  # prompt + verify here: a failure inside the server's startup would print a traceback
        if tailscale:
            tailnet = system.tailscale_self()
            # Bound to the Tailscale IP only: unreachable from the internet and the local Wi-Fi.
            sockets.append(_bind_local(port or 8765, host=tailnet.ip))
            port = sockets[0].getsockname()[1]
        try:
            sockets.append(_bind_local(port))
        except TgfsError:
            if not tailscale:
                raise
            sockets.append(_bind_local(None))  # localhost port taken by something else: pick another
    except TgfsError as e:
        for s in sockets:
            s.close()
        raise _fail(e) from None
    token = _ui_token(cfg, keep_token)
    url = f"http://127.0.0.1:{sockets[-1].getsockname()[1]}/#t={token}"

    def ready() -> None:
        console.print(f"tgfs UI running — on this computer: [link={url}]{url}[/link]")
        if tailnet is not None:
            name = tailnet.dns_name or tailnet.ip
            console.print(f"on your Tailscale devices (phone etc.): http://{name}:{port}/#t={token}")
        console.print("Ctrl-C to stop.")
        if browser:
            threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()

    web = create_app(
        lambda: open_storage(cfg, _passphrase, shared=True),
        token,
        meta={"chat": cfg.chat},
        cache_dir=cfg.cache_dir,
        on_ready=ready,
        allowed_hosts=tailnet.hosts if tailnet else None,
    )
    # Sockets are bound before startup, so the links printed by ready() are guaranteed to work.
    uvicorn.Server(uvicorn.Config(web, log_level="warning")).run(sockets=sockets)


# ---- keyring & autostart -----------------------------------------------------------


@app.command("keyring-save")
def keyring_save() -> None:
    """Save your passphrase in the desktop keyring so tgfs (and autostart) never has to ask."""
    cfg = Config.load()
    if not cfg.encrypt:
        raise _fail(TgfsError("encryption is off — there is no passphrase to save"))
    try:
        _unlock(cfg)
        system.keyring_set(_passphrase())
    except TgfsError as e:
        raise _fail(e) from None
    console.print("[green]Passphrase saved to your keyring.[/green] tgfs won't ask for it on this computer anymore.")


@app.command("keyring-forget")
def keyring_forget() -> None:
    """Remove the saved passphrase from the desktop keyring."""
    system.keyring_clear()
    console.print("Removed from keyring (if it was there).")


@app.command()
def autostart(
    action: Annotated[str, typer.Argument(help="on | off | status")] = "status",
    mount_at: Annotated[Path | None, typer.Option("--mount", help="Also auto-mount at this folder, e.g. ~/tg")] = None,
    port: Annotated[int, typer.Option(help="UI port on your Tailscale address")] = 8765,
) -> None:
    """Start the UI (reachable from your Tailscale devices) — and optionally the mount — when you log in."""
    import sys

    units = ["tgfs-ui.service", "tgfs-mount.service"]
    try:
        if action == "status":
            for unit in units:
                path = system.UNIT_DIR / unit
                state = "installed" if path.exists() else "not installed"
                console.print(f"{unit}: {state}")
            token_file = Config.load().home / "ui-token"
            if token_file.exists():
                tn = system.tailscale_self()
                token = token_file.read_text().strip()
                console.print(f"phone link: http://{tn.dns_name or tn.ip}:{port}/#t={token}")
            return
        if action == "off":
            for unit in units:
                if (system.UNIT_DIR / unit).exists():
                    system.systemctl("disable", "--now", unit)
                    (system.UNIT_DIR / unit).unlink()
            system.systemctl("daemon-reload")
            console.print("Autostart removed.")
            return
        if action != "on":
            raise TgfsError("action must be on, off or status")

        cfg = Config.load()
        if cfg.encrypt and system.keyring_get() is None:
            raise TgfsError("run `tgfs keyring-save` first — a background service can't ask for your passphrase")
        tn = system.tailscale_self()
        tgfs_bin = Path(sys.executable).with_name("tgfs")
        if not tgfs_bin.exists():
            raise TgfsError(f"can't find the tgfs executable next to {sys.executable}")
        system.UNIT_DIR.mkdir(parents=True, exist_ok=True)
        (system.UNIT_DIR / "tgfs-ui.service").write_text(system.ui_unit(tgfs_bin, port))
        enable = ["tgfs-ui.service"]
        if mount_at is not None:
            try:
                import mfusepy  # noqa: F401
            except (ImportError, OSError):
                raise TgfsError("FUSE support missing — run `uv sync --all-extras` first") from None
            unit = system.mount_unit(tgfs_bin, mount_at.expanduser().resolve())
            (system.UNIT_DIR / "tgfs-mount.service").write_text(unit)
            enable.append("tgfs-mount.service")
        system.systemctl("daemon-reload")
        for unit in enable:
            system.systemctl("enable", "--now", unit)
        token = _ui_token(cfg, keep=True)
    except TgfsError as e:
        raise _fail(e) from None
    console.print("[green]Autostart on.[/green] Starts every time you log in.")
    console.print(f"Bookmark this on your phone: [bold]http://{tn.dns_name or tn.ip}:{port}/#t={token}[/bold]")
    if mount_at is not None:
        console.print(f"Files also appear at {mount_at} automatically.")
    console.print("Check it: `systemctl --user status tgfs-ui`  ·  logs: `journalctl --user -u tgfs-ui -f`")


def main() -> None:
    app()


if __name__ == "__main__":
    main()

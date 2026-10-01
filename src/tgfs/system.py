"""OS integration: Tailscale discovery, the desktop keyring (Secret Service via `secret-tool`), and
systemd user units for starting the UI / mount at login. Everything shells out to standard tools, so
nothing here needs extra Python dependencies."""

from __future__ import annotations

import ipaddress
import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .errors import TgfsError

KEYRING_ATTRS = ("service", "tgfs", "key", "passphrase")
UNIT_DIR = Path.home() / ".config" / "systemd" / "user"
TAILNET = ipaddress.ip_network("100.64.0.0/10")  # Tailscale's CGNAT range


# ---- Tailscale -------------------------------------------------------------------


@dataclass(frozen=True)
class Tailnet:
    ip: str  # this machine's Tailscale IPv4
    hostname: str  # short MagicDNS name, e.g. "czrarchlinux"
    dns_name: str  # full name, e.g. "czrarchlinux.tail1234.ts.net" ("" if MagicDNS is off)

    @property
    def hosts(self) -> set[str]:
        return {h for h in (self.ip, self.hostname.lower(), self.dns_name.lower()) if h}


def tailscale_self(run=subprocess.run) -> Tailnet:
    if shutil.which("tailscale") is None and run is subprocess.run:
        raise TgfsError("Tailscale is not installed (Arch: sudo pacman -S tailscale)")
    try:
        out = run(["tailscale", "status", "--json"], capture_output=True, text=True, timeout=10)
        data = json.loads(out.stdout or "{}")
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as e:
        raise TgfsError(f"could not query Tailscale: {e}") from None
    if data.get("BackendState") != "Running":
        raise TgfsError(f"Tailscale is not connected (state: {data.get('BackendState', 'unknown')}) — "
                        "run `sudo tailscale up` and sign in")  # fmt: skip
    me = data.get("Self") or {}
    ipv4 = [ip for ip in me.get("TailscaleIPs") or [] if ipaddress.ip_address(ip) in TAILNET]
    if not ipv4:
        raise TgfsError("Tailscale is running but this machine has no Tailscale IPv4 address")
    return Tailnet(ip=ipv4[0], hostname=me.get("HostName", ""), dns_name=(me.get("DNSName") or "").rstrip("."))


# ---- keyring ---------------------------------------------------------------------


def keyring_get(run=subprocess.run) -> str | None:
    """Passphrase from the desktop keyring, or None if absent/unavailable. Never raises."""
    if shutil.which("secret-tool") is None and run is subprocess.run:
        return None
    try:
        out = run(["secret-tool", "lookup", *KEYRING_ATTRS], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout if out.returncode == 0 and out.stdout else None


def keyring_set(passphrase: str, run=subprocess.run) -> None:
    if shutil.which("secret-tool") is None and run is subprocess.run:
        raise TgfsError("secret-tool not found (Arch: sudo pacman -S libsecret) and a keyring such as gnome-keyring")
    out = run(["secret-tool", "store", "--label=tgfs passphrase", *KEYRING_ATTRS],
              input=passphrase, capture_output=True, text=True, timeout=60)  # fmt: skip
    if out.returncode != 0:
        raise TgfsError(f"could not save to keyring: {out.stderr.strip() or 'unknown error'}")


def keyring_clear(run=subprocess.run) -> None:
    if shutil.which("secret-tool") is None and run is subprocess.run:
        return
    run(["secret-tool", "clear", *KEYRING_ATTRS], capture_output=True, timeout=10)


# ---- systemd user units ----------------------------------------------------------


def ui_unit(tgfs_bin: Path, port: int) -> str:
    return f"""[Unit]
Description=tgfs web UI (Telegram file storage, Tailscale + localhost only)
After=network-online.target

[Service]
ExecStart={tgfs_bin} ui --tailscale --port {port} --no-browser --keep-token
Restart=on-failure
RestartSec=15

[Install]
WantedBy=default.target
"""


def mount_unit(tgfs_bin: Path, mountpoint: Path) -> str:
    return f"""[Unit]
Description=tgfs FUSE mount at {mountpoint}
After=network-online.target

[Service]
ExecStartPre=/usr/bin/mkdir -p {mountpoint}
ExecStart={tgfs_bin} mount {mountpoint}
ExecStop=/usr/bin/fusermount3 -u {mountpoint}
Restart=on-failure
RestartSec=15

[Install]
WantedBy=default.target
"""


def systemctl(*args: str, run=subprocess.run) -> None:
    out = run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        raise TgfsError(f"systemctl --user {' '.join(args)} failed: {out.stderr.strip()}")

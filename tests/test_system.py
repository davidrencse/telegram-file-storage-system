import json
import subprocess
from pathlib import Path

import pytest

from tgfs import system
from tgfs.errors import TgfsError


def fake_run(stdout="", returncode=0):
    def run(cmd, **kw):
        run.calls.append((cmd, kw))
        return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr="boom")

    run.calls = []
    return run


def status(state="Running", ips=("100.101.102.103", "fd7a:115c:a1e0::1"), dns="pc.tail1234.ts.net."):
    return json.dumps({"BackendState": state, "Self": {"HostName": "PC", "DNSName": dns, "TailscaleIPs": list(ips)}})


def test_tailscale_self_parses_status():
    tn = system.tailscale_self(run=fake_run(status()))
    assert tn.ip == "100.101.102.103"
    assert tn.dns_name == "pc.tail1234.ts.net"
    assert tn.hosts == {"100.101.102.103", "pc", "pc.tail1234.ts.net"}


def test_tailscale_logged_out_is_clear_error():
    with pytest.raises(TgfsError, match="tailscale up"):
        system.tailscale_self(run=fake_run(status(state="NeedsLogin")))


def test_tailscale_rejects_non_tailnet_ip():
    with pytest.raises(TgfsError, match="no Tailscale IPv4"):
        system.tailscale_self(run=fake_run(status(ips=("192.168.1.5",))))


def test_keyring_roundtrip_commands():
    run = fake_run("secret-pass")
    assert system.keyring_get(run=run) == "secret-pass"
    assert run.calls[0][0] == ["secret-tool", "lookup", "service", "tgfs", "key", "passphrase"]
    system.keyring_set("pw", run=run)
    cmd, kw = run.calls[1]
    assert cmd[:2] == ["secret-tool", "store"] and kw["input"] == "pw"  # never on the command line
    assert system.keyring_get(run=fake_run("", returncode=1)) is None


def test_units_bind_tailscale_and_unmount_on_stop():
    ui = system.ui_unit(Path("/x/tgfs"), 8765)
    assert "ExecStart=/x/tgfs ui --tailscale --port 8765 --no-browser --keep-token" in ui
    mount = system.mount_unit(Path("/x/tgfs"), Path("/home/u/tg"))
    assert "ExecStop=/usr/bin/fusermount3 -u /home/u/tg" in mount

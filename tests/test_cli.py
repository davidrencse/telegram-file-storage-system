import pytest

from tgfs import cli
from tgfs.errors import TgfsError


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    cli._passphrase.forget()
    monkeypatch.delenv("TGFS_PASSPHRASE", raising=False)
    yield
    cli._passphrase.forget()


def feed(monkeypatch, *answers):
    it = iter(answers)
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": next(it))


def test_first_passphrase_must_be_confirmed(monkeypatch):
    feed(monkeypatch, "correct horse", "correct hose")
    with pytest.raises(TgfsError, match="do not match"):
        cli._passphrase(True)


def test_first_passphrase_min_length(monkeypatch):
    feed(monkeypatch, "short")
    with pytest.raises(TgfsError, match="at least 8"):
        cli._passphrase(True)


def test_first_passphrase_ok(monkeypatch):
    feed(monkeypatch, "correct horse", "correct horse")
    assert cli._passphrase(True) == "correct horse"


def test_existing_key_prompts_once(monkeypatch):
    feed(monkeypatch, "whatever")
    assert cli._passphrase(False) == "whatever"


def test_env_wins(monkeypatch):
    monkeypatch.setenv("TGFS_PASSPHRASE", "from-env")
    assert cli._passphrase(True) == "from-env"


def test_bind_local_skips_busy_port():
    busy = cli._bind_local(None)
    try:
        other = cli._bind_local(None)
        assert other.getsockname()[1] != busy.getsockname()[1]
        other.close()
        with pytest.raises(TgfsError, match="already in use"):
            cli._bind_local(busy.getsockname()[1])
    finally:
        busy.close()


def test_unlock_retries_wrong_passphrase(tmp_path, monkeypatch):
    from tgfs.config import Config
    from tgfs.index import Index
    from tgfs.storage import open_cipher

    cfg = Config(home=tmp_path, encrypt=True)
    idx = Index(cfg.db_path)
    open_cipher(idx, "the right one")
    idx.close()

    feed(monkeypatch, "typo", "the right one")
    cli._unlock(cfg)
    assert cli._passphrase() == "the right one"  # verified value is reused, no further prompt

    cli._passphrase.forget()
    feed(monkeypatch, "a", "b", "c")
    with pytest.raises(TgfsError, match="3 attempts"):
        cli._unlock(cfg)


def test_unlock_wrong_env_passphrase_fails_fast(tmp_path, monkeypatch):
    from tgfs.config import Config
    from tgfs.index import Index
    from tgfs.storage import open_cipher

    cfg = Config(home=tmp_path, encrypt=True)
    idx = Index(cfg.db_path)
    open_cipher(idx, "the right one")
    idx.close()
    monkeypatch.setenv("TGFS_PASSPHRASE", "nope")
    with pytest.raises(TgfsError, match="TGFS_PASSPHRASE"):
        cli._unlock(cfg)


def test_unlock_noop_without_encryption(tmp_path):
    from tgfs.config import Config

    cli._unlock(Config(home=tmp_path, encrypt=False))
    assert not (tmp_path / "index.sqlite3").exists()


def test_keyring_passphrase_used_without_prompt(monkeypatch):
    monkeypatch.setattr(cli.system, "keyring_get", lambda: "from-keyring")
    feed(monkeypatch)  # any prompt would raise StopIteration
    assert cli._passphrase(False) == "from-keyring"
    assert cli._passphrase.source == "keyring"


def test_no_tty_gives_actionable_error(monkeypatch):
    monkeypatch.setattr(cli.system, "keyring_get", lambda: None)

    def eof(prompt=""):
        raise EOFError

    monkeypatch.setattr(cli.getpass, "getpass", eof)
    with pytest.raises(TgfsError, match="keyring-save"):
        cli._passphrase(False)


def test_wrong_keyring_passphrase_explains_fix(tmp_path, monkeypatch):
    from tgfs.config import Config
    from tgfs.index import Index
    from tgfs.storage import open_cipher

    cfg = Config(home=tmp_path, encrypt=True)
    idx = Index(cfg.db_path)
    open_cipher(idx, "the right one")
    idx.close()
    monkeypatch.setattr(cli.system, "keyring_get", lambda: "stale")
    with pytest.raises(TgfsError, match="keyring-forget"):
        cli._unlock(cfg)


def test_kept_ui_token_is_stable(tmp_path):
    from tgfs.config import Config

    cfg = Config(home=tmp_path)
    a = cli._ui_token(cfg, keep=True)
    assert cli._ui_token(cfg, keep=True) == a
    assert (tmp_path / "ui-token").stat().st_mode & 0o777 == 0o600
    assert cli._ui_token(cfg, keep=False) != a

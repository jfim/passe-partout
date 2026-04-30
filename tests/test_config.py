import pytest

from passe_partout.config import Config


def test_defaults(monkeypatch):
    for k in (
        "HOST",
        "PORT",
        "MAX_TABS",
        "IDLE_TAB_CLOSE_SECONDS",
        "IDLE_CHROME_SHUTDOWN_SECONDS",
        "AUTH_TOKEN",
        "UNPACKED_EXTENSION_DIRS",
    ):
        monkeypatch.delenv(k, raising=False)
    cfg = Config.from_env()
    assert cfg.host == "127.0.0.1"
    assert cfg.port == 8000
    assert cfg.max_tabs == 10
    assert cfg.idle_tab_close_seconds == 300
    assert cfg.idle_chrome_shutdown_seconds == 300
    assert cfg.auth_token is None
    assert cfg.extension_dirs == []


def test_overrides(monkeypatch, tmp_path):
    ext_a = tmp_path / "ext_a"
    ext_b = tmp_path / "ext_b"
    ext_a.mkdir()
    ext_b.mkdir()
    monkeypatch.setenv("HOST", "0.0.0.0")
    monkeypatch.setenv("PORT", "9001")
    monkeypatch.setenv("MAX_TABS", "3")
    monkeypatch.setenv("IDLE_TAB_CLOSE_SECONDS", "60")
    monkeypatch.setenv("IDLE_CHROME_SHUTDOWN_SECONDS", "120")
    monkeypatch.setenv("AUTH_TOKEN", "secret")
    monkeypatch.setenv("UNPACKED_EXTENSION_DIRS", f"{ext_a}:{ext_b}")
    cfg = Config.from_env()
    assert cfg.host == "0.0.0.0"
    assert cfg.port == 9001
    assert cfg.max_tabs == 3
    assert cfg.idle_tab_close_seconds == 60
    assert cfg.idle_chrome_shutdown_seconds == 120
    assert cfg.auth_token == "secret"
    assert cfg.extension_dirs == [str(ext_a), str(ext_b)]


def test_extension_dir_must_exist(monkeypatch):
    monkeypatch.setenv("UNPACKED_EXTENSION_DIRS", "/nonexistent/path")
    with pytest.raises(ValueError, match="not a directory"):
        Config.from_env()

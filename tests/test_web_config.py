"""Веб-панель T1: WEB_* конфиг и утилита хеширования пароля."""
from __future__ import annotations

import io
import sys

import pytest
from argon2 import PasswordHasher

import cli
from config import Config

_WEB_ENV = (
    "WEB_HOST",
    "WEB_PORT",
    "WEB_USER",
    "WEB_PASSWORD_HASH",
    "WEB_SECRET_KEY",
    "WEB_TRUSTED_PROXIES",
    "WEB_SESSION_IDLE_MIN",
    "WEB_LOGIN_MAX_ATTEMPTS",
    "WEB_LOGIN_WINDOW_MIN",
    "WEB_ALLOW_PUBLIC",
)


@pytest.fixture(autouse=True)
def _clean_web_env(monkeypatch):
    """Дефолты не должны зависеть от локального .env разработчика."""
    for name in _WEB_ENV:
        monkeypatch.delenv(name, raising=False)


def test_web_config_defaults() -> None:
    cfg = Config()

    assert cfg.web_host == "127.0.0.1"
    assert cfg.web_port == 8000
    assert cfg.web_user == ""
    assert cfg.web_password_hash == ""
    assert cfg.web_secret_key == ""
    assert cfg.web_trusted_proxies == ""
    assert cfg.web_session_idle_min == 30
    assert cfg.web_login_max_attempts == 5
    assert cfg.web_login_window_min == 15
    assert cfg.web_allow_public == ""


def test_web_config_env_overrides(monkeypatch) -> None:
    monkeypatch.setenv("WEB_HOST", "10.1.2.3")
    monkeypatch.setenv("WEB_PORT", "9443")
    monkeypatch.setenv("WEB_USER", "admin")
    monkeypatch.setenv("WEB_PASSWORD_HASH", "$argon2id$v=19$m=65536,t=3,p=4$abc$def")
    monkeypatch.setenv("WEB_SECRET_KEY", "s" * 32)
    monkeypatch.setenv("WEB_TRUSTED_PROXIES", "127.0.0.1,10.0.0.1")
    monkeypatch.setenv("WEB_SESSION_IDLE_MIN", "45")
    monkeypatch.setenv("WEB_LOGIN_MAX_ATTEMPTS", "3")
    monkeypatch.setenv("WEB_LOGIN_WINDOW_MIN", "10")
    monkeypatch.setenv("WEB_ALLOW_PUBLIC", "YES")

    cfg = Config()

    assert cfg.web_host == "10.1.2.3"
    assert cfg.web_port == 9443
    assert cfg.web_user == "admin"
    assert cfg.web_password_hash == "$argon2id$v=19$m=65536,t=3,p=4$abc$def"
    assert cfg.web_secret_key == "s" * 32
    assert cfg.web_trusted_proxies == "127.0.0.1,10.0.0.1"
    assert cfg.web_session_idle_min == 45
    assert cfg.web_login_max_attempts == 3
    assert cfg.web_login_window_min == 10
    assert cfg.web_allow_public == "YES"


def _run_cli(monkeypatch, argv: list[str], stdin_text: str | None = None) -> int:
    monkeypatch.setattr(sys, "argv", ["cli.py", *argv])
    if stdin_text is not None:
        monkeypatch.setattr(sys, "stdin", io.StringIO(stdin_text))
    return cli.main()


def test_hash_password_stdin_prints_argon2id_hash_only(monkeypatch, capsys) -> None:
    password = "testpass12345"

    rc = _run_cli(monkeypatch, ["web-hash-password", "--stdin"], password + "\n")

    captured = capsys.readouterr()
    assert rc == 0
    lines = captured.out.splitlines()
    assert len(lines) == 1
    assert lines[0].startswith("$argon2id$")
    assert password not in captured.out
    assert password not in captured.err


def test_hash_password_printed_hash_really_verifies(monkeypatch, capsys) -> None:
    password = "testpass12345"

    rc = _run_cli(monkeypatch, ["web-hash-password", "--stdin"], password + "\n")
    stdout = capsys.readouterr().out.strip()

    assert rc == 0
    # verify() возвращает True и, что важнее, бросает исключение при несовпадении
    assert PasswordHasher().verify(stdout, password) is True
    with pytest.raises(Exception):
        PasswordHasher().verify(stdout, password + "x")


def test_hash_password_empty_stdin_exits_2_without_stdout(monkeypatch, capsys) -> None:
    rc = _run_cli(monkeypatch, ["web-hash-password", "--stdin"], "")

    captured = capsys.readouterr()
    assert rc == 2
    assert captured.out == ""
    assert captured.err.strip() != ""

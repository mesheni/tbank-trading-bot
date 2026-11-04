"""T8: команда `cli.py web` — валидация WEB_* и запуск uvicorn без токена API."""
from __future__ import annotations

import sys

import pytest

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

_HASH = "$argon2id$v=19$m=65536,t=3,p=4$c29tZXNhbHQ$c29tZWhhc2g"


@pytest.fixture(autouse=True)
def _clean_web_env(monkeypatch):
    for name in _WEB_ENV:
        monkeypatch.delenv(name, raising=False)


def _make_config(monkeypatch, **env: str) -> Config:
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    return Config()


def _valid_env(**overrides: str) -> dict[str, str]:
    env = {
        "WEB_USER": "admin",
        "WEB_PASSWORD_HASH": _HASH,
        "WEB_SECRET_KEY": "s" * 40,
    }
    env.update(overrides)
    return env


def _patch_uvicorn(monkeypatch) -> list[tuple[object, dict]]:
    calls: list[tuple[object, dict]] = []

    def fake_run(app, **kwargs):
        calls.append((app, kwargs))

    monkeypatch.setattr("uvicorn.run", fake_run)
    return calls


def test_web_help_when_requested_exits_zero(monkeypatch, capsys) -> None:
    monkeypatch.setattr(sys, "argv", ["cli.py", "web", "--help"])

    with pytest.raises(SystemExit) as excinfo:
        cli.main()

    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    assert "usage:" in out
    assert "web" in out


def test_web_when_password_hash_missing_returns_2_naming_variable(monkeypatch, capsys) -> None:
    config = _make_config(monkeypatch, WEB_USER="admin", WEB_SECRET_KEY="s" * 40)

    rc = cli.cmd_web(config)

    captured = capsys.readouterr()
    assert rc == 2
    assert "WEB_PASSWORD_HASH" in captured.err
    assert _HASH not in captured.err


def test_web_when_password_hash_not_argon2_returns_2(monkeypatch, capsys) -> None:
    config = _make_config(monkeypatch, **_valid_env(WEB_PASSWORD_HASH="plain-text"))

    rc = cli.cmd_web(config)

    captured = capsys.readouterr()
    assert rc == 2
    assert "WEB_PASSWORD_HASH" in captured.err
    assert "plain-text" not in captured.err


def test_web_when_secret_key_too_short_returns_2_naming_variable(monkeypatch, capsys) -> None:
    config = _make_config(monkeypatch, **_valid_env(WEB_SECRET_KEY="short"))

    rc = cli.cmd_web(config)

    captured = capsys.readouterr()
    assert rc == 2
    assert "WEB_SECRET_KEY" in captured.err
    assert "short" not in captured.err


def test_web_when_public_host_without_allow_flag_returns_2(monkeypatch, capsys) -> None:
    config = _make_config(monkeypatch, **_valid_env(WEB_HOST="0.0.0.0"))
    calls = _patch_uvicorn(monkeypatch)

    rc = cli.cmd_web(config)

    captured = capsys.readouterr()
    assert rc == 2
    assert "WEB_ALLOW_PUBLIC" in captured.err
    assert calls == []


def test_web_when_valid_env_runs_uvicorn_with_expected_options(monkeypatch) -> None:
    config = _make_config(
        monkeypatch,
        **_valid_env(
            WEB_HOST="127.0.0.1",
            WEB_PORT="8765",
            WEB_TRUSTED_PROXIES="10.0.0.1,10.0.0.2",
        ),
    )
    calls = _patch_uvicorn(monkeypatch)

    rc = cli.cmd_web(config)

    assert rc == 0
    assert len(calls) == 1
    app, kwargs = calls[0]
    assert app is not None
    assert kwargs["host"] == "127.0.0.1"
    assert kwargs["port"] == 8765
    assert kwargs["proxy_headers"] is True
    assert kwargs["forwarded_allow_ips"] == "10.0.0.1,10.0.0.2"
    assert kwargs["reload"] is False
    assert kwargs["log_level"] == "info"


def test_web_when_trusted_proxies_empty_forwards_loopback(monkeypatch) -> None:
    config = _make_config(monkeypatch, **_valid_env())
    calls = _patch_uvicorn(monkeypatch)

    assert cli.cmd_web(config) == 0

    assert calls[0][1]["forwarded_allow_ips"] == "127.0.0.1"


def test_web_when_public_allowed_reaches_uvicorn(monkeypatch) -> None:
    config = _make_config(
        monkeypatch,
        **_valid_env(WEB_HOST="0.0.0.0", WEB_ALLOW_PUBLIC="YES"),
    )
    calls = _patch_uvicorn(monkeypatch)

    rc = cli.cmd_web(config)

    assert rc == 0
    assert calls[0][1]["host"] == "0.0.0.0"


def test_web_via_main_needs_no_api_token(monkeypatch, capsys) -> None:
    monkeypatch.delenv("T_INVEST_TOKEN", raising=False)
    monkeypatch.setenv("MODE", "sandbox")
    monkeypatch.setattr(sys, "argv", ["cli.py", "web"])
    seen: list[Config] = []
    monkeypatch.setattr(cli, "cmd_web", lambda config: seen.append(config) or 0)

    rc = cli.main()

    assert rc == 0
    assert len(seen) == 1

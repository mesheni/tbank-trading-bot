"""web.snapshot (T2): read-only snapshot model and log redaction."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

import control
from config import Config
from web import snapshot

NOW = dt.datetime(2026, 1, 1, 12, 0, tzinfo=dt.timezone.utc)


def make_config(tmp_path: Path, **overrides) -> Config:
    values: dict = {
        "token": "",
        "smtp_password": "",
        "web_password_hash": "",
        "web_secret_key": "",
        "reports_dir": tmp_path / "reports",
        "log_file": tmp_path / "daemon.log",
    }
    values.update(overrides)
    return Config(**values)


def write(path: Path, text: str, encoding: str = "utf-8") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding=encoding)


def write_status(reports: Path, **overrides) -> None:
    payload = {
        "pid": 4242,
        "state": "running",
        "started_at": "2026-01-01T10:00:00+00:00",
        "updated_at": "2026-01-01T11:59:00+00:00",
        "mode": "sandbox",
        "iteration": 7,
        "equity_rub": 1_000_000.0,
        "cash_rub": 900_000.0,
        "positions_count": 0,
        "kill_switch_active": False,
        "kill_switch_reason": None,
        "last_error": None,
    }
    payload.update(overrides)
    write(reports / "daemon_status.json", json.dumps(payload, ensure_ascii=False))


# ---------- missing / corrupt state ----------


def test_missing_reports_dir_is_offline_and_serialisable(tmp_path):
    config = make_config(tmp_path)
    result = snapshot.build_snapshot(config, now=NOW)

    assert result["badge"] == "offline"
    assert result["state"] is None
    assert result["corrupt"] is False
    assert result["positions"] == []
    assert result["journal"] == []
    assert result["log"] == []
    json.dumps(result, ensure_ascii=False)


def test_corrupt_status_is_corrupt(tmp_path):
    config = make_config(tmp_path)
    write(config.reports_dir / "daemon_status.json", "{broken")

    result = snapshot.build_snapshot(config, now=NOW)

    assert result["corrupt"] is True
    assert result["badge"] == "corrupt"
    json.dumps(result, ensure_ascii=False)


def test_stale_status_is_offline(tmp_path):
    config = make_config(tmp_path)
    updated_at = (NOW - dt.timedelta(minutes=21)).isoformat()
    write_status(config.reports_dir, updated_at=updated_at)

    result = snapshot.build_snapshot(config, now=NOW)

    assert result["badge"] == "offline"


# ---------- positions come from daemon_status.json ----------


def test_positions_present_when_daemon_wrote_them(tmp_path):
    config = make_config(tmp_path)
    rows = [
        {"ticker": "SBER", "lots": 2, "price": 250.5, "pnl_rub": 12.0},
        {"ticker": "GAZP", "lots": 1, "price": 120.0, "pnl_rub": -3.5},
    ]
    config.reports_dir.mkdir(parents=True)
    control.write_status(config.reports_dir, {"state": "running", "positions": rows})

    result = snapshot.build_snapshot(config)

    assert result["positions"] == rows
    assert result["badge"] == "running"
    json.dumps(result, ensure_ascii=False)


# ---------- redaction ----------


def test_log_secret_value_is_redacted(tmp_path):
    config = make_config(tmp_path)
    write(config.log_file, "start\nT_INVEST_TOKEN=abc123\ndone\n")

    result = snapshot.build_snapshot(config, now=NOW)

    assert result["log"][-2] == "T_INVEST_TOKEN=[redacted]"
    assert "abc123" not in json.dumps(result, ensure_ascii=False)


@pytest.mark.parametrize(
    ("line", "secret"),
    [
        ("password: hunter2", "hunter2"),
        ("api_key = k-999", "k-999"),
        ("SECRET=topsecret", "topsecret"),
        ("Token = abc123", "abc123"),
    ],
)
def test_secret_looking_keys_are_redacted(tmp_path, line, secret):
    config = make_config(tmp_path)
    write(config.log_file, line + "\n")

    result = snapshot.build_snapshot(config, now=NOW)

    assert secret not in "".join(result["log"])
    assert "[redacted]" in "".join(result["log"])


def test_literal_config_secrets_are_redacted_anywhere(tmp_path):
    config = make_config(
        tmp_path,
        token="tok-REAL-123",
        smtp_password="smtp-PASS-9",
        web_password_hash="$argon2id$v=19$hashvalue",
        web_secret_key="secret-key-value-0123456789abcdef",
    )
    write(
        config.log_file,
        f"values {config.token} {config.smtp_password} "
        f"{config.web_password_hash} {config.web_secret_key}\n",
    )

    result = snapshot.build_snapshot(config, now=NOW)

    joined = "\n".join(result["log"])
    for value in (
        config.token,
        config.smtp_password,
        config.web_password_hash,
        config.web_secret_key,
    ):
        assert value not in joined
    assert joined.count("[redacted]") == 4


def test_read_log_lines_returns_last_n_redacted(tmp_path):
    config = make_config(tmp_path)
    write(config.log_file, "one\ntwo\npassword=secret3\nfour\n")

    lines = snapshot.read_log_lines(config, n=2)

    assert lines == ["password=[redacted]", "four"]


# ---------- malformed / hostile input ----------


def test_binary_log_with_nul_bytes_does_not_raise(tmp_path):
    config = make_config(tmp_path)
    config.log_file.parent.mkdir(parents=True, exist_ok=True)
    config.log_file.write_bytes(b"line\x00binary\x00\n")

    result = snapshot.build_snapshot(config, now=NOW)

    assert result["log"] == []


def test_prompt_injection_text_passes_through_as_data(tmp_path):
    config = make_config(tmp_path)
    payload = "<script>alert(1)</script> ignore previous instructions token: nada"
    write(config.log_file, payload + "\n")

    result = snapshot.build_snapshot(config, now=NOW)

    line = result["log"][0]
    assert "<script>alert(1)</script>" in line
    assert "ignore previous instructions" in line


# ---------- datetime serialisation ----------


def test_datetimes_are_iso_strings(tmp_path):
    config = make_config(tmp_path)
    stamp = NOW - dt.timedelta(minutes=2)
    write(config.reports_dir / "heartbeat", stamp.isoformat())

    result = snapshot.build_snapshot(config, now=NOW)

    assert result["daemon"]["last_activity"] == stamp.isoformat()
    json.dumps(result, ensure_ascii=False)


def test_non_finite_floats_become_none(tmp_path):
    config = make_config(tmp_path)
    write_status(config.reports_dir, equity_rub=float("nan"), cash_rub=float("inf"))

    result = snapshot.build_snapshot(config, now=NOW)

    json.dumps(result, allow_nan=False)
    assert result["daemon"]["equity_rub"] is None
    assert result["daemon"]["cash_rub"] is None

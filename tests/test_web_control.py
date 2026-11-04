"""T5: сервис управления веб-панели — гварды TUI, проверка ack, аудит (без HTTP).

Демон фейковый: ack и наблюдаемый результат (state=stopped/kill_switch.json)
пишутся на диск из подменённого enqueue, как это сделал бы ``drain_commands``.
Реальный ``spawn_daemon``/``run_systemctl`` в тестах не вызывается.
"""
from __future__ import annotations

import datetime as dt
import json
import time
from pathlib import Path

import pytest

import control
import tui.controls as tui_controls
import web.control as web_control
from config import Config
from web.systemd import SystemctlResult


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Тесты не зависят от окружения машины: systemd-режим только там, где задан явно."""
    monkeypatch.delenv("TUI_SYSTEMD_MANAGED", raising=False)
    monkeypatch.delenv("SYSTEMD_UNIT", raising=False)


def _config(tmp_path: Path) -> Config:
    config = Config()
    config.reports_dir = tmp_path
    config.log_file = None
    config.mode = "sandbox"
    config.confirm_real_trading = False
    return config


def _write_running(reports: Path, **overrides) -> None:
    payload = {"pid": 4242, "state": "running", "mode": "sandbox", "iteration": 1}
    payload.update(overrides)
    control.write_status(reports, payload)
    (reports / "heartbeat").write_text(
        dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), encoding="utf-8"
    )


def _command_files(reports: Path) -> list[Path]:
    return sorted(control.paths(reports)["commands"].glob("*.json"))


def _command_names(reports: Path) -> list[str]:
    return [json.loads(path.read_text(encoding="utf-8"))["name"] for path in _command_files(reports)]


def _audit_lines(reports: Path) -> list[dict]:
    path = reports / "web_audit.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _fake_ack_on_enqueue(
    monkeypatch,
    *,
    ok: bool = True,
    detail: str = "",
    stop_state: bool = False,
    clear_kill: bool = False,
) -> list[str]:
    """Фейковый демон: ack и наблюдаемый результат появляются сразу после enqueue."""
    real_enqueue = control.enqueue_command
    seen: list[str] = []

    def enqueue(reports_dir, name: str) -> str:
        command_id = real_enqueue(reports_dir, name)
        acks_dir = control.paths(reports_dir)["acks"]
        acks_dir.mkdir(parents=True, exist_ok=True)
        (acks_dir / f"{command_id}.json").write_text(
            json.dumps({"ok": ok, "detail": detail}), encoding="utf-8"
        )
        if stop_state:
            control.write_status(reports_dir, {"state": "stopped", "iteration": 2, "mode": "sandbox"})
        if clear_kill:
            (Path(reports_dir) / "kill_switch.json").unlink(missing_ok=True)
        seen.append(command_id)
        return command_id

    monkeypatch.setattr(web_control.control, "enqueue_command", enqueue)
    return seen


def _spawn_spy(monkeypatch) -> list[int]:
    calls: list[int] = []

    def spawn():
        calls.append(1)
        return object()

    monkeypatch.setattr(web_control.controls, "spawn_daemon", spawn)
    return calls


# =====================================================================
# Stop
# =====================================================================

def test_stop_with_ack_and_stopped_state_is_ok(tmp_path, monkeypatch):
    _write_running(tmp_path)
    acked = _fake_ack_on_enqueue(monkeypatch, stop_state=True)

    result = web_control.request_stop(_config(tmp_path), True)

    assert result.ok is True
    assert result.message == tui_controls.STOPPED
    assert result.command_id == acked[0]
    assert result.audit_result == "ok"
    assert _command_names(tmp_path) == ["stop"]
    # Клиент не дренирует очередь: удалить команду может только демон.
    assert _command_files(tmp_path)
    audit = _audit_lines(tmp_path)
    assert len(audit) == 1
    assert audit[0]["action"] == "stop"
    assert audit[0]["result"] == "ok"
    assert audit[0]["command_id"] == acked[0]


def test_stop_failed_ack_is_error_with_detail(tmp_path, monkeypatch):
    _write_running(tmp_path)
    _fake_ack_on_enqueue(monkeypatch, ok=False, detail="RuntimeError: stop denied")

    result = web_control.request_stop(_config(tmp_path), True)

    assert result.ok is False
    assert "stop denied" in result.message
    assert result.audit_result == "error"
    assert _audit_lines(tmp_path)[0]["result"] == "error"


def test_stop_without_daemon_is_refused_and_enqueues_nothing(tmp_path, monkeypatch):
    result = web_control.request_stop(_config(tmp_path), True)

    assert result.ok is False
    assert result.message == tui_controls.DAEMON_NOT_RUNNING
    assert result.command_id is None
    assert result.audit_result == "refused"
    assert _command_files(tmp_path) == []
    assert _audit_lines(tmp_path)[0]["result"] == "refused"


def test_stop_times_out_with_injected_deadline(tmp_path, monkeypatch):
    _write_running(tmp_path)

    started = time.monotonic()
    result = web_control.request_stop(_config(tmp_path), True, timeout=0.2)
    elapsed = time.monotonic() - started

    assert elapsed < 2.0  # никакого реального 60-секундного ожидания
    assert result.ok is False
    assert result.message == tui_controls.STOP_TIMEOUT
    assert result.audit_result == "timeout"
    assert _command_names(tmp_path) == ["stop"]  # команда всё же отправлена
    assert _audit_lines(tmp_path)[0]["result"] == "timeout"


def test_ack_ok_but_daemon_still_running_is_not_declared_stopped(tmp_path, monkeypatch):
    """Подделанный/поспешный ack ok=True не должен выдаваться за успех."""
    _write_running(tmp_path)
    _fake_ack_on_enqueue(monkeypatch)  # ok=True, но state остаётся running

    result = web_control.request_stop(_config(tmp_path), True, timeout=0.2)

    assert result.ok is False
    assert result.message == tui_controls.STOP_TIMEOUT
    assert result.audit_result == "timeout"


# =====================================================================
# Kill-switch reset
# =====================================================================

def test_kill_reset_offline_clears_file_directly_without_command(tmp_path, monkeypatch):
    (tmp_path / "kill_switch.json").write_text(
        json.dumps({"triggered_at": "2026-01-01T00:00:00+00:00"}), encoding="utf-8"
    )

    result = web_control.request_kill_switch_reset(_config(tmp_path), True)

    assert result.ok is True
    assert result.message == tui_controls.KILL_SWITCH_CLEARED
    assert result.command_id is None
    assert result.audit_result == "ok"
    assert not (tmp_path / "kill_switch.json").exists()
    assert _command_files(tmp_path) == []


def test_kill_reset_online_enqueues_exactly_one_reset_and_never_clears_file(tmp_path, monkeypatch):
    _write_running(tmp_path)
    (tmp_path / "kill_switch.json").write_text(
        json.dumps({"triggered_at": "2026-01-01T00:00:00+00:00"}), encoding="utf-8"
    )
    cleared: list[Path] = []
    monkeypatch.setattr(web_control.bot, "clear_kill_switch", lambda reports_dir: cleared.append(reports_dir))
    acked = _fake_ack_on_enqueue(monkeypatch, clear_kill=True)

    result = web_control.request_kill_switch_reset(_config(tmp_path), True)

    assert result.ok is True
    assert result.message == tui_controls.KILL_SWITCH_RESET_CONFIRMED
    assert result.command_id == acked[0]
    assert result.audit_result == "ok"
    assert _command_names(tmp_path) == ["kill_switch_reset"]
    assert len(_command_files(tmp_path)) == 1
    assert cleared == []  # живой демон сам снимает флаг; сервис файл не трогает


def test_corrupt_status_fails_closed_to_daemon_path(tmp_path, monkeypatch):
    (tmp_path / "daemon_status.json").write_text("{broken", encoding="utf-8")
    (tmp_path / "kill_switch.json").write_text(
        json.dumps({"triggered_at": "2026-01-01T00:00:00+00:00"}), encoding="utf-8"
    )
    cleared: list[Path] = []
    monkeypatch.setattr(web_control.bot, "clear_kill_switch", lambda reports_dir: cleared.append(reports_dir))
    _fake_ack_on_enqueue(monkeypatch, clear_kill=True)

    result = web_control.request_kill_switch_reset(_config(tmp_path), True)

    assert result.ok is True
    assert result.message == tui_controls.KILL_SWITCH_RESET_CONFIRMED
    assert _command_names(tmp_path) == ["kill_switch_reset"]
    assert cleared == []  # не прямой сброс: статус повреждён, демон может быть жив


def test_kill_reset_failure_ack_is_error_and_keeps_flag(tmp_path, monkeypatch):
    _write_running(tmp_path)
    (tmp_path / "kill_switch.json").write_text(
        json.dumps({"triggered_at": "2026-01-01T00:00:00+00:00"}), encoding="utf-8"
    )
    _fake_ack_on_enqueue(monkeypatch, ok=False, detail="RuntimeError: reset denied")

    result = web_control.request_kill_switch_reset(_config(tmp_path), True)

    assert result.ok is False
    assert "reset denied" in result.message
    assert result.audit_result == "error"
    assert (tmp_path / "kill_switch.json").exists()


# =====================================================================
# Start
# =====================================================================

def test_start_in_real_without_confirm_is_refused_and_spawns_nothing(tmp_path, monkeypatch):
    spawned = _spawn_spy(monkeypatch)
    config = _config(tmp_path)
    config.mode = "real"
    config.confirm_real_trading = False

    result = web_control.request_start(config, True)

    assert result.ok is False
    assert result.message == tui_controls.REAL_TRADING_REFUSAL
    assert result.audit_result == "refused"
    assert spawned == []
    assert _audit_lines(tmp_path)[0]["result"] == "refused"


def test_start_sandbox_spawns_daemon_once(tmp_path, monkeypatch):
    spawned = _spawn_spy(monkeypatch)

    result = web_control.request_start(_config(tmp_path), True)

    assert result.ok is True
    assert result.message == tui_controls.STARTING
    assert result.audit_result == "ok"
    assert spawned == [1]


def test_start_systemd_managed_uses_adapter(tmp_path, monkeypatch):
    monkeypatch.setenv("TUI_SYSTEMD_MANAGED", "1")
    spawned = _spawn_spy(monkeypatch)
    calls: list[tuple[str]] = []

    def run(action: str) -> SystemctlResult:
        calls.append((action,))
        return SystemctlResult(ok=True, returncode=0, detail="")

    monkeypatch.setattr(web_control.systemd, "run_systemctl", run)

    result = web_control.request_start(_config(tmp_path), True)

    assert result.ok is True
    assert result.message == tui_controls.SYSTEMD_START
    assert result.audit_result == "ok"
    assert calls == [("start",)]
    assert spawned == []


def test_start_systemd_failure_is_error_not_fake_success(tmp_path, monkeypatch):
    monkeypatch.setenv("TUI_SYSTEMD_MANAGED", "1")
    spawned = _spawn_spy(monkeypatch)
    monkeypatch.setattr(
        web_control.systemd,
        "run_systemctl",
        lambda action: SystemctlResult(ok=False, returncode=1, detail="Failed to start unit"),
    )

    result = web_control.request_start(_config(tmp_path), True)

    assert result.ok is False
    assert "Failed to start unit" in result.message
    assert result.message != tui_controls.SYSTEMD_START
    assert result.audit_result == "error"
    assert spawned == []


# =====================================================================
# reauth, аудит, устойчивость аудита
# =====================================================================

@pytest.mark.parametrize(
    "call",
    [
        lambda config: web_control.request_stop(config, False),
        lambda config: web_control.request_kill_switch_reset(config, False),
        lambda config: web_control.request_start(config, False),
    ],
)
def test_reauth_false_raises_permission_error(tmp_path, call):
    with pytest.raises(PermissionError):
        call(_config(tmp_path))


def test_audit_one_line_per_call_and_never_contains_secrets(tmp_path, monkeypatch):
    config = _config(tmp_path)
    config.mode = "real"
    config.confirm_real_trading = False
    config.token = "TINVEST_SECRET_TOKEN_abc123"
    config.web_password_hash = "$argon2id$v=19$m=65536,t=3,p=4$SECRETHASHVALUE"
    config.web_secret_key = "SECRETKEY" * 8
    _spawn_spy(monkeypatch)

    stopped = web_control.request_stop(config, True, user="admin", client_ip="203.0.113.7")
    cleared = web_control.request_kill_switch_reset(config, True)
    started = web_control.request_start(config, True)

    assert [stopped.audit_result, cleared.audit_result, started.audit_result] == [
        "refused",
        "ok",
        "refused",
    ]
    audit = _audit_lines(tmp_path)
    assert len(audit) == 3  # ровно одна строка на вызов
    assert [entry["action"] for entry in audit] == ["stop", "kill_reset", "start"]
    assert [entry["result"] for entry in audit] == ["refused", "ok", "refused"]
    assert audit[0]["user"] == "admin"
    assert audit[0]["client_ip"] == "203.0.113.7"
    assert audit[1]["user"] == "" and audit[1]["client_ip"] == ""
    assert all(set(entry) == {"time", "action", "user", "client_ip", "result", "command_id"} for entry in audit)

    raw = (tmp_path / "web_audit.jsonl").read_text(encoding="utf-8")
    for secret in (
        "TINVEST_SECRET_TOKEN_abc123",
        "SECRETHASHVALUE",
        "SECRETKEYSECRETKEY",
        "password",
        "token",
    ):
        assert secret not in raw


def test_audit_write_failure_does_not_raise_or_change_result(tmp_path, monkeypatch):
    # web_audit.jsonl существует как каталог — запись строки физически невозможна.
    (tmp_path / "web_audit.jsonl").mkdir()

    result = web_control.request_stop(_config(tmp_path), True)

    assert result.ok is False
    assert result.message == tui_controls.DAEMON_NOT_RUNNING
    assert result.audit_result == "refused"


# =====================================================================
# Adversarial: устаревшая живость -> офлайн по badge
# =====================================================================

def test_stale_heartbeat_without_status_is_offline_and_stop_refused(tmp_path):
    old = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=25)
    (tmp_path / "heartbeat").write_text(old.isoformat(timespec="seconds"), encoding="utf-8")

    result = web_control.request_stop(_config(tmp_path), True)

    assert result.ok is False
    assert result.message == tui_controls.DAEMON_NOT_RUNNING
    assert result.audit_result == "refused"
    assert _command_files(tmp_path) == []

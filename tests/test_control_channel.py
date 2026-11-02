"""control.py: снимок состояния демона, очередь команд и PID-lock.

Файловый контракт single-writer: daemon_status.json пишет только демон,
команды кладёт клиент, подтверждения (acks) — демон. Здесь проверяется
именно содержимое файлов на диске, а не только возвращаемые значения.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
from pathlib import Path

import pytest

import control

REQUIRED_STATUS_KEYS = {
    "pid",
    "state",
    "started_at",
    "updated_at",
    "mode",
    "iteration",
    "equity_rub",
    "cash_rub",
    "positions_count",
    "kill_switch_active",
    "kill_switch_reason",
    "last_error",
}


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _read_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def _command_files(reports_dir: Path) -> list[Path]:
    return sorted((reports_dir / "commands").glob("*.json"))


def test_paths_point_into_reports_dir_without_creating_it(tmp_path: Path) -> None:
    root = tmp_path / "reports"
    root.mkdir()

    result = control.paths(root)

    assert result["pid"] == root / "daemon.pid"
    assert result["lock"] == root / "daemon.lock"
    assert result["status"] == root / "daemon_status.json"
    assert result["commands"] == root / "commands"
    assert result["acks"] == root / "acks"
    assert not (root / "commands").exists()
    assert not (root / "acks").exists()


def test_write_status_round_trip_when_payload_complete(tmp_path: Path) -> None:
    payload = {
        "pid": 4242,
        "state": "running",
        "started_at": "2026-11-02T10:00:00+00:00",
        "updated_at": "2026-11-02T10:00:00+00:00",
        "mode": "sandbox",
        "iteration": 7,
        "equity_rub": 100000.5,
        "cash_rub": 12000.0,
        "positions_count": 3,
        "kill_switch_active": False,
        "kill_switch_reason": None,
        "last_error": None,
    }

    control.write_status(tmp_path, payload)

    on_disk = _read_json(tmp_path / "daemon_status.json")
    assert isinstance(on_disk, dict)
    assert REQUIRED_STATUS_KEYS <= set(on_disk)
    assert on_disk["pid"] == 4242
    assert on_disk["state"] == "running"
    assert on_disk["started_at"] == "2026-11-02T10:00:00+00:00"
    assert on_disk["mode"] == "sandbox"
    assert on_disk["iteration"] == 7
    assert on_disk["equity_rub"] == 100000.5
    assert on_disk["cash_rub"] == 12000.0
    assert on_disk["positions_count"] == 3
    assert on_disk["kill_switch_active"] is False
    assert on_disk["kill_switch_reason"] is None
    assert on_disk["last_error"] is None
    # updated_at — время записи снимка, а не значение из payload
    assert dt.datetime.fromisoformat(on_disk["updated_at"]).tzinfo is not None
    assert control.read_status(tmp_path) == on_disk
    assert list(tmp_path.glob("*.tmp")) == []


def test_write_status_fills_required_keys_when_payload_minimal(tmp_path: Path) -> None:
    control.write_status(tmp_path, {"state": "stopping", "iteration": 2})

    on_disk = _read_json(tmp_path / "daemon_status.json")
    assert isinstance(on_disk, dict)
    assert REQUIRED_STATUS_KEYS <= set(on_disk)
    assert on_disk["pid"] == os.getpid()
    assert on_disk["state"] == "stopping"
    assert on_disk["iteration"] == 2
    assert on_disk["kill_switch_active"] is False
    assert dt.datetime.fromisoformat(on_disk["started_at"]).tzinfo is not None


def test_write_status_rejects_unknown_state_without_file(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        control.write_status(tmp_path, {"state": "exploded"})

    assert not (tmp_path / "daemon_status.json").exists()


def test_read_status_returns_none_when_file_missing(tmp_path: Path) -> None:
    assert control.read_status(tmp_path) is None


@pytest.mark.parametrize("raw", ["{broken", "", "[]", '"text"', "null"])
def test_read_status_returns_corrupt_marker_when_json_invalid(tmp_path: Path, raw: str) -> None:
    (tmp_path / "daemon_status.json").write_text(raw, encoding="utf-8")

    assert control.read_status(tmp_path) == {"corrupt": True}


def test_enqueue_command_writes_exactly_one_file(tmp_path: Path) -> None:
    command_id = control.enqueue_command(tmp_path, "stop")

    files = _command_files(tmp_path)
    assert len(files) == 1
    assert files[0].stem == command_id
    assert command_id.startswith(tuple("0123456789"))

    raw = _read_json(files[0])
    assert isinstance(raw, dict)
    assert set(raw) == {"name", "requested_at"}
    assert raw["name"] == "stop"
    assert dt.datetime.fromisoformat(raw["requested_at"]).tzinfo is not None
    assert list((tmp_path / "commands").glob("*.tmp")) == []


@pytest.mark.parametrize("name", ["reboot", "", "STOP", "stop ", "kill"])
def test_enqueue_unknown_name_raises_and_creates_nothing(tmp_path: Path, name: str) -> None:
    with pytest.raises(ValueError):
        control.enqueue_command(tmp_path, name)

    assert list(tmp_path.iterdir()) == []


def test_drain_commands_sorted_writes_acks_and_deletes_commands(tmp_path: Path) -> None:
    first = "20260101T000001000000Z-aaaa"
    second = "20260101T000002000000Z-bbbb"
    commands = tmp_path / "commands"
    # файлы созданы в обратном порядке — drain обязан идти по имени
    _write_json(commands / f"{second}.json", {"name": "kill_switch_reset", "requested_at": "x"})
    _write_json(commands / f"{first}.json", {"name": "stop", "requested_at": "y"})

    drained = control.drain_commands(tmp_path)

    assert [item["id"] for item in drained] == [first, second]
    assert [item["name"] for item in drained] == ["stop", "kill_switch_reset"]
    assert _command_files(tmp_path) == []

    expected_names = {first: "stop", second: "kill_switch_reset"}
    for command_id, name in expected_names.items():
        ack = _read_json(tmp_path / "acks" / f"{command_id}.json")
        assert isinstance(ack, dict)
        assert set(ack) == {"name", "handled_at", "ok", "detail"}
        assert ack["name"] == name
        assert ack["ok"] is True
        assert ack["detail"] == ""
        assert dt.datetime.fromisoformat(ack["handled_at"]).tzinfo is not None


def test_drain_commands_missing_dir_returns_empty(tmp_path: Path) -> None:
    assert control.drain_commands(tmp_path) == []
    assert (tmp_path / "commands").is_dir()
    assert (tmp_path / "acks").is_dir()


def test_drain_commands_skips_junk_with_warning(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    commands = tmp_path / "commands"
    commands.mkdir(parents=True)
    (commands / "notes.txt").write_text("junk", encoding="utf-8")
    (commands / "broken.json").write_text("{broken", encoding="utf-8")
    _write_json(commands / "noname.json", {"requested_at": "x"})
    valid = "20260101T000003000000Z-cccc"
    _write_json(commands / f"{valid}.json", {"name": "stop", "requested_at": "z"})

    with caplog.at_level(logging.WARNING, logger="control"):
        drained = control.drain_commands(tmp_path)

    assert [item["id"] for item in drained] == [valid]
    # валидная команда обработана, мусор остался лежать (не теряем улики)
    assert not (commands / f"{valid}.json").exists()
    assert (commands / "notes.txt").exists()
    assert (commands / "broken.json").exists()
    assert (commands / "noname.json").exists()
    assert (tmp_path / "acks" / f"{valid}.json").exists()
    assert any("Пропущена" in record.getMessage() for record in caplog.records)


def test_acquire_lock_second_attempt_same_live_pid_returns_false(tmp_path: Path) -> None:
    assert control.acquire_lock(tmp_path) is True
    try:
        assert (tmp_path / "daemon.lock").read_text(encoding="utf-8").strip() == str(os.getpid())
        assert (tmp_path / "daemon.pid").read_text(encoding="utf-8").strip() == str(os.getpid())
        assert control.acquire_lock(tmp_path) is False
    finally:
        assert control.release_lock(tmp_path) is True

    assert not (tmp_path / "daemon.lock").exists()
    assert not (tmp_path / "daemon.pid").exists()
    assert control.acquire_lock(tmp_path) is True
    control.release_lock(tmp_path)


def test_acquire_lock_takes_over_stale_pid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "daemon.lock").write_text("999999\n", encoding="utf-8")
    monkeypatch.setattr(control, "_pid_alive", lambda pid: pid == os.getpid())

    assert control.acquire_lock(tmp_path) is True

    assert (tmp_path / "daemon.lock").read_text(encoding="utf-8").strip() == str(os.getpid())
    assert (tmp_path / "daemon.pid").read_text(encoding="utf-8").strip() == str(os.getpid())
    # после перехвата наш живой pid держит lock
    assert control.acquire_lock(tmp_path) is False
    control.release_lock(tmp_path)


def test_acquire_lock_takes_over_corrupt_lock(tmp_path: Path) -> None:
    (tmp_path / "daemon.lock").write_text("not-a-pid", encoding="utf-8")

    assert control.acquire_lock(tmp_path) is True
    assert (tmp_path / "daemon.lock").read_text(encoding="utf-8").strip() == str(os.getpid())
    control.release_lock(tmp_path)


def test_release_lock_keeps_foreign_lock(tmp_path: Path) -> None:
    (tmp_path / "daemon.lock").write_text("999999\n", encoding="utf-8")

    assert control.release_lock(tmp_path) is False
    assert (tmp_path / "daemon.lock").exists()


def test_pid_alive_current_process_is_true() -> None:
    assert control._pid_alive(os.getpid()) is True


@pytest.mark.parametrize("pid", [0, -1])
def test_pid_alive_invalid_pid_is_false(pid: int) -> None:
    assert control._pid_alive(pid) is False

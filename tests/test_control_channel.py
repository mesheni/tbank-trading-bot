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
import threading
import time
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


def test_daemon_pid_alive_distinguishes_live_dead_and_missing() -> None:
    assert control.daemon_pid_alive({"pid": os.getpid()}) is True
    assert control.daemon_pid_alive({"pid": 999999}) is False  # pid, которого нет на хосте
    assert control.daemon_pid_alive({"corrupt": True}) is False
    assert control.daemon_pid_alive(None) is False


# =====================================================================
# Гонки захвата lock: пустой lock и одновременный перехват устаревшего
# =====================================================================

def test_acquire_lock_waits_for_pid_in_freshly_created_lock(tmp_path: Path) -> None:
    """lock только что создан другим процессом и ещё пуст: перехватывать чужой живой lock нельзя."""
    lock = tmp_path / "daemon.lock"
    lock.write_text("", encoding="utf-8")
    started = time.monotonic()
    writer = threading.Timer(0.15, lambda: lock.write_text(f"{os.getpid()}\n", encoding="utf-8"))
    writer.start()
    try:
        assert control.acquire_lock(tmp_path) is False
        elapsed = time.monotonic() - started
    finally:
        writer.cancel()

    assert elapsed >= 0.15  # дождался pid живого владельца, а не счёл файл повреждённым
    assert lock.read_text(encoding="utf-8").strip() == str(os.getpid())
    assert not (tmp_path / "daemon.pid").exists()


def test_acquire_lock_takes_over_abandoned_empty_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Владелец умер между созданием lock и записью pid: пустой lock в итоге перехватывается."""
    monkeypatch.setattr(control, "_LOCK_PID_WAIT_SEC", 0.05)
    (tmp_path / "daemon.lock").write_text("", encoding="utf-8")

    assert control.acquire_lock(tmp_path) is True

    assert (tmp_path / "daemon.lock").read_text(encoding="utf-8").strip() == str(os.getpid())
    control.release_lock(tmp_path)


def test_concurrent_takeover_is_serialized_and_sees_new_owner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Два старта поверх мёртвого pid: второй ждёт блокировки перехвата и отступает."""
    lock = tmp_path / "daemon.lock"
    lock.write_text("999999\n", encoding="utf-8")
    monkeypatch.setattr(control, "_pid_alive", lambda pid: pid == os.getpid())
    monkeypatch.setattr(control, "_TAKEOVER_WAIT_SEC", 5.0)
    results: list[bool] = []
    with control._takeover_guard(lock) as guarded:  # «первый» процесс держит блокировку перехвата
        assert guarded is True
        contender = threading.Thread(target=lambda: results.append(control.acquire_lock(tmp_path)))
        contender.start()
        contender.join(0.2)
        assert contender.is_alive()  # не лезет поверх: ждёт блокировки
        lock.write_text(f"{os.getpid()}\n", encoding="utf-8")  # «первый» перехватил lock
    contender.join(5.0)  # блокировка снята вместе с дескриптором

    assert results == [False]  # взяв её, второй увидел живого владельца и отступил


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("0::/system.slice/tbank-bot.service\n", "tbank-bot.service"),
        ("0::/user.slice/user-1000.slice/user@1000.service/app.slice/tbank-bot.service\n", "tbank-bot.service"),
        ("12:pids:/system.slice/tbank-bot.service\n11:memory:/system.slice/tbank-bot.service\n", "tbank-bot.service"),
        ("0::/user.slice/user-1000.slice/session-3.scope\n", None),
        ("", None),
    ],
)
def test_parse_systemd_unit_reads_cgroup_v1_v2_and_ignores_non_units(text: str, expected: str | None) -> None:
    assert control.parse_systemd_unit(text) == expected


def test_systemd_unit_of_pid_is_none_for_missing_process() -> None:
    assert control.systemd_unit_of_pid(999_999_999) is None


# =====================================================================
# write_text_atomic: replace на Windows конфликтует с читателем файла
# =====================================================================

def test_write_text_atomic_retries_replace_while_reader_holds_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """PermissionError от replace временен (файл читает TUI/веб): повторяем, а не теряем запись."""
    calls: list[int] = []
    real_replace = os.replace

    def flaky_replace(src, dst) -> None:
        calls.append(1)
        if len(calls) < 3:
            raise PermissionError(5, "Отказано в доступе", str(dst))
        real_replace(src, dst)

    monkeypatch.setattr(control.os, "replace", flaky_replace)
    monkeypatch.setattr(control.time, "sleep", lambda seconds: None)

    control.write_text_atomic(tmp_path / "state.json", "{}")

    assert len(calls) == 3
    assert (tmp_path / "state.json").read_text(encoding="utf-8") == "{}"


def test_write_text_atomic_gives_up_after_attempts_and_cleans_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def busy(src, dst) -> None:
        raise PermissionError(5, "Отказано в доступе", str(dst))

    monkeypatch.setattr(control.os, "replace", busy)
    monkeypatch.setattr(control.time, "sleep", lambda seconds: None)

    with pytest.raises(PermissionError):
        control.write_text_atomic(tmp_path / "state.json", "{}")

    assert list(tmp_path.iterdir()) == []  # временный файл не остался мусором

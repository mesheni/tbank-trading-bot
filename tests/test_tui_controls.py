"""D4: TUI-управление демоном — подтверждения, команды, ack, REAL-гейт, systemd.

Сценарии — sync-тесты с Textual Pilot через asyncio.run (pytest-asyncio нет).
Демон фейковый: ack пишется в ``reports/acks`` из подменённой обёртки, команды и
ack проверяются как файлы на диске, а не только по тексту статуса.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from textual.widgets import Button, Static

import control
import tui.app as tui_app
import tui.controls as tui_controls
from config import Config
from tui.app import TbankApp
from tui.controls import ConfirmScreen

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Тесты не зависят от окружения машины: systemd-режим только там, где задан явно."""
    monkeypatch.delenv("TUI_SYSTEMD_MANAGED", raising=False)


def _config(tmp_path: Path) -> Config:
    config = Config()
    config.reports_dir = tmp_path
    config.log_file = None
    config.tui_refresh_sec = 0.05
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


def _status(app: TbankApp) -> str:
    return str(app.query_one("#status", Static).render())


def _command_files(reports: Path) -> list[Path]:
    return sorted(control.paths(reports)["commands"].glob("*.json"))


def _ack_files(reports: Path) -> list[Path]:
    return sorted(control.paths(reports)["acks"].glob("*.json"))


def _command_names(reports: Path) -> list[str]:
    return [json.loads(path.read_text(encoding="utf-8"))["name"] for path in _command_files(reports)]


def _fake_ack(monkeypatch, *, stop_state: bool = False, ok: bool = True, detail: str = ""):
    """Фейковый демон: ack пишется сразу после enqueue (файл на диске, не UI-текст).

    ``stop_state`` доводит статус до ``stopped`` — TUI проверяет наблюдаемый
    результат, а не только наличие ack-файла.
    """
    seen: list[str] = []
    payload = {"ok": ok, "detail": detail}

    def ack(reports_dir, command_id: str):
        path = control.paths(reports_dir)["acks"] / f"{command_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")
        if stop_state:
            control.write_status(reports_dir, {"state": "stopped", "iteration": 2, "mode": "sandbox"})
        seen.append(command_id)
        return dict(payload)

    monkeypatch.setattr(tui_app, "_ack_received", ack)
    return seen


class _PopenSpy:
    """Перехватывает subprocess.Popen: реального процесса в тестах не появляется."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        return SimpleNamespace(pid=999_999)


def _install_popen_spy(monkeypatch, tmp_path: Path) -> _PopenSpy:
    spy = _PopenSpy()
    monkeypatch.setattr(
        tui_controls,
        "subprocess",
        SimpleNamespace(Popen=spy, DEVNULL=subprocess.DEVNULL, STDOUT=subprocess.STDOUT),
    )
    monkeypatch.setattr(tui_controls, "_daemon_out_path", lambda: tmp_path / "daemon.out")
    return spy


async def _confirm(app: TbankApp, pilot) -> None:
    await pilot.pause()
    assert isinstance(app.screen, ConfirmScreen)
    app.screen.query_one("#confirm", Button).press()
    await pilot.pause(0.3)


async def _cancel(app: TbankApp, pilot) -> None:
    await pilot.pause()
    assert isinstance(app.screen, ConfirmScreen)
    app.screen.query_one("#cancel", Button).press()
    await pilot.pause(0.1)


# =====================================================================
# (a) Stop: команда stop + ack + «демон остановлен»
# =====================================================================

def test_stop_key_enqueues_stop_and_shows_stopped(tmp_path, monkeypatch):
    _write_running(tmp_path)
    acked = _fake_ack(monkeypatch, stop_state=True)
    app = TbankApp(_config(tmp_path))

    async def scenario() -> None:
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.press("x")
            await _confirm(app, pilot)
            assert "демон остановлен" in _status(app)

    asyncio.run(scenario())

    assert _command_names(tmp_path) == ["stop"]  # файл команды на диске
    assert len(_ack_files(tmp_path)) == 1  # ack прочитан
    assert acked == [_ack_files(tmp_path)[0].stem]
    # TUI не дренирует очередь: удалить команду может только демон.
    assert _command_files(tmp_path)


# =====================================================================
# (b) Kill-switch: офлайн — напрямую, онлайн — командой
# =====================================================================

def test_kill_switch_offline_calls_clear_directly_without_command(tmp_path, monkeypatch):
    cleared: list[Path] = []
    monkeypatch.setattr(tui_app.bot, "clear_kill_switch", lambda reports_dir: cleared.append(reports_dir))
    app = TbankApp(_config(tmp_path))

    async def scenario() -> None:
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.press("k")
            await _confirm(app, pilot)
            assert "kill-switch сброшен" in _status(app)

    asyncio.run(scenario())

    assert cleared == [tmp_path]
    assert _command_files(tmp_path) == []  # никакой команды офлайн-демону


def test_kill_switch_online_enqueues_reset_and_never_clears_file(tmp_path, monkeypatch):
    _write_running(tmp_path)
    cleared: list[Path] = []
    monkeypatch.setattr(tui_app.bot, "clear_kill_switch", lambda reports_dir: cleared.append(reports_dir))
    _fake_ack(monkeypatch)
    app = TbankApp(_config(tmp_path))

    async def scenario() -> None:
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.press("k")
            await _confirm(app, pilot)
            assert "подтверждено демоном" in _status(app)

    asyncio.run(scenario())

    assert _command_names(tmp_path) == ["kill_switch_reset"]
    assert len(_ack_files(tmp_path)) == 1
    assert cleared == []  # живой демон сам снимает флаг; TUI файл не трогает


def test_kill_switch_reset_failure_ack_shows_error_not_confirmed(tmp_path, monkeypatch):
    """F1/Pilot: ack ok=False -> ошибка, а не «сброшен»; файл флага на месте."""
    _write_running(tmp_path)
    (tmp_path / "kill_switch.json").write_text(
        json.dumps({"triggered_at": "2026-01-01T00:00:00+00:00"}), encoding="utf-8"
    )
    _fake_ack(monkeypatch, ok=False, detail="RuntimeError: reset denied")
    app = TbankApp(_config(tmp_path))

    async def scenario() -> None:
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.press("k")
            await _confirm(app, pilot)
            await pilot.pause(0.2)
            text = _status(app)
            assert "ошибка выполнения" in text
            assert "reset denied" in text
            assert "сброшен" not in text

    asyncio.run(scenario())
    assert (tmp_path / "kill_switch.json").exists()


def test_stop_ack_with_running_state_is_not_declared_stopped(tmp_path, monkeypatch):
    """F1/Pilot: ack есть, но демон всё ещё running -> «остановлен» не показываем."""
    _write_running(tmp_path)
    _fake_ack(monkeypatch)  # ok=True, но статус остаётся running
    app = TbankApp(_config(tmp_path))
    app.ack_timeout = 0.2
    app.ack_poll_interval = 0.02

    async def scenario() -> None:
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.press("x")
            await _confirm(app, pilot)
            await pilot.pause(0.35)
            text = _status(app)
            assert "демон остановлен" not in text
            assert "не подтвердил остановку" in text

    asyncio.run(scenario())


# =====================================================================
# (c) Start: REAL-гейт и реальный запуск (Popen подменён)
# =====================================================================

def test_start_in_real_without_confirm_is_refused_and_spawns_nothing(tmp_path, monkeypatch):
    spy = _install_popen_spy(monkeypatch, tmp_path)
    config = _config(tmp_path)
    config.mode = "real"
    config.confirm_real_trading = False
    app = TbankApp(config)

    async def scenario() -> None:
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.press("s")
            await pilot.pause()
            assert not isinstance(app.screen, ConfirmScreen)  # отказано до модала
            assert "CONFIRM_REAL_TRADING=YES" in _status(app)

    asyncio.run(scenario())

    assert spy.calls == []


def test_start_sandbox_spawns_detached_daemon_once(tmp_path, monkeypatch):
    spy = _install_popen_spy(monkeypatch, tmp_path)
    config = _config(tmp_path)
    config.confirm_real_trading = True
    app = TbankApp(config)

    async def scenario() -> None:
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.press("s")
            await _confirm(app, pilot)
            assert "демон запускается" in _status(app)

    asyncio.run(scenario())

    assert len(spy.calls) == 1
    argv, kwargs = spy.calls[0]
    assert argv[1:] == [str(ROOT / "cli.py"), "daemon", "run"]
    assert kwargs["shell"] is False
    assert kwargs["stderr"] == subprocess.STDOUT
    assert kwargs["cwd"] == str(ROOT)
    if os.name == "nt":
        assert kwargs["creationflags"] == 0x00000008 | 0x00000200
    else:
        assert kwargs["start_new_session"] is True
    assert (tmp_path / "daemon.out").exists()  # stdout/stderr перенаправлены в файл


def test_start_while_running_is_refused_even_after_double_press(tmp_path, monkeypatch):
    _write_running(tmp_path)
    spy = _install_popen_spy(monkeypatch, tmp_path)
    app = TbankApp(_config(tmp_path))

    async def scenario() -> None:
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.press("s")
            await pilot.press("s")
            await pilot.pause()
            assert "уже запущен" in _status(app)

    asyncio.run(scenario())

    assert spy.calls == []


# =====================================================================
# (d) Отмена в каждом модале: ничего не отправлено
# =====================================================================

def test_cancel_in_every_modal_sends_nothing(tmp_path, monkeypatch):
    spy = _install_popen_spy(monkeypatch, tmp_path)
    cleared: list[Path] = []
    monkeypatch.setattr(tui_app.bot, "clear_kill_switch", lambda reports_dir: cleared.append(reports_dir))
    app = TbankApp(_config(tmp_path))

    async def scenario() -> None:
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.press("s")
            await pilot.pause()
            # Фокус по умолчанию — «Отмена», а не разрушительная кнопка.
            assert app.screen.focused is app.screen.query_one("#cancel", Button)
            await _cancel(app, pilot)
            assert "отменено" in _status(app)

            _write_running(tmp_path)
            await pilot.press("x")
            await _cancel(app, pilot)

            await pilot.press("k")
            await _cancel(app, pilot)

    asyncio.run(scenario())

    assert spy.calls == []
    assert cleared == []
    assert _command_files(tmp_path) == []


def test_enter_uses_default_cancel_focus(tmp_path, monkeypatch):
    spy = _install_popen_spy(monkeypatch, tmp_path)
    app = TbankApp(_config(tmp_path))

    async def scenario() -> None:
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.press("s")
            await pilot.pause()
            await pilot.press("enter")  # Enter на кнопке в фокусе = «Отмена»
            await pilot.pause(0.1)
            assert not isinstance(app.screen, ConfirmScreen)
            assert "отменено" in _status(app)

    asyncio.run(scenario())

    assert spy.calls == []


# =====================================================================
# (e) systemd-режим: s/x — подсказка, k остаётся доступен
# =====================================================================

def test_systemd_managed_start_stop_show_hint_and_kill_switch_still_works(tmp_path, monkeypatch):
    monkeypatch.setenv("TUI_SYSTEMD_MANAGED", "1")
    spy = _install_popen_spy(monkeypatch, tmp_path)
    cleared: list[Path] = []
    monkeypatch.setattr(tui_app.bot, "clear_kill_switch", lambda reports_dir: cleared.append(reports_dir))
    app = TbankApp(_config(tmp_path))

    async def scenario() -> None:
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.press("s")
            await pilot.pause()
            assert not isinstance(app.screen, ConfirmScreen)
            assert "systemctl start tbank-bot" in _status(app)

            await pilot.press("x")
            await pilot.pause()
            assert not isinstance(app.screen, ConfirmScreen)
            assert "systemctl stop tbank-bot" in _status(app)

            await pilot.press("k")  # kill-switch не управляется systemd
            await _confirm(app, pilot)
            assert "kill-switch сброшен" in _status(app)

    asyncio.run(scenario())

    assert spy.calls == []
    assert cleared == [tmp_path]
    assert _command_files(tmp_path) == []


# =====================================================================
# Adversarial: зависший демон, битый ack — дедлайн вместо зависания
# =====================================================================

def test_stop_times_out_with_injected_deadline(tmp_path, monkeypatch):
    _write_running(tmp_path)
    monkeypatch.setattr(tui_app, "_ack_received", lambda reports_dir, command_id: False)
    app = TbankApp(_config(tmp_path))
    app.ack_timeout = 0.2
    app.ack_poll_interval = 0.02

    async def scenario() -> float:
        started = time.monotonic()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.press("x")
            await _confirm(app, pilot)
            await pilot.pause(0.4)
            assert "не подтвердил остановку" in _status(app)
        return time.monotonic() - started

    elapsed = asyncio.run(scenario())

    assert elapsed < 2.0  # никакого реального 60-секундного ожидания
    assert _command_names(tmp_path) == ["stop"]  # команда всё же отправлена


def test_corrupt_ack_is_treated_as_no_ack(tmp_path, monkeypatch):
    _write_running(tmp_path)
    real_enqueue = control.enqueue_command

    def enqueue_and_corrupt(reports_dir, name: str) -> str:
        command_id = real_enqueue(reports_dir, name)
        ack = control.paths(reports_dir)["acks"] / f"{command_id}.json"
        ack.parent.mkdir(parents=True, exist_ok=True)
        ack.write_text("{broken", encoding="utf-8")
        return command_id

    monkeypatch.setattr(tui_app.control, "enqueue_command", enqueue_and_corrupt)
    app = TbankApp(_config(tmp_path))
    app.ack_timeout = 0.15
    app.ack_poll_interval = 0.02

    async def scenario() -> None:
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.press("x")
            await _confirm(app, pilot)
            await pilot.pause(0.4)
            assert "не подтвердил остановку" in _status(app)

    asyncio.run(scenario())

    acks = _ack_files(tmp_path)
    assert len(acks) == 1 and acks[0].read_text(encoding="utf-8") == "{broken"

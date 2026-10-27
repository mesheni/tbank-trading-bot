"""Kill-switch переживает рестарт: файл состояния, fail-closed чтение, сброс (аудит F-C1.2)."""
from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from bot import (
    TradingBot,
    _write_kill_switch,
    clear_kill_switch,
    kill_switch_path,
    read_kill_switch,
)
from notify import Notifier, SmtpConfig

PAYLOAD = {
    "triggered_at": "2026-10-24T10:00:00+00:00",
    "equity": 840_000.0,
    "budget": 1_000_000.0,
    "threshold": 0.15,
}


def make_state_bot(reports_dir) -> TradingBot:
    """Бот без сети/БД: для проверки восстановления состояния достаточно config."""
    bot = TradingBot.__new__(TradingBot)
    bot.config = SimpleNamespace(reports_dir=reports_dir)
    bot._entries_allowed = True
    return bot


def make_trigger_bot(reports_dir=None) -> TradingBot:
    bot = TradingBot.__new__(TradingBot)
    bot.config = SimpleNamespace(
        sandbox_initial_rub=1_000_000.0,
        max_drawdown_pct=0.15,
        **({"reports_dir": reports_dir} if reports_dir is not None else {}),
    )
    bot._budget_rub = None
    bot.notifier = Notifier(SmtpConfig())  # выключен: send() сразу False, без SMTP
    bot._entries_allowed = True
    return bot


def test_kill_switch_path_points_into_reports_dir(tmp_path):
    assert kill_switch_path(tmp_path) == tmp_path / "kill_switch.json"
    assert kill_switch_path(str(tmp_path)) == tmp_path / "kill_switch.json"


def test_write_read_clear_roundtrip(tmp_path):
    _write_kill_switch(tmp_path, PAYLOAD)

    assert read_kill_switch(tmp_path) == PAYLOAD
    # временный файл не остаётся после атомарной записи
    assert not list(tmp_path.glob("*.tmp"))

    assert clear_kill_switch(tmp_path) is True
    assert read_kill_switch(tmp_path) is None
    assert clear_kill_switch(tmp_path) is False


def test_missing_file_reads_as_none(tmp_path):
    assert read_kill_switch(tmp_path) is None


@pytest.mark.parametrize("content", ["{not json", "[]", "null", '"text"'])
def test_corrupt_file_fails_closed(tmp_path, content):
    kill_switch_path(tmp_path).write_text(content, encoding="utf-8")

    assert read_kill_switch(tmp_path) == {"corrupt": True}


def test_bot_with_existing_state_disables_entries_and_logs_once(tmp_path, caplog):
    _write_kill_switch(tmp_path, PAYLOAD)
    bot = make_state_bot(tmp_path)

    with caplog.at_level(logging.ERROR, logger="bot"):
        bot._load_kill_switch_state()

    assert bot._entries_allowed is False
    errors = [rec.getMessage() for rec in caplog.records if rec.levelno == logging.ERROR]
    assert any("kill-switch активен с 2026-10-24T10:00:00+00:00" in msg for msg in errors)
    assert any("cli.py kill-switch-reset" in msg for msg in errors)


def test_bot_without_state_file_keeps_entries_allowed(tmp_path):
    bot = make_state_bot(tmp_path)

    bot._load_kill_switch_state()

    assert bot._entries_allowed is True


def test_corrupt_state_disables_entries(tmp_path, caplog):
    kill_switch_path(tmp_path).write_text("{broken", encoding="utf-8")
    bot = make_state_bot(tmp_path)

    with caplog.at_level(logging.ERROR, logger="bot"):
        bot._load_kill_switch_state()

    assert bot._entries_allowed is False
    assert any("неизвестного времени" in rec.getMessage() for rec in caplog.records)


def test_trigger_writes_state_file(tmp_path):
    bot = make_trigger_bot(tmp_path)

    bot._check_kill_switch(800_000.0)

    assert bot._entries_allowed is False
    state = read_kill_switch(tmp_path)
    assert state is not None
    assert state["equity"] == pytest.approx(800_000.0)
    assert state["budget"] == pytest.approx(1_000_000.0)
    assert state["threshold"] == pytest.approx(0.15)
    assert state["triggered_at"]


def test_trigger_without_reports_dir_still_blocks_entries():
    # оффлайн-тесты собирают бота через __new__ без reports_dir — запись не обязательна
    bot = make_trigger_bot()

    bot._check_kill_switch(800_000.0)

    assert bot._entries_allowed is False

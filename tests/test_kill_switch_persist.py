"""Kill-switch переживает рестарт: файл состояния, fail-closed чтение, сброс (аудит F-C1.2)."""
from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import pytest

import bot as bot_module
from bot import (
    TradingBot,
    _write_kill_switch,
    _write_kill_switch_anchor,
    clear_kill_switch,
    kill_switch_path,
    read_kill_switch,
    read_kill_switch_anchor,
)
from notify import Notifier, SmtpConfig
from strategy import PortfolioState

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


# ---------- якорь kill-switch: сброс переносит точку отсчёта просадки ----------

def make_anchor_bot(reports_dir, equity: float) -> TradingBot:
    """Бот с известным капиталом и якорем = бюджет 100 000 (порог просадки 15%)."""
    bot = make_trigger_bot(reports_dir)
    bot._budget_rub = 100_000.0
    bot._kill_switch_anchor = 100_000.0
    bot._load_portfolio = lambda: PortfolioState(cash=equity, equity=equity, positions={})
    return bot


def test_reset_moves_anchor_to_current_equity_so_check_does_not_retrigger(tmp_path):
    """Капитал всё ещё ниже бюджет×(1−порог): без переноса якоря сброс взводился бы сам собой."""
    bot = make_anchor_bot(tmp_path, equity=80_000.0)  # < 100_000 × 0.85
    _write_kill_switch(tmp_path, PAYLOAD)
    bot._entries_allowed = False

    assert bot.reset_kill_switch() is True

    assert bot._entries_allowed is True
    assert read_kill_switch(tmp_path) is None
    bot._check_kill_switch(80_000.0)
    assert bot._entries_allowed is True  # просадка от НОВОГО якоря не превышена
    assert read_kill_switch(tmp_path) is None  # запрет не взведён заново, письма не будет
    assert read_kill_switch_anchor(tmp_path)["anchor_rub"] == pytest.approx(80_000.0)

    bot._check_kill_switch(67_900.0)  # −15% от нового якоря
    assert bot._entries_allowed is False
    assert read_kill_switch(tmp_path)["budget"] == pytest.approx(80_000.0)


def test_reset_when_switch_inactive_keeps_anchor(tmp_path):
    """`k` без активного запрета — не должно незаметно менять точку отсчёта просадки."""
    bot = make_anchor_bot(tmp_path, equity=120_000.0)
    bot._load_portfolio = lambda: pytest.fail("капитал для сброса неактивного запрета не нужен")

    assert bot.reset_kill_switch() is False

    assert bot._kill_switch_anchor == 100_000.0
    assert read_kill_switch_anchor(tmp_path) is None


def test_check_kill_switch_uses_anchor_not_budget(tmp_path):
    bot = make_anchor_bot(tmp_path, equity=80_000.0)
    bot._kill_switch_anchor = 50_000.0

    bot._check_kill_switch(80_000.0)

    assert bot._entries_allowed is True  # ниже бюджетного порога 85 000, но выше якорного 42 500


def test_trigger_persists_anchor(tmp_path):
    bot = make_trigger_bot(tmp_path)
    bot._budget_rub = 100_000.0
    bot._kill_switch_anchor = 90_000.0

    bot._check_kill_switch(75_000.0)

    assert read_kill_switch_anchor(tmp_path)["anchor_rub"] == pytest.approx(90_000.0)


def test_saved_anchor_is_restored_after_restart(tmp_path):
    _write_kill_switch_anchor(tmp_path, 80_000.0)
    bot = make_anchor_bot(tmp_path, equity=80_000.0)

    bot._restore_kill_switch_anchor()

    assert bot._kill_switch_anchor == pytest.approx(80_000.0)


def test_offline_reset_anchors_to_equity_at_daemon_start(tmp_path):
    """CLI/TUI/веб сбрасывают без API: якорь — капитал на момент следующего запуска демона."""
    _write_kill_switch(tmp_path, PAYLOAD)

    assert clear_kill_switch(tmp_path) is True
    assert read_kill_switch_anchor(tmp_path)["anchor_rub"] is None

    bot = make_anchor_bot(tmp_path, equity=80_000.0)
    bot._restore_kill_switch_anchor()

    assert bot._kill_switch_anchor == pytest.approx(80_000.0)
    assert read_kill_switch_anchor(tmp_path)["anchor_rub"] == pytest.approx(80_000.0)  # записан конкретно


@pytest.mark.parametrize("saved", [0, -5, "abc", float("nan")])
def test_unusable_saved_anchor_falls_back_to_budget(tmp_path, saved):
    (tmp_path / "kill_switch_anchor.json").write_text(
        json.dumps({"anchor_rub": saved, "reset_at": "x"}), encoding="utf-8"
    )
    bot = make_anchor_bot(tmp_path, equity=80_000.0)

    bot._restore_kill_switch_anchor()

    assert bot._kill_switch_anchor == 100_000.0


@pytest.mark.parametrize("content", ["{broken", "[]", '{"reset_at": "x"}'])
def test_corrupt_anchor_file_falls_back_to_budget(tmp_path, content):
    (tmp_path / "kill_switch_anchor.json").write_text(content, encoding="utf-8")
    bot = make_anchor_bot(tmp_path, equity=80_000.0)

    bot._restore_kill_switch_anchor()

    assert bot._kill_switch_anchor == 100_000.0


def test_reset_fails_when_equity_unavailable_and_keeps_entries_blocked(tmp_path):
    bot = make_anchor_bot(tmp_path, equity=80_000.0)
    _write_kill_switch(tmp_path, PAYLOAD)
    bot._entries_allowed = False

    def no_equity() -> PortfolioState:
        raise RuntimeError("API недоступен")

    bot._load_portfolio = no_equity
    with pytest.raises(RuntimeError):
        bot.reset_kill_switch()

    assert bot._entries_allowed is False
    assert bot._kill_switch_anchor == 100_000.0
    assert read_kill_switch(tmp_path) is not None  # запрет остался ровно как был
    assert bot_module.read_kill_switch_anchor(tmp_path) is None  # якорь не подменён

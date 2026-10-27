"""Защита от устаревших свечей: StaleDataError и пропуск тикера в цикле (аудит F-C1.5)."""
from __future__ import annotations

import datetime as dt
import logging
from types import SimpleNamespace

import pandas as pd
import pytest

import bot as bot_module
from bot import (
    _STALE_UNKNOWN_INTERVALS,
    StaleDataError,
    TradingBot,
)
from strategy import PortfolioState


def make_bot(interval: str = "hour") -> TradingBot:
    bot = TradingBot.__new__(TradingBot)
    bot.config = SimpleNamespace(candle_interval=interval)
    bot.conn = object()
    bot.api = object()
    return bot


def candles_at(ts: pd.Timestamp) -> pd.DataFrame:
    return pd.DataFrame({"close": [100.0]}, index=pd.DatetimeIndex([ts]))


def patch_candles(monkeypatch, df: pd.DataFrame) -> None:
    # сеть/БД не нужны: download — заглушка, load отдаёт заранее собранный кадр
    monkeypatch.setattr(bot_module, "download_candles", lambda *args, **kwargs: 0)
    monkeypatch.setattr(bot_module, "load_candles", lambda *args, **kwargs: df)


def utc_now() -> pd.Timestamp:
    return pd.Timestamp(dt.datetime.now(dt.timezone.utc))


def test_old_last_bar_raises_stale_data_error(monkeypatch):
    old = utc_now() - pd.Timedelta(hours=10)
    patch_candles(monkeypatch, candles_at(old))
    bot = make_bot("hour")

    with pytest.raises(StaleDataError) as excinfo:
        bot.refresh_candles("BBG_TEST", "SBER")

    message = str(excinfo.value)
    assert "SBER" in message
    assert "старше допустимого" in message
    assert old.isoformat() in message


def test_fresh_last_bar_returns_frame(monkeypatch):
    fresh = utc_now() - pd.Timedelta(minutes=5)
    frame = candles_at(fresh)
    patch_candles(monkeypatch, frame)
    bot = make_bot("hour")

    result = bot.refresh_candles("BBG_TEST", "SBER")

    assert result is frame


def test_unknown_interval_skips_guard_and_warns_once(monkeypatch, caplog):
    interval = "7min"
    _STALE_UNKNOWN_INTERVALS.discard(interval)
    old = utc_now() - pd.Timedelta(days=30)
    patch_candles(monkeypatch, candles_at(old))
    bot = make_bot(interval)

    try:
        with caplog.at_level(logging.WARNING, logger="bot"):
            assert bot.refresh_candles("BBG_TEST", "SBER").shape[0] == 1
            bot.refresh_candles("BBG_TEST", "SBER")

        warnings = [
            rec for rec in caplog.records
            if "не учтён в BAR_SECONDS" in rec.getMessage()
        ]
        assert len(warnings) == 1
    finally:
        _STALE_UNKNOWN_INTERVALS.discard(interval)


def test_naive_index_is_treated_as_utc(monkeypatch):
    fresh = pd.Timestamp(dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)) - pd.Timedelta(minutes=5)
    patch_candles(monkeypatch, candles_at(fresh))
    bot = make_bot("hour")

    # tz-naive метка не должна падать на вычитании aware/naive
    assert bot.refresh_candles("BBG_TEST", "SBER").shape[0] == 1


def test_run_forever_skips_stale_ticker_and_alerts(monkeypatch, tmp_path):
    bot = TradingBot.__new__(TradingBot)
    bot.config = SimpleNamespace(
        loop_interval_sec=0,
        reports_dir=tmp_path,
        mode="sandbox",
        candle_interval="hour",
        forecast_horizon=1,
        budget_rub=100_000.0,
    )
    bot.instruments = {"SBER": {"ticker": "SBER", "figi": "BBG_TEST", "lot": 1}}
    bot._budget_rub = 100_000.0
    bot._entries_allowed = True
    bot._running = False
    bot.trader = SimpleNamespace(
        initialize_balance=lambda budget: None,
        portfolio=lambda: {"total_amount_rub": 100_000.0, "cash_rub": 100_000.0, "positions": {}},
    )
    bot._setup_capital_profile = lambda: None
    bot.in_trading_session = lambda: True
    bot.update_news = lambda: None
    bot._load_portfolio = lambda: PortfolioState(cash=100_000.0, equity=100_000.0, positions={})
    bot._check_kill_switch = lambda equity: None
    bot.conn = SimpleNamespace(close=lambda: None)
    sent: list[tuple[str, str, str]] = []
    bot.notifier = SimpleNamespace(
        send_throttled=lambda key, subject, body: sent.append((key, subject, body))
    )

    def stale_step(ticker, instrument, portfolio, entries_allowed):
        raise StaleDataError(f"{ticker}: последняя свеча старая")

    bot.step_ticker = stale_step

    bot.run_forever(max_iterations=1)

    assert sent == [("stale-data:SBER", "свечи SBER устарели", "SBER: последняя свеча старая")]
    # generic-обработчик не должен был сработать: письмо об ошибке шага не отправлялось
    assert all(not key.startswith("step-error:") for key, _, _ in sent)

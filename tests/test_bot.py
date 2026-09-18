"""_load_portfolio: разбор позиций счёта без сети и БД (нужны только trader и instruments)."""
from __future__ import annotations

import logging
from types import SimpleNamespace

import pandas as pd
import pytest

from bot import FAILED_SELL_ESCALATION, TradingBot
from models.registry import ModelArtifact
from strategy import PortfolioState, Position

OZON_FIGI = "BBG00Y5RXXXX"


def make_bot(instruments: dict, raw_portfolio: dict) -> TradingBot:
    bot = TradingBot.__new__(TradingBot)
    bot.instruments = instruments
    bot.trader = SimpleNamespace(portfolio=lambda: raw_portfolio)
    return bot


def make_gate_bot(min_dir_acc: float = 0.5) -> TradingBot:
    bot = TradingBot.__new__(TradingBot)
    bot.config = SimpleNamespace(min_model_dir_acc=min_dir_acc)
    return bot


def test_model_gate_blocks_weak_directional_accuracy():
    # регрессия первого live-прогона: OZON торговался моделью persistence с dir_acc 0.488
    # (net sharpe не измерен — блокировка по обоим критериям)
    bot = make_gate_bot()
    artifact = ModelArtifact(kind="persistence", horizon=1, metrics={"directional_acc": 0.488})
    assert bot._model_allowed("OZON", artifact) is False


def test_model_gate_blocks_missing_or_nan_metric():
    bot = make_gate_bot()
    assert bot._model_allowed("T", ModelArtifact(kind="arima", horizon=1, metrics={})) is False
    nan = ModelArtifact(kind="ets", horizon=1, metrics={"directional_acc": float("nan")})
    assert bot._model_allowed("T", nan) is False


def test_model_gate_allows_confirmed_edge():
    bot = make_gate_bot()
    artifact = ModelArtifact(kind="lgbm", horizon=1, metrics={"directional_acc": 0.547})
    assert bot._model_allowed("SBER", artifact) is True


def test_model_gate_allows_positive_net_sharpe_with_subthreshold_dir_acc():
    # ARIMA/ETS после фикса statsmodels: dir_acc 47-48% (< порога 0.5), но edge
    # после издержек положительный — тот же критерий, по которому модель выбрана
    bot = make_gate_bot()
    artifact = ModelArtifact(
        kind="arima", horizon=1,
        metrics={"directional_acc": 0.476, "strategy_sharpe_net": 1.74},
    )
    assert bot._model_allowed("ALRS", artifact) is True


def test_model_gate_blocks_negative_net_sharpe_with_subthreshold_dir_acc():
    bot = make_gate_bot()
    artifact = ModelArtifact(
        kind="ma5_ret", horizon=1,
        metrics={"directional_acc": 0.480, "strategy_sharpe_net": -7.02},
    )
    assert bot._model_allowed("SBER", artifact) is False


def test_model_gate_blocks_nan_net_sharpe_with_subthreshold_dir_acc():
    bot = make_gate_bot()
    artifact = ModelArtifact(
        kind="arima", horizon=1,
        metrics={"directional_acc": 0.480, "strategy_sharpe_net": float("nan")},
    )
    assert bot._model_allowed("T", artifact) is False


def raw_portfolio(positions: dict) -> dict:
    return {
        "positions": positions,
        "cash_rub": 752_350.24,
        "total_amount_rub": 999_968.24,
    }


def test_tracked_position_gets_ticker():
    # регрессия: KeyError 'ticker' на первой же позиции с отслеживаемым figi
    instruments = {"OZON": {"ticker": "OZON", "figi": OZON_FIGI, "lot": 1}}
    raw = raw_portfolio(
        {OZON_FIGI: {"quantity": 92.0, "current_price": 2690.5, "average_position_price": 2690.5}}
    )

    portfolio = make_bot(instruments, raw)._load_portfolio()

    assert isinstance(portfolio, PortfolioState)
    pos = portfolio.positions[OZON_FIGI]
    assert pos.ticker == "OZON"
    assert pos.lots == 92
    assert pos.lot_size == 1
    assert pos.avg_price == 2690.5


def test_position_quantity_shares_converted_to_lots():
    # регрессия live-прогона 09-18.09.2026: GetPortfolio отдаёт ШТУКИ, а код клал
    # их в Position.lots → бот «владел» 9630 лотами вместо 963, любая продажа
    # отклонялась биржей и стоп-лосс не мог исполниться 9 дней
    instruments = {"ALRS": {"ticker": "ALRS", "figi": "BBG004S681W3", "lot": 10}}
    raw = raw_portfolio(
        {"BBG004S681W3": {"quantity": 9630.0, "current_price": 19.35, "average_position_price": 20.71}}
    )

    portfolio = make_bot(instruments, raw)._load_portfolio()

    pos = portfolio.positions["BBG004S681W3"]
    assert pos.lot_size == 10
    assert pos.lots == 963


def test_position_fractional_share_remainder_floors_to_whole_lot():
    # корп. действия могут дать некратное лоту количество штук: 9635 шт = 963 лота
    instruments = {"ALRS": {"ticker": "ALRS", "figi": "BBG004S681W3", "lot": 10}}
    raw = raw_portfolio(
        {"BBG004S681W3": {"quantity": 9635.0, "current_price": 19.35, "average_position_price": 20.71}}
    )

    portfolio = make_bot(instruments, raw)._load_portfolio()

    assert portfolio.positions["BBG004S681W3"].lots == 963


def test_instrument_without_ticker_field_falls_back_to_figi():
    # словарь инструмента в старом формате (без ключа ticker) не должен ломать итерацию
    instruments = {"OZON": {"figi": OZON_FIGI, "lot": 1}}
    raw = raw_portfolio(
        {OZON_FIGI: {"quantity": 92.0, "current_price": 2690.5, "average_position_price": 2690.5}}
    )

    portfolio = make_bot(instruments, raw)._load_portfolio()

    assert portfolio.positions[OZON_FIGI].ticker == OZON_FIGI


def test_unknown_figi_position_kept_with_figi_as_ticker():
    # валютная позиция (ключ = instrumentUid) не совпадает ни с одним инструментом
    instruments = {"LKOH": {"ticker": "LKOH", "figi": "BBG004730NMR", "lot": 1}}
    raw = raw_portfolio(
        {"a1b2c3d4-uid": {"quantity": 1000.0, "current_price": 1.0, "average_position_price": 1.0}}
    )

    portfolio = make_bot(instruments, raw)._load_portfolio()

    pos = portfolio.positions["a1b2c3d4-uid"]
    assert pos.ticker == "a1b2c3d4-uid"
    assert pos.lot_size == 1


def make_step_bot(figi: str, sell_state: dict) -> tuple[TradingBot, dict]:
    """Бот для step_ticker без сети: свечи/прогнозы/новости зашиты, продажа возвращает sell_state."""
    bot = TradingBot.__new__(TradingBot)
    bot.instruments = {"ALRS": {"ticker": "ALRS", "figi": figi, "lot": 10}}
    bot.risk = __import__("strategy").RiskConfig()
    bot._sell_failures = {}
    calls = {"sell": 0}
    sent: list[tuple[str, str]] = []

    def fake_sell(instrument_id, lots, price):
        calls["sell"] += 1
        return sell_state

    bot.trader = SimpleNamespace(sell=fake_sell, log_trade=lambda row: None)
    bot.notifier = SimpleNamespace(
        send_throttled=lambda key, subject, body: sent.append((key, subject)),
        send=lambda subject, body: None,
    )
    artifact = ModelArtifact(kind="arima", horizon=1, metrics={"directional_acc": 0.6}, threshold=0.0)
    bot._artifact = lambda ticker: artifact
    # цена 19.0 против средней 20.71 = -8.3% → стоп-лосс сработает на первом же шаге
    candles = pd.DataFrame(
        {"close": [19.0]}, index=pd.DatetimeIndex([pd.Timestamp("2026-09-18 12:00", tz="UTC")])
    )
    bot.refresh_candles = lambda figi_, ticker: candles
    bot.ticker_agenda = lambda instrument, ticker: SimpleNamespace(sentiment=0.0, n_items=0)
    bot.predict = lambda ticker, c, a: (0.0, 0.0)
    return bot, {"calls": calls, "sent": sent}


def test_repeated_failed_sell_escalates_to_error_and_alert(caplog):
    # регрессия live-прогона 09-18.09.2026: продажа отклонялась биржей каждую
    # итерацию 9 дней и никто не замечал — после 3 подряд шумим ERROR-логом и письмом
    figi = "BBG004S681W3"
    bot, env = make_step_bot(
        figi, {"status": "rejected", "lots_executed": 0, "order_id": "o1", "avg_exec_price": 0.0}
    )
    portfolio = PortfolioState(
        cash=0.0, equity=100_000.0, positions={figi: Position(figi, "ALRS", 963, 10, 20.71)}
    )

    with caplog.at_level(logging.ERROR, logger="bot"):
        for _ in range(FAILED_SELL_ESCALATION):
            bot.step_ticker("ALRS", bot.instruments["ALRS"], portfolio)

    assert env["calls"]["sell"] == FAILED_SELL_ESCALATION
    assert bot._sell_failures[figi] == FAILED_SELL_ESCALATION
    assert any("подряд неудачных продаж" in rec.getMessage() for rec in caplog.records)
    assert any(key.startswith("sell-stuck:") for key, _ in env["sent"])


def test_successful_sell_resets_failure_counter():
    figi = "BBG004S681W3"
    bot, env = make_step_bot(
        figi, {"status": "fill", "lots_executed": 963, "order_id": "o1", "avg_exec_price": 19.0}
    )
    portfolio = PortfolioState(
        cash=0.0, equity=100_000.0, positions={figi: Position(figi, "ALRS", 963, 10, 20.71)}
    )
    bot._sell_failures[figi] = FAILED_SELL_ESCALATION - 1

    bot.step_ticker("ALRS", bot.instruments["ALRS"], portfolio)

    assert env["calls"]["sell"] == 1
    assert figi not in bot._sell_failures

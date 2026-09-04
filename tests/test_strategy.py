from __future__ import annotations

import pytest

from strategy import PortfolioState, Position, RiskConfig, calc_cost, decide, lots_for_budget


@pytest.fixture
def risk() -> RiskConfig:
    return RiskConfig(
        max_position_pct=0.25,
        stop_loss_pct=0.03,
        take_profit_pct=0.05,
        min_abs_return=0.004,
        news_sentiment_gate=-0.35,
    )


def test_buy_signal(risk):
    portfolio = PortfolioState(cash=1_000_000, equity=1_000_000)
    d = decide("SBER", "FG", 250.0, 0.01, 0.2, portfolio, lot_size=10, risk=risk)
    assert d.action == "BUY"
    # бюджет 25% от 1М = 250_000; по 2500 за лот -> 100 лотов
    assert d.lots == 100


def test_no_buy_below_threshold(risk):
    portfolio = PortfolioState(cash=1_000_000, equity=1_000_000)
    d = decide("SBER", "FG", 250.0, 0.001, 0.2, portfolio, lot_size=10, risk=risk)
    assert d.action == "HOLD"


def test_news_gate_blocks_buy(risk):
    portfolio = PortfolioState(cash=1_000_000, equity=1_000_000)
    d = decide("SBER", "FG", 250.0, 0.01, -0.5, portfolio, lot_size=10, risk=risk)
    assert d.action == "HOLD"
    assert "новостной фильтр" in d.reason


def test_news_gate_allows_neutral(risk):
    portfolio = PortfolioState(cash=1_000_000, equity=1_000_000)
    d = decide("SBER", "FG", 250.0, 0.01, 0.0, portfolio, lot_size=10, risk=risk)
    assert d.action == "BUY"


def test_stop_loss_exit(risk):
    position = Position("FG", "SBER", lots=10, lot_size=10, avg_price=250.0)
    portfolio = PortfolioState(cash=0, equity=1_000_000, positions={"FG": position})
    # цена упала на 5% -> стоп-лосс независимо от прогноза
    d = decide("SBER", "FG", 237.5, 0.02, 0.5, portfolio, lot_size=10, risk=risk)
    assert d.action == "SELL"
    assert "стоп-лосс" in d.reason


def test_take_profit_exit(risk):
    position = Position("FG", "SBER", lots=10, lot_size=10, avg_price=250.0)
    portfolio = PortfolioState(cash=0, equity=1_000_000, positions={"FG": position})
    d = decide("SBER", "FG", 265.0, 0.01, 0.0, portfolio, lot_size=10, risk=risk)
    assert d.action == "SELL"
    assert "тейк-профит" in d.reason


def test_model_reversal_exit(risk):
    position = Position("FG", "SBER", lots=10, lot_size=10, avg_price=250.0)
    portfolio = PortfolioState(cash=0, equity=1_000_000, positions={"FG": position})
    d = decide("SBER", "FG", 250.5, -0.01, 0.0, portfolio, lot_size=10, risk=risk)
    assert d.action == "SELL"
    assert "развернулся" in d.reason


def test_hold_in_band(risk):
    position = Position("FG", "SBER", lots=10, lot_size=10, avg_price=250.0)
    portfolio = PortfolioState(cash=0, equity=1_000_000, positions={"FG": position})
    d = decide("SBER", "FG", 251.0, 0.002, 0.0, portfolio, lot_size=10, risk=risk)
    assert d.action == "HOLD"


def test_lot_sizing_respects_cash(risk):
    portfolio = PortfolioState(cash=20_000, equity=1_000_000)
    d = decide("SBER", "FG", 250.0, 0.01, 0.2, portfolio, lot_size=10, risk=risk)
    assert d.action == "BUY" and d.lots * 250 * 10 <= 20_000


def test_helpers(risk):
    assert lots_for_budget(100_000, 250.0, 10) == 40
    assert lots_for_budget(0, 250.0, 10) == 0
    # 10 лотов * 10 акций * 250 руб * 0.04%
    assert calc_cost(250.0, 10, 10, 0.0004) == pytest.approx(250 * 10 * 10 * 0.0004)

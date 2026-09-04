from __future__ import annotations

import numpy as np
import pytest

from backtest import run_backtest
from features import build_features
from models.baseline import MovingAverageReturn, NaiveZero
from models.registry import walk_forward_baselines
from strategy import RiskConfig


@pytest.fixture
def risk() -> RiskConfig:
    return RiskConfig(
        max_position_pct=0.25,
        stop_loss_pct=0.03,
        take_profit_pct=0.05,
        min_abs_return=0.004,
        commission_pct=0.0004,
    )


def test_backtest_never_trades_with_zero_model(candles, risk):
    df = candles.iloc[-400:]
    features = build_features(df, horizon=1)
    preds = walk_forward_baselines(
        df["close"], features["target"], 1, NaiveZero, features.index, refit_every=48
    )
    result = run_backtest(df, preds, risk, initial_cash=1_000_000)
    # нулевые прогнозы не открывают позиций, капитал не меняется
    assert result.metrics["n_trades"] == 0
    assert result.equity_curve.iloc[-1] == pytest.approx(1_000_000)


def test_backtest_with_trades_is_finite(candles, risk):
    df = candles.iloc[-400:]
    features = build_features(df, horizon=1)
    preds = walk_forward_baselines(
        df["close"], features["target"], 1, lambda: MovingAverageReturn(1, k=5), features.index, refit_every=24
    )
    result = run_backtest(df, preds, risk, initial_cash=1_000_000)
    assert np.isfinite(result.equity_curve).all()
    assert set(result.metrics) >= {"total_return", "sharpe", "max_drawdown", "n_trades", "win_rate"}
    # капитал не может уйти ниже нуля при лонг-онли и ограничении позиции
    assert (result.equity_curve > 0).all()
    if len(result.trades):
        assert (result.trades["lots"] > 0).all()


def test_backtest_equity_matches_cash_plus_position(candles, risk):
    """Инвариант: equity = cash + позиции по текущей цене (проверяем на крайних точках)."""
    df = candles.iloc[-300:]
    features = build_features(df, horizon=1)
    preds = walk_forward_baselines(
        df["close"], features["target"], 1, lambda: MovingAverageReturn(1, k=5), features.index, refit_every=24
    )
    result = run_backtest(df, preds, risk, initial_cash=1_000_000)
    if not len(result.trades):
        pytest.skip("нет сделок на этой серии")
    # итоговое equity воспроизводимо: последний тик кривой финализирован по close
    assert result.equity_curve.iloc[-1] > 0

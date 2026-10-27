"""Отчёт бэктеста: имя JSON с метриками и отсутствие пустого заголовка в markdown."""
from __future__ import annotations

import json

import pandas as pd
import pytest

from backtest import BacktestResult, save_report


def _result() -> BacktestResult:
    index = pd.date_range("2026-01-01", periods=3, freq="h", tz="UTC")
    equity = pd.Series([100.0, 101.0, 102.0], index=index, name="equity")
    trades = pd.DataFrame(
        columns=["time", "action", "ticker", "lots", "price", "reason", "commission", "pnl", "return_pct"]
    )
    metrics = {
        "total_return": 0.02,
        "cagr": 0.2,
        "sharpe": 1.0,
        "max_drawdown": -0.01,
        "n_trades": 0,
        "win_rate": 0.0,
        "realized_pnl": 0.0,
        "total_commission": 0.0,
        "benchmark_buyhold": 0.01,
    }
    return BacktestResult(equity, trades, metrics)


def test_save_report_uses_backtest_metrics_name_and_no_empty_heading(tmp_path):
    report_path = save_report(_result(), tmp_path, "unit")

    assert report_path == tmp_path / "backtest_unit.md"
    md = report_path.read_text(encoding="utf-8")
    # «Кривая капитала» — строка со ссылкой на CSV, а не висячий пустой заголовок
    assert "## Кривая капитала" not in md
    assert "equity_unit.csv" in md

    metrics_files = sorted(tmp_path.glob("*metrics*.json"))
    assert [p.name for p in metrics_files] == ["backtest_metrics_unit.json"]
    assert not (tmp_path / "metrics_unit.json").exists()
    assert json.loads(metrics_files[0].read_text(encoding="utf-8"))["total_return"] == pytest.approx(0.02)

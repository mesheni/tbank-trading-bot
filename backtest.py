"""Walk-forward бэктест стратегии на исторических свечах с прогнозами модели.

Учитывает комиссию, проскальзывание, стоп-лосс/тейк-профит по экстремумам бара.
Лонг-онли, один инструмент за прогон (портфель бота — сумма независимых прогонов).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from strategy import PortfolioState, Position, RiskConfig, calc_cost, decide


@dataclass
class BacktestResult:
    equity_curve: pd.Series
    trades: pd.DataFrame
    metrics: dict


def run_backtest(
    candles: pd.DataFrame,
    predictions: pd.Series,
    risk: RiskConfig,
    initial_cash: float = 1_000_000.0,
    lot_size: int = 1,
    ticker: str = "TICKER",
) -> BacktestResult:
    """candles: OHLCV-DataFrame (DatetimeIndex); predictions: прогноз доходности по времени бара."""
    common = candles.index.intersection(predictions.index)
    candles = candles.loc[common]
    preds = predictions.loc[common]

    cash = initial_cash
    equity = initial_cash
    position: Position | None = None
    equity_points: dict[pd.Timestamp, float] = {}
    trades: list[dict] = []
    commission = risk.commission_pct
    slip = 0.0002

    for ts, row in candles.iterrows():
        price = float(row["close"])
        high, low = float(row["high"]), float(row["low"])
        r_hat = float(preds.get(ts, 0.0))
        sentiment = 0.0  # в офлайн-бэктесте новости не моделируются

        # 1) интрабарные стопы по существующей позиции (по high/low бара)
        if position is not None and position.avg_price > 0:
            pnl_low = low / position.avg_price - 1.0
            pnl_high = high / position.avg_price - 1.0
            if pnl_low <= -risk.stop_loss_pct:
                sell_price = position.avg_price * (1 - risk.stop_loss_pct) * (1 - slip)
                cash += sell_price * position.lots * lot_size * (1 - commission)
                trades.append(_trade(ts, "SELL", ticker, position.lots, sell_price, "stop_loss"))
                position = None
            elif pnl_high >= risk.take_profit_pct:
                sell_price = position.avg_price * (1 + risk.take_profit_pct) * (1 - slip)
                cash += sell_price * position.lots * lot_size * (1 - commission)
                trades.append(_trade(ts, "SELL", ticker, position.lots, sell_price, "take_profit"))
                position = None

        # 2) решение по закрытию бара
        portfolio = PortfolioState(
            cash=cash,
            equity=cash + (position.lots * lot_size * price if position else 0.0),
            positions={position.figi: position} if position else {},
        )
        decision = decide(
            ticker=ticker,
            figi=ticker,
            price=price,
            predicted_return=r_hat,
            news_sentiment=sentiment,
            portfolio=portfolio,
            lot_size=lot_size,
            risk=risk,
        )

        if decision.action == "BUY" and position is None:
            exec_price = price * (1 + slip)
            cost = exec_price * decision.lots * lot_size
            fee = cost * commission
            if cost + fee <= cash + 1e-9:
                cash -= cost + fee
                position = Position(ticker, ticker, decision.lots, lot_size, exec_price)
                trades.append(_trade(ts, "BUY", ticker, decision.lots, exec_price, decision.reason))
        elif decision.action == "SELL" and position is not None:
            exec_price = price * (1 - slip)
            proceeds = exec_price * position.lots * lot_size
            fee = proceeds * commission
            cash += proceeds - fee
            trades.append(_trade(ts, "SELL", ticker, position.lots, exec_price, decision.reason))
            position = None

        equity_points[ts] = cash + (position.lots * lot_size * price if position else 0.0)

    equity_curve = pd.Series(equity_points, name="equity").sort_index()
    metrics = _metrics(equity_curve, trades, initial_cash, candles)
    return BacktestResult(equity_curve, pd.DataFrame(trades), metrics)


def _trade(ts, action, ticker, lots, price, reason) -> dict:
    return {"time": ts, "action": action, "ticker": ticker, "lots": lots, "price": round(price, 4), "reason": reason}


def _metrics(equity: pd.Series, trades: list[dict], initial_cash: float, candles: pd.DataFrame) -> dict:
    if equity.empty:
        return {}
    total_return = float(equity.iloc[-1] / initial_cash - 1.0)

    bars_per_year = _bars_per_year(candles)
    bar_returns = equity.pct_change().dropna()
    sharpe = 0.0
    if bar_returns.std() > 1e-12:
        sharpe = float(bar_returns.mean() / bar_returns.std() * np.sqrt(bars_per_year))

    running_max = equity.cummax()
    drawdown = equity / running_max - 1.0
    max_drawdown = float(drawdown.min())

    sells = [t for t in trades if t["action"] == "SELL"]
    wins = 0
    for i, sell in enumerate(sells):
        prior_buys = [t for t in trades if t["action"] == "BUY" and t["time"] < sell["time"]]
        if prior_buys and sell["price"] > prior_buys[-1]["price"]:
            wins += 1
    win_rate = wins / len(sells) if sells else 0.0

    years = max(1e-9, len(equity) / bars_per_year)
    cagr = (1.0 + total_return) ** (1 / years) - 1.0 if total_return > -1 else -1.0

    return {
        "total_return": total_return,
        "cagr": float(cagr),
        "sharpe": sharpe,
        "max_drawdown": max_drawdown,
        "n_trades": len(trades),
        "win_rate": win_rate,
    }


def _bars_per_year(candles: pd.DataFrame) -> float:
    if len(candles) < 2:
        return 252.0
    seconds_per_bar = (candles.index[-1] - candles.index[0]).total_seconds() / (len(candles) - 1)
    # торговый год MOEX ~ 247 сессий; дневной бар ~ 8.5ч торговли
    if seconds_per_bar >= 20 * 3600:
        return 247.0
    return 247.0 * (8.5 * 3600) / max(seconds_per_bar, 1.0)


def save_report(result: BacktestResult, reports_dir: Path, name: str) -> Path:
    reports_dir.mkdir(parents=True, exist_ok=True)
    path = reports_dir / f"backtest_{name}.md"
    m = result.metrics
    lines = [
        f"# Бэктест: {name}",
        "",
        "| Метрика | Значение |",
        "|---|---|",
        f"| Итоговая доходность | {m.get('total_return', 0):+.2%} |",
        f"| CAGR | {m.get('cagr', 0):+.2%} |",
        f"| Sharpe | {m.get('sharpe', 0):.2f} |",
        f"| Макс. просадка | {m.get('max_drawdown', 0):+.2%} |",
        f"| Сделок | {m.get('n_trades', 0)} |",
        f"| Win rate | {m.get('win_rate', 0):.1%} |",
        "",
        "## Кривая капитала",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
    result.equity_curve.to_csv(reports_dir / f"equity_{name}.csv")
    result.trades.to_csv(reports_dir / f"trades_{name}.csv", index=False)
    (reports_dir / f"metrics_{name}.json").write_text(json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8")
    return path

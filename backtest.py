"""Walk-forward бэктест стратегии на исторических свечах с прогнозами модели.

Учитывает комиссию, проскальзывание (из RiskConfig), стоп-лосс/тейк-профит
по экстремумам бара (гэп сквозь стоп исполняется по цене открытия), новостной
фильтр (если передана историческая серия сентимента). Сделки закрываются FIFO,
в trades_*.csv попадают комиссия и реализованный P&L каждой сделки.
Лонг-онли, один инструмент за прогон (портфель бота — сумма независимых прогонов).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from strategy import PortfolioState, Position, RiskConfig, decide
from stats_utils import bars_per_year


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
    sentiment: pd.Series | None = None,
) -> BacktestResult:
    """candles: OHLCV-DataFrame (DatetimeIndex); predictions: прогноз доходности по времени бара.

    sentiment: историческая серия скоров повестки по времени бара (см. nlp.agenda.sentiment_series);
    без неё новостной фильтр считается нейтральным (0.0).
    """
    common = candles.index.intersection(predictions.index)
    candles = candles.loc[common]
    preds = predictions.loc[common]

    cash = initial_cash
    segments: list[list] = []  # FIFO-сегменты покупки: [лоты, цена исполнения]
    position: Position | None = None
    equity_points: dict[pd.Timestamp, float] = {}
    trades: list[dict] = []
    commission = risk.commission_pct
    slip = risk.slippage_pct

    def open_lots() -> int:
        return sum(seg[0] for seg in segments)

    def avg_price() -> float:
        lots = open_lots()
        return sum(seg[0] * seg[1] for seg in segments) / lots if lots > 0 else 0.0

    def sync_position() -> None:
        nonlocal position
        position = Position(ticker, ticker, open_lots(), lot_size, avg_price()) if segments else None

    def do_buy(ts, lots: int, price: float, reason: str) -> None:
        nonlocal cash
        exec_price = price * (1 + slip)
        cost = exec_price * lots * lot_size
        fee = cost * commission
        if cost + fee > cash + 1e-9:
            return
        cash -= cost + fee
        segments.append([lots, exec_price])
        sync_position()
        trades.append(_trade(ts, "BUY", ticker, lots, exec_price, reason, commission=fee))

    def do_sell(ts, lots_requested: int, price: float, reason: str) -> None:
        """FIFO-закрытие до lots_requested лотов с фиксацией реализованного P&L."""
        nonlocal cash
        exec_price = price * (1 - slip)
        to_close = min(lots_requested, open_lots())
        if to_close <= 0:
            return
        pnl = 0.0
        cost_basis = 0.0
        buy_fees = 0.0
        remaining = to_close
        while remaining > 0 and segments:
            seg_lots, seg_price = segments[0]
            take = min(remaining, seg_lots)
            pnl += (exec_price - seg_price) * take * lot_size
            cost_basis += seg_price * take * lot_size
            buy_fees += seg_price * take * lot_size * commission
            segments[0][0] = seg_lots - take
            remaining -= take
            if segments[0][0] == 0:
                segments.pop(0)
        proceeds = exec_price * to_close * lot_size
        sell_fee = proceeds * commission
        cash += proceeds - sell_fee
        sync_position()
        # комиссия строки — только стороны продажи (вход учтён своей строкой);
        # pnl — нетто-экономика круга: минус комиссии обеих сторон
        net_pnl = pnl - (buy_fees + sell_fee)
        trades.append(
            _trade(
                ts, "SELL", ticker, to_close, exec_price, reason,
                commission=sell_fee, pnl=net_pnl,
                return_pct=net_pnl / cost_basis if cost_basis > 0 else None,
            )
        )

    for ts, row in candles.iterrows():
        price = float(row["close"])
        high, low, bar_open = float(row["high"]), float(row["low"]), float(row["open"])
        r_hat = float(preds.get(ts, 0.0))
        sent = float(sentiment.get(ts, 0.0)) if sentiment is not None else 0.0

        # 1) интрабарные стопы по существующей позиции (по high/low бара);
        #    гэп сквозь уровень исполняется по цене открытия бара, не по уровню
        if position is not None and position.avg_price > 0:
            pnl_low = low / position.avg_price - 1.0
            pnl_high = high / position.avg_price - 1.0
            if pnl_low <= -risk.stop_loss_pct:
                stop_level = position.avg_price * (1 - risk.stop_loss_pct)
                do_sell(ts, position.lots, min(stop_level, bar_open), "stop_loss")
            elif pnl_high >= risk.take_profit_pct:
                tp_level = position.avg_price * (1 + risk.take_profit_pct)
                do_sell(ts, position.lots, max(tp_level, bar_open), "take_profit")

        # 2) решение по закрытию бара
        portfolio = PortfolioState(
            cash=cash,
            equity=cash + (open_lots() * lot_size * price if segments else 0.0),
            positions={position.figi: position} if position else {},
        )
        decision = decide(
            ticker=ticker,
            figi=ticker,
            price=price,
            predicted_return=r_hat,
            news_sentiment=sent,
            portfolio=portfolio,
            lot_size=lot_size,
            risk=risk,
        )

        if decision.action == "BUY" and position is None:
            do_buy(ts, decision.lots, price, decision.reason)
        elif decision.action == "SELL" and position is not None:
            do_sell(ts, position.lots, price, decision.reason)

        equity_points[ts] = cash + (open_lots() * lot_size * price if segments else 0.0)

    equity_curve = pd.Series(equity_points, name="equity").sort_index()
    metrics = _metrics(equity_curve, trades, initial_cash, candles)
    return BacktestResult(equity_curve, pd.DataFrame(trades), metrics)


def _trade(
    ts, action, ticker, lots, price, reason,
    commission: float | None = None, pnl: float | None = None, return_pct: float | None = None,
) -> dict:
    return {
        "time": ts,
        "action": action,
        "ticker": ticker,
        "lots": lots,
        "price": round(price, 4),
        "reason": reason,
        "commission": round(commission, 4) if commission is not None else None,
        "pnl": round(pnl, 4) if pnl is not None else None,
        "return_pct": round(return_pct, 6) if return_pct is not None else None,
    }


def _metrics(equity: pd.Series, trades: list[dict], initial_cash: float, candles: pd.DataFrame) -> dict:
    if equity.empty:
        return {}
    total_return = float(equity.iloc[-1] / initial_cash - 1.0)

    bars = bars_per_year(candles)
    bar_returns = equity.pct_change().dropna()
    sharpe = 0.0
    if bar_returns.std() > 1e-12:
        sharpe = float(bar_returns.mean() / bar_returns.std() * np.sqrt(bars))

    running_max = equity.cummax()
    drawdown = equity / running_max - 1.0
    max_drawdown = float(drawdown.min())

    sells = [t for t in trades if t["action"] == "SELL" and t.get("pnl") is not None]
    wins = sum(1 for t in sells if t["pnl"] > 0)
    win_rate = wins / len(sells) if sells else 0.0
    realized_pnl = float(sum(t["pnl"] for t in sells))
    total_commission = float(sum(t.get("commission") or 0.0 for t in trades))

    # бенчмарк: купить и держать тот же инструмент на том же тестовом окне
    benchmark = 0.0
    if len(candles) >= 2 and float(candles["close"].iloc[0]) > 0:
        benchmark = float(candles["close"].iloc[-1]) / float(candles["close"].iloc[0]) - 1.0

    years = max(1e-9, len(equity) / bars)
    cagr = (1.0 + total_return) ** (1 / years) - 1.0 if total_return > -1 else -1.0

    return {
        "total_return": total_return,
        "cagr": float(cagr),
        "sharpe": sharpe,
        "max_drawdown": max_drawdown,
        "n_trades": len(trades),
        "win_rate": win_rate,
        "realized_pnl": realized_pnl,
        "total_commission": total_commission,
        "benchmark_buyhold": benchmark,
    }


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
        f"| Реализованный P&L | {m.get('realized_pnl', 0):+,.0f} |",
        f"| Комиссии (всего) | {m.get('total_commission', 0):,.0f} |",
        f"| Buy&hold того же окна | {m.get('benchmark_buyhold', 0):+.2%} |",
        f"| Стратегия минус бенчмарк | {m.get('total_return', 0) - m.get('benchmark_buyhold', 0):+.2%} |",
        "",
        "## Кривая капитала",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
    result.equity_curve.to_csv(reports_dir / f"equity_{name}.csv")
    result.trades.to_csv(reports_dir / f"trades_{name}.csv", index=False)
    (reports_dir / f"metrics_{name}.json").write_text(json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8")
    return path

"""Торговая стратегия: сигнал модели + новостной фильтр + риск-правила.

Чистые функции без обращений к API — легко тестируются.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field


@dataclass
class Position:
    figi: str
    ticker: str
    lots: int
    lot_size: int
    avg_price: float


@dataclass
class PortfolioState:
    cash: float
    equity: float  # cash + стоимость позиций по текущим ценам
    positions: dict[str, Position] = field(default_factory=dict)  # figi -> Position


@dataclass
class RiskConfig:
    max_position_pct: float = 0.25
    stop_loss_pct: float = 0.03
    take_profit_pct: float = 0.05
    min_abs_return: float = 0.004
    news_sentiment_gate: float = -0.35
    commission_pct: float = 0.0004


@dataclass
class Decision:
    action: str  # BUY | SELL | HOLD
    lots: int = 0
    reason: str = ""
    price: float = 0.0


def decide(
    ticker: str,
    figi: str,
    price: float,
    predicted_return: float,
    news_sentiment: float,
    portfolio: PortfolioState,
    lot_size: int,
    risk: RiskConfig,
) -> Decision:
    """Решение по одному инструменту на текущем баре.

    Логика:
      * вход в лонг — прогноз доходности выше порога и новостной фильтр пройден;
      * выход — стоп-лосс, тейк-профит, разворот прогноза вниз или резкое
        ухудшение новостей;
      * размер позиции — доля `max_position_pct` от equity, кратно лоту.
    """
    position = portfolio.positions.get(figi)

    if position is None:
        if price <= 0 or lot_size <= 0:
            return Decision("HOLD", 0, "нет цены/лота", price)
        if predicted_return < risk.min_abs_return:
            return Decision("HOLD", 0, f"прогноз {predicted_return:+.4f} ниже порога", price)
        if news_sentiment < risk.news_sentiment_gate:
            return Decision("HOLD", 0, f"новостной фильтр: sentiment={news_sentiment:+.2f}", price)
        budget = portfolio.equity * risk.max_position_pct
        lots = int(budget / (price * lot_size))
        if lots < 1:
            return Decision("HOLD", 0, "недостаточно средств на 1 лот", price)
        max_affordable = int(portfolio.cash / (price * lot_size))
        lots = min(lots, max_affordable)
        if lots < 1:
            return Decision("HOLD", 0, "не хватает свободных денег", price)
        return Decision("BUY", lots, f"прогноз {predicted_return:+.4f}, sentiment={news_sentiment:+.2f}", price)

    # позиция есть
    if position.avg_price <= 0:
        return Decision("HOLD", 0, "нет средней цены позиции", price)
    pnl_pct = price / position.avg_price - 1.0

    if pnl_pct <= -risk.stop_loss_pct:
        return Decision("SELL", position.lots, f"стоп-лосс {pnl_pct:+.2%}", price)
    if pnl_pct >= risk.take_profit_pct:
        return Decision("SELL", position.lots, f"тейк-профит {pnl_pct:+.2%}", price)
    if predicted_return < -risk.min_abs_return:
        return Decision("SELL", position.lots, f"прогноз развернулся {predicted_return:+.4f}", price)
    if news_sentiment < risk.news_sentiment_gate * 1.5:
        return Decision("SELL", position.lots, f"новости резко негативны {news_sentiment:+.2f}", price)
    return Decision("HOLD", 0, f"держим, pnl {pnl_pct:+.2%}", price)


def calc_cost(price: float, lots: int, lot_size: int, commission_pct: float) -> float:
    return price * lots * lot_size * commission_pct


def lots_for_budget(budget: float, price: float, lot_size: int) -> int:
    if price <= 0 or lot_size <= 0:
        return 0
    return max(0, math.floor(budget / (price * lot_size)))

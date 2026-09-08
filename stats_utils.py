"""Общая статистика таймсерий: календарная аннуализация и разметка walk-forward.

Используется и бэктестом (backtest.py), и реестром моделей (models/registry.py),
чтобы метрики и сплиты не расходились между обучением и оценкой.
"""
from __future__ import annotations

import pandas as pd

# Ориентир на классический график торгов (247 дней × 8.5 ч) — фолбэк, когда
# шаг индекса измерить нельзя; с вечерними/выходными сессиями MOEX реальность ~7000.
DEFAULT_BARS_PER_YEAR = 2100.0


def bars_per_year(candles: pd.DataFrame) -> float:
    """Число баров в календарном году по фактическому среднему шагу индекса.

    Шаг между крайними точками учитывает пропуски (ночи, выходные), поэтому
    формула сама подстраивается под режим торгов: с вечерними и выходными
    сессиями MOEX часовых баров ~7000/год, при классическом графике 247×8.5ч
    получилось бы ~2100 — жёсткие предположения здесь занижали CAGR в ~4 раза.
    """
    if len(candles) < 2:
        return DEFAULT_BARS_PER_YEAR
    seconds_per_bar = (candles.index[-1] - candles.index[0]).total_seconds() / (len(candles) - 1)
    if seconds_per_bar <= 0:
        return DEFAULT_BARS_PER_YEAR
    return 365.25 * 24 * 3600 / seconds_per_bar


def n_test_points(n_total: int, frac: float = 0.25, min_points: int = 30) -> int:
    """Размер walk-forward теста: доля истории, но не меньше min_points.

    Единое правило для evaluate_all и cmd_backtest, чтобы отбор модели
    и её бэктест оценивались на одном и том же тестовом окне.
    """
    return max(min_points, int(n_total * frac))

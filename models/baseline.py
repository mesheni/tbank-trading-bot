"""Бейзлайны прогноза доходности на горизонт H баров.

Единый интерфейс: fit(close: pd.Series) -> self; predict() -> float
(прогнозируемая доходность за горизонт, доля).
"""
from __future__ import annotations

import logging
import warnings

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)


class NaiveZero:
    """Предсказываем нулевую доходность (самый строгий бейзлайн для доходностей)."""

    name = "naive_zero"
    refit_policy = "periodic"  # прогноз константный — переобучение не меняет ничего

    def fit(self, close: pd.Series) -> "NaiveZero":
        return self

    def predict(self) -> float:
        return 0.0


class PersistenceReturn:
    """Повторение последней однобаровой доходности на весь горизонт."""

    name = "persistence"
    refit_policy = "every_bar"  # прогноз зависит от последнего бара — нужен свежий фит

    def __init__(self, horizon: int = 1):
        self.horizon = horizon
        self._last_ret = 0.0

    def fit(self, close: pd.Series) -> "PersistenceReturn":
        self._last_ret = float(close.pct_change().iloc[-1]) if len(close) > 1 else 0.0
        return self

    def predict(self) -> float:
        return self._last_ret * self.horizon


class MovingAverageReturn:
    """Средняя доходность за k баров, экстраполированная на горизонт."""

    refit_policy = "every_bar"

    def __init__(self, horizon: int = 1, k: int = 5):
        self.horizon = horizon
        self.k = k
        self.name = f"ma{k}_ret"
        self._mean_ret = 0.0

    def fit(self, close: pd.Series) -> "MovingAverageReturn":
        rets = close.pct_change().dropna().iloc[-self.k :]
        self._mean_ret = float(rets.mean()) if len(rets) else 0.0
        return self

    def predict(self) -> float:
        return self._mean_ret * self.horizon


class ARIMAReturn:
    """ARIMA(p,0,q) на ряде доходностей; сетка (p,q) по AIC; прогноз суммы на H шагов."""

    name = "arima"
    refit_policy = "periodic"  # тяжёлый фит: допустимо обновлять раз в N баров

    def __init__(self, horizon: int = 1, p_max: int = 2, q_max: int = 2, max_len: int = 1500):
        self.horizon = horizon
        self.p_max = p_max
        self.q_max = q_max
        self.max_len = max_len
        self.name = "arima"
        self._order = (1, 0, 0)
        self._params = None

    def fit(self, close: pd.Series) -> "ARIMAReturn":
        from statsmodels.tsa.arima.model import ARIMA

        rets = np.log(close).diff().dropna().iloc[-self.max_len :]
        if len(rets) < 30:
            self._order, self._params = (1, 0, 0), None
            return self

        best_aic, best_order, best_res = np.inf, (1, 0, 0), None
        for p in range(self.p_max + 1):
            for q in range(self.q_max + 1):
                if p == 0 and q == 0:
                    continue
                try:
                    with warnings.catch_warnings():
                        warnings.simplefilter("ignore")
                        res = ARIMA(
                            rets,
                            order=(p, 0, q),
                            enforce_stationarity=False,
                            enforce_invertibility=False,
                        ).fit(method_kwargs={"maxiter": 60})
                    if res.aic < best_aic:
                        best_aic, best_order, best_res = res.aic, (p, 0, q), res
                except Exception as exc:  # не роняем walk-forward на одной точке
                    log.debug("ARIMA(%d,%d) не сошлась: %s", p, q, exc)
        self._order = best_order
        self._res = best_res
        return self

    def predict(self) -> float:
        if getattr(self, "_res", None) is None:
            return 0.0
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                forecast = self._res.forecast(steps=self.horizon)
            return float(np.sum(np.asarray(forecast)))
        except Exception as exc:
            log.debug("ARIMA forecast упал: %s", exc)
            return 0.0


class ETSReturn:
    """Экспоненциальное сглаживание (Holt) на ценах; доходность = прогноз/последняя цена - 1."""

    name = "ets"
    refit_policy = "periodic"

    def __init__(self, horizon: int = 1):
        self.horizon = horizon
        self.name = "ets"
        self._last = 0.0
        self._forecast_last = 0.0

    def fit(self, close: pd.Series) -> "ETSReturn":
        from statsmodels.tsa.holtwinters import ExponentialSmoothing

        prices = close.astype(float).iloc[-1500:]
        self._last = float(prices.iloc[-1])
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                model = ExponentialSmoothing(
                    prices, trend="add", damped_trend=True, initialization_method="estimated"
                ).fit(optimized=True)
                self._forecast_last = float(model.forecast(self.horizon).iloc[-1])
        except Exception as exc:
            log.debug("ETS не сошлась: %s", exc)
            self._forecast_last = self._last
        return self

    def predict(self) -> float:
        if self._last <= 0:
            return 0.0
        return self._forecast_last / self._last - 1.0

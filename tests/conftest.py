"""Общие фикстуры тестов: синтетические OHLCV-данные."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest


def synthetic_candles(n: int = 600, seed: int = 7, freq: str = "h") -> pd.DataFrame:
    """Геометрическое случайное блуждание с трендовыми фазами и OHLC вокруг close."""
    rng = np.random.default_rng(seed)
    drift = np.where(np.arange(n) % 200 < 100, 0.0004, -0.0002)
    rets = drift + rng.normal(0, 0.012, n)
    close = 250.0 * np.exp(np.cumsum(rets))

    spread = np.abs(rng.normal(0, 0.004, n)) + 0.002
    high = close * (1 + spread)
    low = close * (1 - spread)
    open_ = np.roll(close, 1) * (1 + rng.normal(0, 0.002, n))
    open_[0] = close[0]
    volume = rng.integers(50_000, 500_000, n).astype(float)

    index = pd.date_range("2025-01-01", periods=n, freq=freq, tz="UTC")
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume}, index=index
    )


@pytest.fixture
def candles() -> pd.DataFrame:
    return synthetic_candles()

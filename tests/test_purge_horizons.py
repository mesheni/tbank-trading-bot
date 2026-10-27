"""Purging на разных горизонтах и sentinel выпадения строк в build_features."""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd
import pytest

from features import build_features, ensure_news_columns


# ---------- purging: хвост обучения не пересекается с тестом ни на одном горизонте ----------

@pytest.mark.parametrize("horizon", [1, 2, 4])
def test_walk_forward_lgbm_purges_horizon_overlap(candles, monkeypatch, horizon):
    import models.registry as reg

    fits: list[pd.DataFrame] = []

    class StubModel:
        model = object()  # предсказывает всегда

        def __init__(self, horizon, use_news_features=False):
            pass

        def fit(self, train):
            fits.append(train.copy())

        def predict_all(self, features):
            return [0.0] * len(features)

    monkeypatch.setattr(reg, "LGBMReturnModel", StubModel)
    features = ensure_news_columns(build_features(candles, horizon=horizon))
    test_points = features.index[-60:]
    reg.walk_forward_lgbm(features, test_points, horizon=horizon, refit_every=30, min_train=10)

    assert fits, "обучение должно было случиться"
    first_block_start = test_points[0]
    test_pos = features.index.get_loc(first_block_start)
    last_train_pos = features.index.get_loc(fits[0].index[-1])
    # метка последней обучающей строки использует close[t+horizon]:
    # t должен быть минимум на horizon+1 позиций раньше начала теста
    assert last_train_pos <= test_pos - (horizon + 1)


# ---------- sentinel: предупреждение при массовом выпадении строк ----------

def _flat_then_random_candles(n: int = 60, flat: int = 45) -> pd.DataFrame:
    """60 баров: длинный плоский участок даёт NaN в RSI и роняет большинство строк."""
    rng = np.random.default_rng(7)
    close = np.empty(n)
    close[:flat] = 250.0
    close[flat:] = 250.0 * np.exp(np.cumsum(rng.normal(0, 0.012, n - flat)))
    index = pd.date_range("2025-01-01", periods=n, freq="h", tz="UTC")
    return pd.DataFrame(
        {
            "open": close,
            "high": close * 1.001,
            "low": close * 0.999,
            "close": close,
            "volume": rng.integers(50_000, 500_000, n).astype(float),
        },
        index=index,
    )


def test_build_features_warns_when_most_rows_dropped(caplog):
    df = _flat_then_random_candles(n=60)
    with caplog.at_level(logging.WARNING, logger="features"):
        features = build_features(df, horizon=3)

    assert len(features) / len(df) < 0.5  # действительно выпало больше половины
    warnings = [r for r in caplog.records if r.name == "features"]
    assert warnings, "ожидалось предупреждение о массовом выпадении строк"
    assert "отброшено" in warnings[0].message


def test_build_features_no_warning_on_long_series(candles, caplog):
    with caplog.at_level(logging.WARNING, logger="features"):
        features = build_features(candles, horizon=3)

    assert not features.empty
    assert not [r for r in caplog.records if r.name == "features"]

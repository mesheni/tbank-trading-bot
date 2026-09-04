from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from features import (
    FEATURE_COLUMNS,
    NEWS_FEATURE_COLUMNS,
    TARGET,
    add_news_features,
    build_features,
)


def test_build_features_shapes_and_no_nan(candles):
    features = build_features(candles, horizon=3)
    assert len(features) > 400
    assert not features[FEATURE_COLUMNS].isna().any().any()
    for col in FEATURE_COLUMNS:
        assert col in features.columns, f"нет признака {col}"


def test_target_is_forward_return(candles):
    horizon = 3
    features = build_features(candles, horizon)
    close = candles["close"]
    for ts in features.index[:50]:
        expected = close.shift(-horizon).loc[ts] / close.loc[ts] - 1.0
        assert features[TARGET].loc[ts] == pytest.approx(expected, rel=1e-9)


def test_target_uses_future_not_past(candles):
    """Таргет строки t должен совпадать с фактической доходностью t -> t+H."""
    horizon = 5
    features = build_features(candles, horizon)
    close = candles["close"]
    ts = features.index[100]
    future_ts = features.index[100 + horizon] if 100 + horizon < len(features) else None
    if future_ts is not None:
        direct = close.loc[future_ts] / close.loc[ts] - 1.0
        assert features[TARGET].loc[ts] == pytest.approx(direct, rel=1e-9)


def test_add_news_features_weighted_sentiment(candles):
    features = build_features(candles, horizon=1)
    bar_time = features.index[300]

    news = pd.DataFrame(
        {
            "pub_dt": [bar_time - pd.Timedelta(hours=1), bar_time - pd.Timedelta(hours=2)],
            "sentiment": [1.0, -1.0],
        }
    )
    out = add_news_features(features.iloc[298:303], news)
    # обе новости свежие, sentiments усредняются к ~0
    row = out.loc[bar_time]
    assert row["news_sentiment_24h"] == pytest.approx(0.0, abs=0.15)
    assert row["news_count_24h"] == 2.0

    # только негативная старая новость — скор негативный
    old_bad = pd.DataFrame(
        {"pub_dt": [bar_time - pd.Timedelta(hours=20)], "sentiment": [-1.0]}
    )
    out2 = add_news_features(features.iloc[298:303], old_bad)
    assert out2.loc[bar_time, "news_sentiment_24h"] < 0

    # будущие новости не утекают в прошлое
    future = pd.DataFrame({"pub_dt": [bar_time + pd.Timedelta(hours=5)], "sentiment": [1.0]})
    out3 = add_news_features(features.iloc[298:303], future)
    assert out3.loc[bar_time, "news_sentiment_24h"] == 0.0
    assert out3.loc[bar_time, "news_count_24h"] == 0.0
    assert set(NEWS_FEATURE_COLUMNS).issubset(out3.columns)

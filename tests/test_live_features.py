"""Признаки для живого прогноза: последняя строка — последний бар, без дрейфа."""
from __future__ import annotations

import pandas as pd
import pytest

from features import FEATURE_COLUMNS, TARGET, build_features, build_live_features


@pytest.mark.parametrize("horizon", [1, 3])
def test_live_features_end_at_latest_bar(candles, horizon):
    live = build_live_features(candles, horizon)
    assert live.index[-1] == candles.index[-1]
    assert len(live) == len(build_features(candles, horizon)) + horizon


@pytest.mark.parametrize("horizon", [1, 3])
def test_build_features_still_drops_target_tail(candles, horizon):
    features = build_features(candles, horizon)
    assert features.index[-1] == candles.index[-horizon - 1]
    assert not features[TARGET].isna().any()

    live = build_live_features(candles, horizon)
    assert live[TARGET].iloc[-horizon:].isna().all()


@pytest.mark.parametrize("horizon", [1, 3])
def test_common_bars_have_identical_features(candles, horizon):
    features = build_features(candles, horizon)
    live = build_live_features(candles, horizon)

    # все строки build_features сохраняются в live-версии (удалён только хвост таргета)
    assert features.index.isin(live.index).all()
    pd.testing.assert_frame_equal(
        features.loc[features.index, FEATURE_COLUMNS],
        live.loc[features.index, FEATURE_COLUMNS],
    )


@pytest.mark.parametrize("n", [0, 39])
def test_too_short_input_raises_like_build_features(n):
    df = pd.DataFrame(
        {
            "open": [],
            "high": [],
            "low": [],
            "close": [],
            "volume": [],
        }
    )
    if n:
        idx = pd.date_range("2025-01-01", periods=n, freq="h", tz="UTC")
        df = pd.DataFrame(
            {
                "open": [100.0] * n,
                "high": [101.0] * n,
                "low": [99.0] * n,
                "close": [100.0] * n,
                "volume": [1000.0] * n,
            },
            index=idx,
        )

    with pytest.raises(ValueError) as live_error:
        build_live_features(df, 1)
    with pytest.raises(ValueError) as base_error:
        build_features(df, 1)
    assert str(live_error.value) == str(base_error.value)

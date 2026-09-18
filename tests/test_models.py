from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from features import TARGET, build_features
from models.baseline import ARIMAReturn, ETSReturn, MovingAverageReturn, NaiveZero, PersistenceReturn
from models.boosting import LGBMReturnModel
from models.registry import evaluate_all, train_and_save


def test_naive_zero():
    assert NaiveZero().fit(None).predict() == 0.0


def test_persistence_and_ma(candles):
    close = candles["close"]
    p = PersistenceReturn(horizon=3).fit(close)
    last_ret = close.pct_change().iloc[-1]
    assert p.predict() == pytest.approx(last_ret * 3, rel=1e-9)

    ma = MovingAverageReturn(horizon=2, k=5).fit(close)
    mean5 = close.pct_change().dropna().iloc[-5:].mean()
    assert ma.predict() == pytest.approx(mean5 * 2, rel=1e-9)


def test_arima_and_ets_small(candles):
    close = candles["close"].iloc[-400:]
    arima = ARIMAReturn(horizon=1, p_max=1, q_max=1).fit(close)
    r = arima.predict()
    assert np.isfinite(r) and abs(r) < 0.5

    ets = ETSReturn(horizon=4).fit(close)
    assert np.isfinite(ets.predict())


def test_arima_ets_forecast_with_microsecond_datetime_index(candles):
    # регрессия live-прогона 09-18.09.2026: pandas 2.x отдаёт из SQLite индекс
    # datetime64[us], statsmodels на нём молча валит forecast/fit («No supported
    # index is available») — ARIMA и ETS возвращали сплошные 0.0, у всех моделей
    # dir_acc становился NaN, а «нулевые» модели выигрывали отбор
    close = candles["close"].iloc[-400:].copy()
    close.index = close.index.astype("datetime64[us, UTC]")
    assert str(close.index.dtype) == "datetime64[us, UTC]"

    arima = ARIMAReturn(horizon=1, p_max=1, q_max=1).fit(close)
    assert arima.predict() != 0.0

    ets = ETSReturn(horizon=1).fit(close)
    assert ets.predict() != 0.0


def test_lgbm_fit_predict(candles):
    features = build_features(candles, horizon=1)
    for col in ("news_sentiment_24h", "news_sentiment_72h", "news_count_24h"):
        if col not in features:
            features[col] = 0.0
    model = LGBMReturnModel(horizon=1)
    train = features.iloc[:-50]
    model.fit(train)
    r = model.predict_row(features.iloc[-2])
    assert np.isfinite(r)

    preds = model.predict_all(features.iloc[-10:])
    assert len(preds) == 10 and np.isfinite(preds).all()


def test_evaluate_all_smoke(candles):
    # урезаем данные для скорости, но оставляем достаточно для обучения
    metrics, results = evaluate_all(candles.iloc[-500:], horizon=1, test_frac=0.2, refit_every=48)
    assert {"naive_zero", "persistence", "ma5_ret", "arima", "ets", "lgbm"} <= set(metrics.index)
    assert metrics["rmse"].notna().all()
    assert "strategy_sharpe" in metrics.columns
    for name, res in results.items():
        assert len(res.predictions) > 0
        assert np.isfinite(res.predictions.to_numpy()).all()


def test_naive_zero_dir_acc_is_nan_and_excluded_from_selection(candles):
    metrics, _ = evaluate_all(candles.iloc[-500:], horizon=1, test_frac=0.2, refit_every=48)
    # у naive_zero нет ненулевых прогнозов — направленность не определена
    assert pd.isna(metrics.loc["naive_zero", "directional_acc"])


def test_walk_forward_lgbm_block_equals_pointwise(candles):
    """Блочные предсказания lgbm идентичны поточечным при том же расписании реfitов."""
    from models.registry import walk_forward_lgbm

    features = build_features(candles, horizon=1)
    for col in ("news_sentiment_24h", "news_sentiment_72h", "news_count_24h"):
        features[col] = 0.0
    features = features.iloc[-300:]
    test_points = features.index[::4]

    blocked = walk_forward_lgbm(features, test_points, horizon=1, refit_every=3)

    # эталон: поточечно, refit на тех же границах блоков
    # (срез обучения повторяет purging из walk_forward_lgbm: horizon+1 строк с хвоста)
    from models.boosting import LGBMReturnModel

    model = LGBMReturnModel(horizon=1)
    test_list = list(test_points)
    for start in range(0, len(test_list), 3):
        block = test_list[start : start + 3]
        train = features.loc[: block[0]].iloc[: -(1 + 1)]
        if len(train) >= 200:
            model.fit(train)
        if model.model is not None:  # пока бустера нет — честный NaN, не 0.0
            for ts in block:
                assert model.predict_row(features.loc[ts]) == pytest.approx(blocked[ts], abs=1e-12)
        else:
            assert all(pd.isna(blocked[ts]) for ts in block)


def test_persistence_refits_every_bar(candles):
    """Persistence с per-bar переобучением даёт разные прогнозы на соседних барах."""
    from models.registry import walk_forward_baselines

    df = candles.iloc[-300:]
    features = build_features(df, horizon=1)
    test_points = features.index[-30:]
    preds = walk_forward_baselines(
        df["close"], features["target"], 1, lambda: PersistenceReturn(1), test_points, refit_every=1000
    )
    # при refit_every=1000 (фактически без периодического refit) прогнозы всё равно
    # обновляются каждый бар: значения не "заморожены"
    assert preds.nunique() > 3


def test_train_never_selects_naive_zero(tmp_path, candles):
    df = candles.iloc[-500:]
    artifact = train_and_save(
        df, horizon=1, artifacts_dir=tmp_path, ticker="TEST", interval="hour", cost_floor=0.0012
    )
    assert artifact.kind != "naive_zero"
    # порог не ниже издержек за круг
    assert artifact.threshold >= 0.0012

    from models.registry import load_artifact

    loaded = load_artifact(tmp_path, "TEST", "hour", 1)
    assert loaded.threshold == artifact.threshold


def _degenerate_metrics() -> pd.DataFrame:
    """Таблица как из live-прогона 09-18.09.2026: у arima/ets/naive_zero walk-forward
    прогнозы сплошные нули (dir_acc NaN, net sharpe ровно 0.0), у остальных —
    честный отрицательный net sharpe."""
    return pd.DataFrame(
        {
            "rmse": {"naive_zero": 0.006, "arima": 0.006, "ets": 0.006, "lgbm": 0.005},
            "mae": {"naive_zero": 0.004, "arima": 0.004, "ets": 0.004, "lgbm": 0.003},
            "directional_acc": {"naive_zero": np.nan, "arima": np.nan, "ets": np.nan, "lgbm": 0.51},
            "strategy_sharpe": {"naive_zero": 0.0, "arima": 0.0, "ets": 0.0, "lgbm": 0.2},
            "strategy_sharpe_net": {"naive_zero": 0.0, "arima": 0.0, "ets": 0.0, "lgbm": -0.3},
            "n_points": {"naive_zero": 100.0, "arima": 100.0, "ets": 100.0, "lgbm": 100.0},
        }
    )


def test_selection_ignores_zero_prediction_models_with_nan_dir_acc(tmp_path, monkeypatch, candles):
    # раньше «нулевой» arima выигрывал отбор (0.0 > -0.3) и гейтил всю торговлю
    import models.registry as registry

    metrics = _degenerate_metrics()
    zero_preds = pd.Series(0.0, index=candles.index[-100:])
    results = {
        name: registry.WalkForwardResult(zero_preds, zero_preds, metrics.loc[name].to_dict())
        for name in metrics.index
    }
    monkeypatch.setattr(registry, "evaluate_all", lambda *a, **k: (metrics, results))

    artifact = registry.train_and_save(candles.iloc[-500:], 1, tmp_path, "TEST", "hour")

    # выбирается единственная модель с измеренным направлением, не «нулевой» arima
    assert artifact.kind == "lgbm"


def test_train_raises_when_no_model_has_measured_direction(tmp_path, monkeypatch, candles):
    import models.registry as registry

    metrics = _degenerate_metrics()
    metrics.loc["lgbm", "directional_acc"] = np.nan  # и у lgbm направление не измерено
    zero_preds = pd.Series(0.0, index=candles.index[-100:])
    results = {
        name: registry.WalkForwardResult(zero_preds, zero_preds, metrics.loc[name].to_dict())
        for name in metrics.index
    }
    monkeypatch.setattr(registry, "evaluate_all", lambda *a, **k: (metrics, results))

    with pytest.raises(ValueError, match="directional_acc"):
        registry.train_and_save(candles.iloc[-500:], 1, tmp_path, "TEST", "hour")


def test_train_and_save(tmp_path, candles):
    df = candles.iloc[-500:]
    artifact = train_and_save(df, horizon=1, artifacts_dir=tmp_path, ticker="TEST", interval="hour")
    assert artifact.kind in {"naive_zero", "persistence", "ma5_ret", "arima", "ets", "lgbm"}
    files = list(tmp_path.glob("model_TEST_hour_h1.*"))
    assert files, "артефакт не сохранён"

    from models.registry import load_artifact

    loaded = load_artifact(tmp_path, "TEST", "hour", 1)
    assert loaded.kind == artifact.kind

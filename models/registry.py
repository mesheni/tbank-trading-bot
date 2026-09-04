"""Реестр моделей: walk-forward сравнение бейзлайнов и LightGBM, выбор лучшей.

Протокол: последние `test_frac` данных — тест; идём по тесту вперёд по времени,
модель переобучается раз в `refit_every` баров на всей истории до текущего бара
(expanding window), предсказывает доходность на горизонт H.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

import joblib
import numpy as np
import pandas as pd

from features import (
    FEATURE_COLUMNS,
    NEWS_FEATURE_COLUMNS,
    TARGET,
    build_features,
    ensure_news_columns,
)
from .baseline import ARIMAReturn, ETSReturn, MovingAverageReturn, NaiveZero, PersistenceReturn
from .boosting import LGBMReturnModel

log = logging.getLogger(__name__)

METRIC_KEYS = ("rmse", "mae", "directional_acc", "strategy_sharpe", "n_points")

# Модель «предсказывать ноль» не может дать торговый сигнал — из выбора лучшей исключается,
# но в таблице остаётся как эталон RMSE.
NON_SELECTABLE = ("naive_zero",)


@dataclass
class WalkForwardResult:
    predictions: pd.Series  # индекс = время бара, значение = прогноз доходности
    facts: pd.Series  # фактическая доходность за горизонт
    metrics: dict = field(default_factory=dict)


def _metrics(pred: np.ndarray, fact: np.ndarray, bars_per_year: float = 2100.0) -> dict:
    mask = ~np.isnan(fact)
    pred, fact = pred[mask], fact[mask]
    if len(pred) == 0:
        return {k: float("nan") for k in METRIC_KEYS}
    nonzero = pred != 0
    directional_acc = (
        float(np.mean(np.sign(pred[nonzero]) == np.sign(fact[nonzero])))
        if nonzero.any()
        else float("nan")
    )
    # экономическая ценность: доходность стратегии «идём в сторону прогноза» без порога и издержек
    strategy_ret = np.sign(pred) * fact
    std = strategy_ret.std()
    strategy_sharpe = float(strategy_ret.mean() / std * np.sqrt(bars_per_year)) if std > 1e-12 else 0.0
    return {
        "rmse": float(np.sqrt(np.mean((pred - fact) ** 2))),
        "mae": float(np.mean(np.abs(pred - fact))),
        "directional_acc": directional_acc,
        "strategy_sharpe": strategy_sharpe,
        "n_points": int(len(pred)),
    }


def walk_forward_baselines(
    close: pd.Series,
    targets: pd.Series,
    horizon: int,
    model_factory,
    test_points: pd.Index,
    refit_every: int,
) -> pd.Series:
    """Walk-forward прогон бейзлайна по тестовым точкам.

    Модели с refit_policy='every_bar' (persistence, MA) переобучаются на каждом баре —
    их прогноз зависит от последнего значения; 'periodic' (ARIMA, ETS) — раз в
    refit_every баров: фиты тяжёлые, а прогноз между переобучениями всё равно константный.
    """
    model = model_factory()
    every_bar = getattr(model, "refit_policy", "periodic") == "every_bar"
    close_index = close.index
    positions = close_index.get_indexer(test_points)
    preds = {}
    for i, pos in enumerate(positions):
        if every_bar or i % refit_every == 0:
            if every_bar:
                model = model_factory()
            model.fit(close.iloc[:pos])
        preds[test_points[i]] = model.predict()
    return pd.Series(preds, index=test_points, dtype=float)


def walk_forward_lgbm(
    features: pd.DataFrame,
    test_points: pd.Index,
    horizon: int,
    refit_every: int = 48,
    use_news: bool = False,
    min_train: int = 200,
) -> pd.Series:
    """Walk-forward предсказания LightGBM: переобучение только на прошлом (без утечки).

    Для скорости предсказания идут блоками по refit_every баров одним вызовом —
    внутри блока модель та же, как и при поточечном варианте, результат идентичен.
    Используется и в evaluate_all, и в cmd_backtest — финальная артефактная модель
    обучена на всей истории, поэтому для оценки на её собственных данных непригодна.
    """
    lgbm = LGBMReturnModel(horizon=horizon, use_news_features=use_news)
    preds = pd.Series(np.nan, index=test_points, dtype=float)
    test_list = list(test_points)
    for start in range(0, len(test_list), refit_every):
        block = test_list[start : start + refit_every]
        train = features.loc[: block[0]].iloc[:-1]
        if len(train) >= min_train:
            lgbm.fit(train)
        if lgbm.model is not None:
            values = lgbm.predict_all(features.loc[block])
            for ts, value in zip(block, values):
                preds[ts] = float(value)
    return preds


def evaluate_all(
    candles: pd.DataFrame,
    horizon: int,
    test_frac: float = 0.25,
    refit_every: int = 48,
    use_news: bool = False,
) -> tuple[pd.DataFrame, dict[str, WalkForwardResult]]:
    """Сравнивает все модели на одинаковых тестовых точках. Возвращает таблицу метрик."""
    features = ensure_news_columns(build_features(candles, horizon))

    n_test = max(30, int(len(features) * test_frac))
    test_points = features.index[-n_test:]
    facts = features[TARGET].reindex(test_points)
    close = candles["close"]

    results: dict[str, WalkForwardResult] = {}
    bars_per_year = _bars_per_year(candles)

    def register(name: str, preds: pd.Series):
        results[name] = WalkForwardResult(
            preds, facts, _metrics(preds.to_numpy(), facts.to_numpy(), bars_per_year)
        )

    register("naive_zero", walk_forward_baselines(close, facts, horizon, NaiveZero, test_points, 1))
    register(
        "persistence",
        walk_forward_baselines(close, facts, horizon, lambda: PersistenceReturn(horizon), test_points, 1),
    )
    register(
        "ma5_ret",
        walk_forward_baselines(close, facts, horizon, lambda: MovingAverageReturn(horizon, k=5), test_points, 1),
    )
    # тяжёлые модели с константным прогнозом переобучаются реже: 168 часов = неделя
    slow_refit = max(refit_every, 168)
    register(
        "arima",
        walk_forward_baselines(close, facts, horizon, lambda: ARIMAReturn(horizon), test_points, slow_refit),
    )
    register("ets", walk_forward_baselines(close, facts, horizon, lambda: ETSReturn(horizon), test_points, slow_refit))

    # LightGBM: walk-forward предсказания с переобучением раз в refit_every
    register("lgbm", walk_forward_lgbm(features, test_points, horizon, refit_every, use_news))

    metrics = pd.DataFrame({name: r.metrics for name, r in results.items()}).T
    metrics = metrics.sort_values("strategy_sharpe", ascending=False, na_position="last")
    return metrics, results


def _bars_per_year(candles: pd.DataFrame) -> float:
    """Число баров в году по фактическому шагу индекса (для годового Sharpe)."""
    if len(candles) < 2:
        return 2100.0
    seconds_per_bar = (candles.index[-1] - candles.index[0]).total_seconds() / (len(candles) - 1)
    if seconds_per_bar >= 20 * 3600:
        return 247.0
    if seconds_per_bar >= 3600:
        return 247.0 * 8.5
    return 247.0 * 8.5 * 3600 / max(seconds_per_bar, 1.0)


@dataclass
class ModelArtifact:
    """Артефакт лучшей модели: вид, гиперпараметры, обученный объект (для lgbm).

    threshold — рекомендованный порог входа: минимум, при котором |прогноз| стратегии
    имеет смысл (не ниже издержек за круг и не ниже типичного масштаба прогнозов модели).
    """

    kind: str
    horizon: int
    metrics: dict
    feature_cols: list[str] = field(default_factory=list)
    model: object | None = None  # LGBMReturnModel с обученным booster'ом
    interval: str = ""
    ticker: str = ""
    threshold: float = 0.0

    def to_meta(self) -> dict:
        return {
            "kind": self.kind,
            "horizon": self.horizon,
            "metrics": self.metrics,
            "feature_cols": self.feature_cols,
            "interval": self.interval,
            "ticker": self.ticker,
            "threshold": self.threshold,
        }


def train_and_save(
    candles: pd.DataFrame,
    horizon: int,
    artifacts_dir: Path,
    ticker: str,
    interval: str,
    test_frac: float = 0.25,
    cost_floor: float = 0.0012,
) -> ModelArtifact:
    """cost_floor — издержки за круг сделки (комиссия + проскальзывание ×2), доли."""
    metrics, results = evaluate_all(candles, horizon, test_frac=test_frac)

    selectable = metrics.drop(index=[m for m in NON_SELECTABLE if m in metrics.index])
    best_name = selectable["strategy_sharpe"].idxmax()
    best_sharpe = float(selectable.loc[best_name, "strategy_sharpe"])
    log.info(
        "Лучшая модель: %s (strategy_sharpe=%.2f, dir_acc=%s, rmse=%.5f)",
        best_name,
        best_sharpe,
        "n/a" if pd.isna(selectable.loc[best_name, "directional_acc"]) else f"{selectable.loc[best_name, 'directional_acc']:.3f}",
        selectable.loc[best_name, "rmse"],
    )
    if best_sharpe <= 0:
        log.warning(
            "ВНИМАНИЕ: ни одна модель не показала положительной доходности на тестовом периоде "
            "(лучший strategy_sharpe=%.2f). Бот будет торговать редко или не торговать — это "
            "защитное поведение, а не ошибка. Попробуйте другой таймфрейм/горизонт или "
            "дополнительные признаки.", best_sharpe,
        )

    # самокалибровка порога входа: не ниже издержек и не ниже типичного масштаба прогнозов
    best_preds = results[best_name].predictions.abs()
    p75 = float(best_preds.quantile(0.75)) if len(best_preds) else 0.0
    threshold = max(cost_floor, p75)
    log.info(
        "Порог входа: max(издержки %.4f, 75%% |прогнозов| %.4f) = %.4f. "
        "Итоговый порог в боте = max(MIN_ABS_RETURN из .env, этот порог).",
        cost_floor, p75, threshold,
    )

    features = ensure_news_columns(build_features(candles, horizon))

    artifact = ModelArtifact(
        kind=best_name,
        horizon=horizon,
        metrics=metrics.loc[best_name].to_dict(),
        interval=interval,
        ticker=ticker,
        threshold=threshold,
    )

    if best_name == "lgbm":
        model = LGBMReturnModel(horizon=horizon)
        model.fit(features)
        artifact.model = model
        artifact.feature_cols = model.feature_cols

    artifacts_dir.mkdir(parents=True, exist_ok=True)
    joblib.dump(artifact, artifacts_dir / f"model_{ticker}_{interval}_h{horizon}.joblib")
    (artifacts_dir / f"metrics_{ticker}_{interval}_h{horizon}.json").write_text(
        json.dumps(
            {
                "metrics": json.loads(metrics.to_json()),
                "best": best_name,
                "threshold": threshold,
                "cost_floor": cost_floor,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return artifact


def load_artifact(artifacts_dir: Path, ticker: str, interval: str, horizon: int) -> ModelArtifact:
    path = artifacts_dir / f"model_{ticker}_{interval}_h{horizon}.joblib"
    if not path.exists():
        raise FileNotFoundError(
            f"Артефакт модели не найден: {path}. Сначала выполните команду train."
        )
    return joblib.load(path)

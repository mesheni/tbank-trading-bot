"""Инженерия признаков для прогноза доходности на H баров вперёд.

Вход: DataFrame OHLCV с DatetimeIndex (см. tbank.market_data.load_candles).
Выход: тот же DataFrame + признаки + колонка `target` (forward return на H баров).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

ROLL_WINDOWS = (5, 10, 20)
RSI_PERIOD = 14
MACD_FAST, MACD_SLOW, MACD_SIGNAL = 12, 26, 9
ATR_PERIOD = 14
RETURN_LAGS = (1, 2, 3, 5, 10)
TARGET = "target"


def _rsi(close: pd.Series, period: int = RSI_PERIOD) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    return 100.0 - 100.0 / (1.0 + rs)


def _atr(df: pd.DataFrame, period: int = ATR_PERIOD) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()


def build_features(df: pd.DataFrame, horizon: int) -> pd.DataFrame:
    """Добавляет признаки и таргет; строки с прогревочными NaN удаляются."""
    if df.empty or len(df) < 40:
        raise ValueError(f"Слишком мало свечей ({len(df)}), нужно >= 40")

    out = df.copy()
    close = out["close"]

    out["log_ret"] = np.log(close).diff()
    out["ret"] = close.pct_change()
    for lag in RETURN_LAGS:
        out[f"ret_lag_{lag}"] = out["ret"].shift(lag)

    for window in ROLL_WINDOWS:
        roll = out["ret"].rolling(window)
        out[f"ret_mean_{window}"] = roll.mean()
        out[f"ret_std_{window}"] = roll.std()
        out[f"ret_skew_{window}"] = roll.skew()

    sma20 = close.rolling(20).mean()
    std20 = close.rolling(20).std()
    out["close_sma20_ratio"] = close / sma20 - 1.0
    out["bb_pos"] = (close - (close.rolling(20).mean() - 2 * std20)) / (
        (close.rolling(20).mean() + 2 * std20) - (close.rolling(20).mean() - 2 * std20)
    )

    out["rsi"] = _rsi(close)

    ema_fast = close.ewm(span=MACD_FAST, adjust=False).mean()
    ema_slow = close.ewm(span=MACD_SLOW, adjust=False).mean()
    macd = ema_fast - ema_slow
    signal = macd.ewm(span=MACD_SIGNAL, adjust=False).mean()
    out["macd_hist_norm"] = (macd - signal) / close

    out["atr_norm"] = _atr(out) / close

    out["log_volume"] = np.log1p(out["volume"].astype(float))
    out["volume_ratio_20"] = out["volume"] / out["volume"].rolling(20).mean()

    idx = out.index
    if isinstance(idx, pd.DatetimeIndex):
        out["dow"] = idx.dayofweek
        hour = getattr(idx, "hour", None)
        if hour is not None and int(pd.Series(hour).nunique()) > 1:
            out["hour_sin"] = np.sin(2 * np.pi * idx.hour / 24.0)
            out["hour_cos"] = np.cos(2 * np.pi * idx.hour / 24.0)

    out[TARGET] = close.shift(-horizon) / close - 1.0
    out = out.replace([np.inf, -np.inf], np.nan)
    out = out.dropna()
    return out


FEATURE_COLUMNS = [
    c
    for c in [
        "log_ret",
        *[f"ret_lag_{l}" for l in RETURN_LAGS],
        *[f"ret_mean_{w}" for w in ROLL_WINDOWS],
        *[f"ret_std_{w}" for w in ROLL_WINDOWS],
        *[f"ret_skew_{w}" for w in ROLL_WINDOWS],
        "close_sma20_ratio",
        "bb_pos",
        "rsi",
        "macd_hist_norm",
        "atr_norm",
        "log_volume",
        "volume_ratio_20",
        "dow",
        "hour_sin",
        "hour_cos",
    ]
]

# Новостные признаки, которые стратегия/бот добавляют поверх базовых
NEWS_FEATURE_COLUMNS = ["news_sentiment_24h", "news_sentiment_72h", "news_count_24h"]


def ensure_news_columns(features: pd.DataFrame) -> pd.DataFrame:
    """Гарантирует наличие новостных колонок (нулями), если реальные скоры не подставлены."""
    out = features.copy()
    for col in NEWS_FEATURE_COLUMNS:
        if col not in out.columns:
            out[col] = 0.0
    return out


def add_news_features(features: pd.DataFrame, news_scores: pd.DataFrame) -> pd.DataFrame:
    """Присоединяет агрегированные новостные скоры.

    news_scores: DataFrame с колонками [pub_dt, sentiment].
    Для каждой свечи берутся новости, вышедшие до её времени:
    экспоненциально взвешенная тональность за 24ч и 72ч + число новостей за 24ч.
    """
    out = features.copy()
    for col in NEWS_FEATURE_COLUMNS:
        out[col] = 0.0
    if news_scores is None or news_scores.empty:
        return out

    scores = news_scores.dropna(subset=["pub_dt"]).sort_values("pub_dt")
    if scores.empty:
        return out

    t_ns = scores["pub_dt"].to_numpy(dtype="datetime64[ns]").astype(np.int64)
    sentiments = scores["sentiment"].to_numpy(dtype=float)
    bar_ns = np.asarray(
        [ts.value if hasattr(ts, "value") else np.datetime64(ts, "ns").astype(np.int64) for ts in out.index],
        dtype=np.int64,
    )

    for hours, sent_col, count_col in (
        (24, "news_sentiment_24h", "news_count_24h"),
        (72, "news_sentiment_72h", None),
    ):
        for i, bar in enumerate(bar_ns):
            lo = np.searchsorted(t_ns, bar - int(pd.Timedelta(hours=hours).value), side="left")
            hi = np.searchsorted(t_ns, bar, side="right")
            if hi <= lo:
                continue
            ages_h = (bar - t_ns[lo:hi]) / 3_600_000_000_000
            # окно в 3 полураспада внутри агрегата (half-life = hours/3)
            weights = 0.5 ** (ages_h / (hours / 3.0))
            out.iloc[i, out.columns.get_loc(sent_col)] = float(np.average(sentiments[lo:hi], weights=weights))
            if count_col is not None and hours == 24:
                out.iloc[i, out.columns.get_loc(count_col)] = float(hi - lo)
    return out

"""Агрегатор новостной повестки по инструменту.

Вход — нормализованные новости (см. tbank.market_data.load_news) со скорами
тональности; выход — агрегированное состояние повестки: взвешенный по свежести
тональный скор, интенсивность (число новостей), импульс, доминирующие темы
(кластеризация эмбеддингов, если эмбеддер доступен).
"""
from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)


@dataclass
class AgendaScore:
    sentiment: float = 0.0  # [-1, 1], взвешенный по свежести
    n_items: int = 0  # число новостей в окне
    momentum: float = 0.0  # sentiment(свежая половина) - sentiment(старая половина)
    topics: list[str] = field(default_factory=list)  # заголовки-представители тем


def score_agenda(
    news_df: pd.DataFrame,
    sentiment_by_id: dict[str, float],
    now: pd.Timestamp,
    window_hours: float = 48.0,
    half_life_hours: float = 24.0,
    embedder=None,
    max_topics: int = 3,
) -> AgendaScore:
    """Собирает повестку по одному инструменту за окно `window_hours` от `now`."""
    result = AgendaScore()
    if news_df is None or news_df.empty or "pub_dt" not in news_df:
        return result

    df = news_df.dropna(subset=["pub_dt"]).copy()
    cutoff = now - pd.Timedelta(hours=window_hours)
    df = df[(df["pub_dt"] <= now) & (df["pub_dt"] >= cutoff)]
    if df.empty:
        return result

    df["sentiment"] = df["news_id"].map(sentiment_by_id).fillna(0.0)
    ages_h = (now - df["pub_dt"]).dt.total_seconds() / 3600.0
    weights = 0.5 ** (ages_h / max(1e-9, half_life_hours))
    # макро-новости (без привязки к инструменту) весят вдвое меньше — они шумят для конкретного тикера
    if "instrument_uid" in df.columns:
        macro = (df["instrument_uid"] == "").to_numpy()
        weights = weights * np.where(macro, 0.5, 1.0)
    result.sentiment = float(np.average(df["sentiment"].to_numpy(), weights=weights.to_numpy()))
    result.n_items = int(len(df))

    # импульс: сравниваем свежую и старую половины окна
    mid = now - pd.Timedelta(hours=window_hours / 2.0)
    fresh = df[df["pub_dt"] >= mid]
    old = df[df["pub_dt"] < mid]
    if len(fresh) and len(old):
        result.momentum = float(fresh["sentiment"].mean() - old["sentiment"].mean())

    result.topics = _extract_topics(df, embedder, max_topics)
    return result


def _extract_topics(df: pd.DataFrame, embedder, max_topics: int) -> list[str]:
    titles = df["title"].astype(str).tolist()
    if len(titles) <= max_topics:
        return titles

    embeddings = embedder.encode(titles) if embedder is not None else None
    if embeddings is not None and len(set(map(str, df.index))) >= max_topics:
        try:
            from sklearn.cluster import KMeans

            k = min(max_topics, len(titles) - 1)
            labels = KMeans(n_clusters=k, n_init=5, random_state=42).fit_predict(embeddings)
            topics = []
            for cluster in range(k):
                members = np.where(labels == cluster)[0]
                if len(members) == 0:
                    continue
                # представитель — ближайший к центроиду
                centroid = embeddings[members].mean(axis=0)
                best = members[np.argmax(embeddings[members] @ centroid)]
                topics.append(titles[int(best)])
            return topics
        except Exception as exc:
            log.debug("Кластеризация тем не удалась: %s", exc)

    # фолбэк: самые частые значимые слова заголовков
    words = Counter()
    for title in titles:
        for token in str(title).lower().split():
            token = token.strip(".,!?«»—:;()\"'")
            if len(token) >= 5 and token.isalpha():
                words[token] += 1
    return [w for w, _ in words.most_common(max_topics)]


def batch_score_news(news_df: pd.DataFrame, scorer) -> dict[str, float]:
    """Проставляет скор тональности каждой новости: news_id -> [-1, 1]."""
    if news_df is None or news_df.empty:
        return {}
    out: dict[str, float] = {}
    for _, row in news_df.iterrows():
        text = " ".join(x for x in (str(row.get("title", "")), str(row.get("text", ""))) if x != "nan")
        out[str(row["news_id"])] = float(scorer.score(text))
    return out

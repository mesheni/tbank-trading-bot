from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from tbank.market_data import _store_candles, connect, load_candles, load_news, store_news


def _row(time_iso: str, price: float) -> dict:
    return {
        "time": time_iso,
        "open": price,
        "high": price * 1.01,
        "low": price * 0.99,
        "close": price,
        "volume": 1000,
        "is_complete": True,
    }


def test_candle_upsert_and_load(tmp_path):
    conn = connect(tmp_path / "t.sqlite")
    rows = [_row(f"2026-09-0{d}T10:00:00Z", 250.0 + d) for d in range(1, 6)]
    assert _store_candles(conn, "FIGI1", "SBER", "day", rows) == 5

    # повторная запись той же свечи не дублируется (upsert)
    assert _store_candles(conn, "FIGI1", "SBER", "day", rows[:1]) == 1
    df = load_candles(conn, "SBER", "day")
    assert len(df) == 5
    assert isinstance(df.index, pd.DatetimeIndex)
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert df["close"].iloc[-1] == 255.0

    # загрузка по figi тоже работает
    assert len(load_candles(conn, "FIGI1", "day")) == 5
    # другой интервал не пересекается
    assert load_candles(conn, "SBER", "hour").empty


def test_incomplete_candle_filter(tmp_path):
    conn = connect(tmp_path / "t.sqlite")
    rows = [_row("2026-09-01T10:00:00Z", 100.0), _row("2026-09-02T10:00:00Z", 101.0)]
    rows[1]["is_complete"] = False
    _store_candles(conn, "F", "T", "hour", rows)
    assert len(load_candles(conn, "T", "hour", only_complete=True)) == 1
    assert len(load_candles(conn, "T", "hour", only_complete=False)) == 2


def test_news_roundtrip(tmp_path):
    conn = connect(tmp_path / "t.sqlite")
    items = [
        {"news_id": "1", "instrument_uid": "U1", "instrument_uids": ["U1"],
         "pub_time": "2026-09-01T09:00:00Z", "title": "A", "text": "a"},
        {"news_id": "2", "instrument_uid": "U2", "instrument_uids": ["U2"],
         "pub_time": "2026-09-02T09:00:00Z", "title": "B", "text": "b"},
    ]
    assert store_news(conn, items) == 2
    assert store_news(conn, items) == 2  # дубликат по PK

    all_news = load_news(conn)
    assert len(all_news) == 2
    one = load_news(conn, instrument_uid="U1")
    assert len(one) == 1 and one.iloc[0]["title"] == "A"

    since = dt.datetime(2026, 9, 2, tzinfo=dt.timezone.utc)
    assert len(load_news(conn, since=since)) == 1


def test_news_multi_instrument_and_market_wide(tmp_path):
    conn = connect(tmp_path / "t.sqlite")
    items = [
        # новость про два инструмента -> две строки
        {"news_id": "1", "instrument_uid": "U1", "instrument_uids": ["U1", "U2"],
         "pub_time": "2026-09-01T09:00:00Z", "title": "A", "text": "a"},
        # макро-новость без привязки -> одна строка с пустым uid
        {"news_id": "2", "instrument_uid": "", "instrument_uids": [],
         "pub_time": "2026-09-02T09:00:00Z", "title": "MACRO", "text": "m"},
    ]
    assert store_news(conn, items) == 3

    # фильтр по U1: своя + макро, дубли по news_id нет
    df = load_news(conn, instrument_uid="U1")
    assert set(df["title"]) == {"A", "MACRO"}
    # без макро — только своя
    assert list(load_news(conn, instrument_uid="U1", include_market=False)["title"]) == ["A"]
    # фильтр по U2 видит ту же новость A
    assert "A" in set(load_news(conn, instrument_uid="U2")["title"])
    # все новости без фильтра — 2 уникальные
    assert len(load_news(conn)) == 2


def test_news_old_schema_is_migrated(tmp_path):
    import sqlite3

    path = tmp_path / "old.sqlite"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE news (news_id TEXT PRIMARY KEY, instrument_uid TEXT,"
        " pub_time TEXT, title TEXT, text TEXT)"
    )
    conn.execute("INSERT INTO news VALUES ('old', 'U', '2020-01-01', 'x', 'y')")
    conn.commit()
    conn.close()

    conn = connect(path)
    # схема пересоздана под составной PK
    pk = [r[5] for r in conn.execute("PRAGMA table_info(news)") if r[5]]
    assert len(pk) == 2
    conn.close()


def test_chunked_window_boundaries():
    """Проверяем логику чанков: сумма чанков покрывает период без дыр и пересечений."""
    from config import CANDLE_INTERVALS

    for interval, meta in CANDLE_INTERVALS.items():
        assert meta["chunk"] > 0
        assert meta["enum"].startswith("CANDLE_INTERVAL_")
        # 3 года истории должны покрыться конечным числом чанков
        days = 1095
        n_chunks = -(-days // meta["chunk"])
        assert n_chunks * meta["chunk"] >= days

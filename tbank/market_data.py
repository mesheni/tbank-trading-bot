"""Слой данных: свечи и новости в SQLite (учёт лимитов API через чанкирование)."""
from __future__ import annotations

import datetime as dt
import logging
import sqlite3
import time
from pathlib import Path

import pandas as pd

from config import CANDLE_INTERVALS
from .api import TBankAPI

log = logging.getLogger(__name__)


def connect(db_path) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS candles (
            figi TEXT NOT NULL,
            ticker TEXT NOT NULL,
            interval TEXT NOT NULL,
            time TEXT NOT NULL,
            open REAL, high REAL, low REAL, close REAL,
            volume INTEGER,
            is_complete INTEGER DEFAULT 1,
            PRIMARY KEY (figi, interval, time)
        )
        """
    )
    # миграция для старых БД без колонки ticker
    columns = {row[1] for row in conn.execute("PRAGMA table_info(candles)")}
    if "ticker" not in columns:
        conn.execute("ALTER TABLE candles ADD COLUMN ticker TEXT NOT NULL DEFAULT ''")
    conn.commit()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS news (
            news_id TEXT NOT NULL,
            instrument_uid TEXT NOT NULL,
            pub_time TEXT,
            title TEXT,
            text TEXT,
            PRIMARY KEY (news_id, instrument_uid)
        )
        """
    )
    # миграция старой схемы (PK только по news_id): новости переизвлекаемы — пересоздаём
    news_pk = [row[5] for row in conn.execute("PRAGMA table_info(news)") if row[5]]
    if len(news_pk) != 2:
        conn.execute("DROP TABLE news")
        conn.execute(
            """
            CREATE TABLE news (
                news_id TEXT NOT NULL,
                instrument_uid TEXT NOT NULL,
                pub_time TEXT,
                title TEXT,
                text TEXT,
                PRIMARY KEY (news_id, instrument_uid)
            )
            """
        )
    return conn


# ---------- Свечи ----------

def download_candles(
    api: TBankAPI,
    conn: sqlite3.Connection,
    figi: str,
    interval: str,
    days_back: int,
    now: dt.datetime | None = None,
    ticker: str = "",
) -> int:
    """Выгружает историю свечей чанками (лимит глубины на один запрос API) и апсертит в БД.

    Возвращает число сохранённых свечей.
    """
    if interval not in CANDLE_INTERVALS:
        raise ValueError(f"Интервал {interval!r} не поддерживается: {list(CANDLE_INTERVALS)}")
    meta = CANDLE_INTERVALS[interval]
    chunk_days = meta["chunk"]
    now = now or dt.datetime.now(dt.timezone.utc)
    start = now - dt.timedelta(days=days_back)

    total = 0
    cursor = start
    while cursor < now:
        chunk_end = min(cursor + dt.timedelta(days=chunk_days), now)
        rows = api.get_candles(figi, meta["enum"], cursor, chunk_end)
        if rows:
            total += _store_candles(conn, figi, ticker or figi, interval, rows)
        cursor = chunk_end
        time.sleep(0.05)
    log.info("%s %s: выгружено %d свечей за %d дней", ticker or figi, interval, total, days_back)
    return total


def _store_candles(conn: sqlite3.Connection, figi: str, ticker: str, interval: str, rows: list[dict]) -> int:
    payload = [
        (
            figi,
            ticker,
            interval,
            r["time"],
            r["open"],
            r["high"],
            r["low"],
            r["close"],
            r["volume"],
            int(bool(r.get("is_complete", True))),
        )
        for r in rows
    ]
    conn.executemany(
        "INSERT OR REPLACE INTO candles (figi, ticker, interval, time, open, high, low, close, volume, is_complete)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        payload,
    )
    conn.commit()
    return len(payload)


def load_candles(conn: sqlite3.Connection, key: str, interval: str, only_complete: bool = True) -> pd.DataFrame:
    """Свечи -> DataFrame с DatetimeIndex (UTC). `key` — тикер или figi."""
    query = "SELECT time, open, high, low, close, volume FROM candles WHERE (ticker = ? OR figi = ?) AND interval = ?"
    if only_complete:
        query += " AND is_complete = 1"
    df = pd.read_sql_query(query + " ORDER BY time", conn, params=(key, key, interval))
    if df.empty:
        return df
    df["time"] = pd.to_datetime(df["time"], utc=True, format="mixed")
    df = df.set_index("time")
    return df


# ---------- Новости ----------

def store_news(conn: sqlite3.Connection, items: list[dict]) -> int:
    """Сохраняет новости. Новость с несколькими инструментами даёт по строке на каждый;
    новость без привязки (макро) хранится один раз с instrument_uid=''.
    Возвращает число записанных строк."""
    payload = []
    for i in items:
        if not i.get("news_id"):
            continue
        uids = i.get("instrument_uids") or [i.get("instrument_uid") or ""]
        for uid in uids:
            payload.append((i["news_id"], uid, i.get("pub_time"), i.get("title"), i.get("text")))
    conn.executemany(
        "INSERT OR REPLACE INTO news (news_id, instrument_uid, pub_time, title, text) VALUES (?, ?, ?, ?, ?)",
        payload,
    )
    conn.commit()
    return len(payload)


def load_news(
    conn: sqlite3.Connection,
    instrument_uid: str | None = None,
    since: dt.datetime | None = None,
    include_market: bool = True,
) -> pd.DataFrame:
    """Новости -> DataFrame. При фильтре по инструменту также включает макро-новости
    (instrument_uid=''), если include_market=True; дубли по news_id убираются."""
    query = "SELECT news_id, instrument_uid, pub_time, title, text FROM news"
    params: list = []
    if instrument_uid:
        if include_market:
            query += " WHERE instrument_uid = ? OR instrument_uid = ''"
            params = [instrument_uid]
        else:
            query += " WHERE instrument_uid = ?"
            params = [instrument_uid]
    query += " ORDER BY pub_time DESC"
    df = pd.read_sql_query(query, conn, params=params)
    if not df.empty:
        df = df.drop_duplicates(subset="news_id")
        df["pub_dt"] = pd.to_datetime(df["pub_time"], utc=True, format="mixed", errors="coerce")
        if since is not None:
            df = df[df["pub_dt"] >= since]
    return df

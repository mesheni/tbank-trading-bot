"""Ретраи _store_candles при SQLite «database is locked»."""
from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

import tbank.market_data as md


def _row() -> dict:
    return {
        "time": "2026-09-01T10:00:00Z",
        "open": 1.0,
        "high": 1.0,
        "low": 1.0,
        "close": 1.0,
        "volume": 1,
        "is_complete": True,
    }


class FlakyConn:
    def __init__(self, failures: int, message: str = "database is locked"):
        self.failures = failures
        self.message = message
        self.calls = 0
        self.commits = 0

    def executemany(self, sql, payload):
        self.calls += 1
        if self.calls <= self.failures:
            raise sqlite3.OperationalError(self.message)

    def commit(self) -> None:
        self.commits += 1


@pytest.fixture
def fake_sleep(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(md, "time", SimpleNamespace(sleep=sleeps.append))
    return sleeps


def test_store_candles_retries_locked_then_succeeds(fake_sleep):
    conn = FlakyConn(failures=2)
    assert md._store_candles(conn, "F", "T", "day", [_row()]) == 1
    assert conn.calls == 3
    assert conn.commits == 1
    assert fake_sleep == [0.5, 1.0]


def test_store_candles_raises_after_three_locked_attempts(fake_sleep):
    conn = FlakyConn(failures=99)
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        md._store_candles(conn, "F", "T", "day", [_row()])
    assert conn.calls == 3
    assert conn.commits == 0
    assert fake_sleep == [0.5, 1.0]


def test_store_candles_does_not_retry_other_operational_errors(fake_sleep):
    conn = FlakyConn(failures=99, message="disk I/O error")
    with pytest.raises(sqlite3.OperationalError, match="disk I/O error"):
        md._store_candles(conn, "F", "T", "day", [_row()])
    assert conn.calls == 1
    assert fake_sleep == []

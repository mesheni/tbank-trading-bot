"""smoke: недоступность новостей — частичный успех, а не «пройдено»."""
from __future__ import annotations

from types import SimpleNamespace

import pandas as pd

import cli


class _StubAPI:
    def __init__(self, news_error: Exception | None = None) -> None:
        self.news_error = news_error

    def get_accounts(self) -> list[dict]:
        return [{"id": "acc-1", "type": "sandbox"}]

    def resolve_instruments(self, tickers: list[str]) -> dict:
        return {tickers[0]: {"figi": "FIGI-SBER", "uid": "uid-sber"}}

    def get_news(self, max_pages: int = 1) -> list[dict]:
        if self.news_error:
            raise self.news_error
        return [
            {
                "pub_time": "2026-01-01T10:00:00+00:00",
                "title": "SBER news",
                "instrument_uids": ["uid-sber"],
            }
        ]


class _StubConn:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _run_smoke(monkeypatch, api: _StubAPI) -> tuple[int, _StubConn]:
    conn = _StubConn()
    monkeypatch.setattr(cli, "make_api", lambda config: api)
    monkeypatch.setattr(cli, "connect", lambda db_path: conn)
    monkeypatch.setattr(cli, "download_candles", lambda *args, **kwargs: 0)
    monkeypatch.setattr(cli, "load_candles", lambda *args, **kwargs: pd.DataFrame())
    config = SimpleNamespace(mode="sandbox", tickers=["SBER"], db_path="unused.sqlite")
    return cli.cmd_smoke(config), conn


def test_smoke_reports_partial_failure_when_news_unavailable(monkeypatch, capsys) -> None:
    rc, conn = _run_smoke(monkeypatch, _StubAPI(news_error=RuntimeError("news down")))

    captured = capsys.readouterr()
    assert rc == 1
    assert "частично" in captured.err
    assert "Smoke-тест пройден частично: новости недоступны" in captured.err
    assert conn.closed is True


def test_smoke_returns_zero_when_everything_passed(monkeypatch, capsys) -> None:
    rc, conn = _run_smoke(monkeypatch, _StubAPI())

    captured = capsys.readouterr()
    assert rc == 0
    assert "Smoke-тест пройден" in captured.out
    assert "частично" not in captured.out
    assert conn.closed is True

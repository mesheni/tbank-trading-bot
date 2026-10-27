"""Тесты на пробелы из AUDIT_REPORT: STARTTLS, цвета ui, дайджест, download, report.

Всё офлайн: SMTP, API и файлы подменяются через monkeypatch, сети нет.
"""
from __future__ import annotations

import io
from types import SimpleNamespace

import pandas as pd
import pytest

import cli
import ui
from notify import Notifier, SmtpConfig


# ---------- STARTTLS (порт 587) ----------

class FakeSMTP:
    instances: list["FakeSMTP"] = []

    def __init__(self, host, port, timeout=None):
        self.host, self.port = host, port
        self.calls: list[str] = []
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self):
        self.calls.append("starttls")

    def login(self, user, password):
        self.calls.append("login")

    def send_message(self, msg):
        self.calls.append("send")


def test_send_uses_starttls_on_port_587(monkeypatch):
    FakeSMTP.instances.clear()
    monkeypatch.setattr("notify.smtplib.SMTP", FakeSMTP)
    monkeypatch.setattr("notify.smtplib.SMTP_SSL", lambda *a, **k: pytest.fail("SSL не должен использоваться"))
    cfg = SmtpConfig(host="smtp.example", port=587, user="u", password="p", to_addr="t@example.com")

    assert Notifier(cfg).send("тема", "тело") is True

    smtp = FakeSMTP.instances[-1]
    assert smtp.port == 587
    assert smtp.calls == ["starttls", "login", "send"]


# ---------- цвета ui ----------

class _Stream(io.StringIO):
    def __init__(self, tty: bool):
        super().__init__()
        self._tty = tty

    def isatty(self):
        return self._tty


def test_detect_no_color_wins_over_force(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("FORCE_COLOR", "1")
    assert ui._detect(_Stream(tty=True)) is False


def test_detect_force_color_enables_on_non_tty(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("FORCE_COLOR", "1")
    assert ui._detect(_Stream(tty=False)) is True


def test_detect_disables_for_redirected_output(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    assert ui._detect(_Stream(tty=False)) is False


# ---------- дайджест: дата в теме и строка покрытия ----------

class FakeDigestAPI:
    def get_accounts(self):
        return [{"id": "acc-1"}]

    def get_portfolio(self, account_id):
        return {"total_amount_rub": 1_050_000.0, "cash_rub": 50_000.0, "positions": {}}


def _digest_config(reports_dir) -> SimpleNamespace:
    return SimpleNamespace(
        mode="sandbox",
        reports_dir=reports_dir,
        tickers=["SBER"],
        account_id="",
        sandbox_initial_rub=1_000_000.0,
        account_budget_rub=None,
    )


def test_digest_reports_coverage_from_first_equity_point(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "make_api", lambda config: FakeDigestAPI())
    equity = pd.DataFrame(
        {
            "time": ["2026-10-01T10:00:00+00:00", "2026-10-11T10:00:00+00:00"],
            "total_rub": [1_000_000.0, 1_050_000.0],
            "cash_rub": [1_000_000.0, 50_000.0],
            "positions_rub": [0.0, 1_000_000.0],
        }
    )
    equity.to_csv(tmp_path / "equity_live.csv", index=False)

    body = cli.compose_digest(_digest_config(tmp_path))

    assert "Данные за 10 дн. (с 01.10.2026)" in body


def test_digest_omits_coverage_without_equity_file(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "make_api", lambda config: FakeDigestAPI())

    body = cli.compose_digest(_digest_config(tmp_path))

    assert "Данные за" not in body


def test_cmd_digest_subject_contains_utc_date(tmp_path, monkeypatch, capsys):
    import datetime as dt

    monkeypatch.setattr(cli, "make_api", lambda config: FakeDigestAPI())
    sent: list[str] = []

    def fake_send(self, subject, body):
        sent.append(subject)
        return True

    monkeypatch.setattr(Notifier, "send", fake_send)
    monkeypatch.setattr(Notifier, "enabled", property(lambda self: True))

    assert cli.cmd_digest(_digest_config(tmp_path)) == 0

    today = dt.datetime.now(dt.timezone.utc).date()
    assert sent == [f"дайджест {today:%Y-%m-%d}"]


# ---------- download: прогресс [i/N] ----------

class FakeDownloadAPI:
    def resolve_instruments(self, tickers):
        return {t: {"figi": f"FIGI-{t}", "uid": f"uid-{t}", "lot": 1} for t in tickers}


def test_download_prints_progress_per_ticker(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "make_api", lambda config: FakeDownloadAPI())
    monkeypatch.setattr(cli, "connect", lambda db_path: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(cli, "store_instruments", lambda conn, instruments: None)
    monkeypatch.setattr(cli, "download_candles", lambda *a, **k: 7)
    config = SimpleNamespace(
        tickers=["SBER", "GAZP"], candle_interval="hour", db_path=tmp_path / "m.sqlite"
    )

    assert cli.cmd_download(config, days=3) == 0

    out = capsys.readouterr().out
    assert "[1/2] SBER" in out
    assert "[2/2] GAZP" in out
    assert "SBER: 7 свечей" in out


# ---------- download_candles: сбой API посреди выгрузки ----------

def _candle_row(ts: str) -> dict:
    return {"time": ts, "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1, "is_complete": True}


def test_download_candles_keeps_stored_chunks_when_api_fails(tmp_path, monkeypatch):
    import datetime as dt

    import tbank.market_data as md

    monkeypatch.setattr(md, "time", SimpleNamespace(sleep=lambda s: None))
    conn = md.connect(tmp_path / "m.sqlite")
    chunk_days = md.CANDLE_INTERVALS["hour"]["chunk"]
    calls = {"n": 0}

    class FlakyAPI:
        def get_candles(self, figi, enum, start, end):
            calls["n"] += 1
            if calls["n"] == 1:
                return [_candle_row("2026-10-01T10:00:00+00:00"), _candle_row("2026-10-01T11:00:00+00:00")]
            raise RuntimeError("API down")

    now = dt.datetime(2026, 10, 24, tzinfo=dt.timezone.utc)
    with pytest.raises(RuntimeError, match="API down"):
        md.download_candles(FlakyAPI(), conn, "FIGI-SBER", "hour", days_back=chunk_days * 2 + 1, now=now, ticker="SBER")

    stored = conn.execute("SELECT COUNT(*) FROM candles WHERE ticker = 'SBER'").fetchone()[0]
    assert calls["n"] == 2
    assert stored == 2  # первый чанк уже записан и не откатывается
    conn.close()


# ---------- news: загрузка и сводка по тикерам ----------

class FakeNewsAPI:
    def resolve_instruments(self, tickers):
        return {t: {"figi": f"FIGI-{t}", "uid": f"uid-{t}", "lot": 1} for t in tickers}

    def get_news(self, max_pages=1):
        return [{"news_id": "n1", "title": "Новость", "pub_time": "2026-10-20T10:00:00+00:00"}]


def test_cmd_news_stores_items_and_prints_summary(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "make_api", lambda config: FakeNewsAPI())
    monkeypatch.setattr(cli, "connect", lambda db_path: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(cli, "store_news", lambda conn, items: len(items))
    frame = pd.DataFrame({"pub_time": ["2026-10-20T10:00:00"], "title": ["Новость"]})
    monkeypatch.setattr(cli, "load_news", lambda conn, uid, include_market=True: frame)
    config = SimpleNamespace(tickers=["SBER"], db_path=tmp_path / "m.sqlite")

    assert cli.cmd_news(config) == 0

    out = capsys.readouterr().out
    assert "загружено 1" in out
    assert "SBER" in out


# ---------- report: счёт и капитал ----------

class FakeReportAPI:
    def get_accounts(self):
        return [{"id": "acc-1"}]

    def get_portfolio(self, account_id):
        return {"total_amount_rub": 1_050_000.0, "cash_rub": 50_000.0, "positions": {}}

    def resolve_instruments(self, tickers):
        return {}


def test_report_shows_capital_and_pnl(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "make_api", lambda config: FakeReportAPI())
    config = SimpleNamespace(
        mode="sandbox",
        reports_dir=tmp_path,
        tickers=["SBER"],
        account_id="",
        sandbox_initial_rub=1_000_000.0,
        account_budget_rub=None,
    )

    assert cli.cmd_report(config) == 0

    out = capsys.readouterr().out
    assert "Капитал" in out
    assert "P&L" in out


def test_report_without_accounts_returns_1(tmp_path, monkeypatch):
    class NoAccountsAPI(FakeReportAPI):
        def get_accounts(self):
            return []

    monkeypatch.setattr(cli, "make_api", lambda config: NoAccountsAPI())
    config = SimpleNamespace(
        mode="sandbox",
        reports_dir=tmp_path,
        tickers=["SBER"],
        account_id="",
        sandbox_initial_rub=1_000_000.0,
        account_budget_rub=None,
    )

    assert cli.cmd_report(config) == 1

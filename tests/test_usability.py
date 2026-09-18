"""Удобство использования: subparsers, предупреждения .env, status, setup, send-test-email."""
from __future__ import annotations

import json
import logging
import logging.handlers
import sys

import pytest

import cli
from config import Config
from tbank.market_data import connect


def make_config(tmp_path, **kwargs) -> Config:
    defaults = dict(
        token="t",
        mode="sandbox",
        tickers=["SBER"],
        db_path=tmp_path / "market.sqlite",
        models_dir=tmp_path / "artifacts",
        reports_dir=tmp_path / "reports",
    )
    return Config(**{**defaults, **kwargs})


# ---------- .env: опечатки не проходят молча ----------

def test_env_float_typo_warns_and_uses_default(monkeypatch, capsys):
    monkeypatch.setenv("MAX_POSITION_PCT", "0,2")  # запятая вместо точки
    config = Config(token="t")
    assert config.max_position_pct == pytest.approx(0.20)
    assert "MAX_POSITION_PCT" in capsys.readouterr().err


def test_env_bool_unknown_value_warns(monkeypatch, capsys):
    monkeypatch.setenv("ORDER_FALLBACK_TO_MARKET", "maybe")
    config = Config(token="t")
    assert config.order_fallback_to_market is True  # дефолт
    assert "ORDER_FALLBACK_TO_MARKET" in capsys.readouterr().err


def test_env_valid_values_do_not_warn(monkeypatch, capsys):
    monkeypatch.setenv("MAX_POSITION_PCT", "0.5")
    monkeypatch.setenv("NLP_EMBEDDER", "0")
    config = Config(token="t")
    assert config.max_position_pct == pytest.approx(0.5)
    assert config.nlp_embedder is False
    assert capsys.readouterr().err == ""


# ---------- парсер: флаги принадлежат своим командам ----------

def test_parser_rejects_foreign_flags():
    parser = cli._build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["smoke", "--restart"])
    with pytest.raises(SystemExit):
        parser.parse_args(["backtest", "--days", "5"])  # раньше молча игнорировался
    args = parser.parse_args(["watchdog", "--max-stale-min", "5", "--restart"])
    assert args.command == "watchdog" and args.max_stale_min == 5 and args.restart
    args = parser.parse_args(["download", "--days", "30"])
    assert args.days == 30


def test_main_status_and_test_email_work_without_token(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["cli.py", "status"])
    monkeypatch.setenv("T_INVEST_TOKEN", "")
    monkeypatch.setattr(cli, "cmd_status", lambda config: 0)
    assert cli.main() == 0  # status не требует токена

    monkeypatch.setattr(sys, "argv", ["cli.py", "send-test-email"])
    monkeypatch.setattr(cli, "cmd_send_test_email", lambda config: 0)
    assert cli.main() == 0  # проверка SMTP тоже не требует токена


# ---------- status ----------

def test_status_reports_data_models_and_gaps(tmp_path, capsys):
    config = make_config(tmp_path, tickers=["SBER", "GAZP"])
    conn = connect(config.db_path)
    conn.execute(
        "INSERT INTO candles (figi, ticker, interval, time, open, high, low, close, volume)"
        " VALUES ('F1', 'SBER', 'hour', '2026-01-01T10:00:00+00:00', 1, 1, 1, 100, 10)"
    )
    conn.commit()
    conn.close()
    config.models_dir.mkdir(parents=True)
    (config.models_dir / "model_SBER_hour_h1.joblib").write_bytes(b"x")
    (config.models_dir / "metrics_SBER_hour_h1.json").write_text(
        json.dumps({"best": "lgbm", "threshold": 0.0012,
                    "metrics": {"directional_acc": {"lgbm": 0.55}}}),
        encoding="utf-8",
    )

    assert cli.cmd_status(config) == 0
    out = capsys.readouterr().out
    assert "SBER" in out and "lgbm" in out and "55.0%" in out
    assert "GAZP" in out  # нет ни данных, ни модели — должен быть назван в предупреждениях
    assert "download" in out and "train" in out  # подсказки, что делать с пробелами


def test_status_on_empty_project(tmp_path, capsys):
    config = make_config(tmp_path)
    assert cli.cmd_status(config) == 0
    out = capsys.readouterr().out
    assert "Свечей нет" in out and "нет моделей" in out


# ---------- setup: цепочка шагов с остановкой на сбое ----------

def test_setup_runs_all_steps_in_order(tmp_path, monkeypatch):
    calls = []

    def fake(name, rc=0):
        return lambda *a, **k: (calls.append(name), rc)[1]

    for step in ("smoke", "download", "news", "train", "backtest"):
        monkeypatch.setattr(cli, f"cmd_{step}", fake(step))
    config = make_config(tmp_path)

    assert cli.cmd_setup(config) == 0
    assert calls == ["smoke", "download", "news", "train", "backtest"]


def test_setup_stops_on_failed_step(tmp_path, monkeypatch, capsys):
    calls = []

    def fake(name, rc=0):
        return lambda *a, **k: (calls.append(name), rc)[1]

    for step in ("smoke", "download", "news", "train", "backtest"):
        monkeypatch.setattr(cli, f"cmd_{step}", fake(step, rc=(1 if step == "train" else 0)))
    config = make_config(tmp_path)

    assert cli.cmd_setup(config) == 1
    assert calls == ["smoke", "download", "news", "train"]  # backtest не запускался
    assert "python cli.py train" in capsys.readouterr().out  # подсказка продолжения


# ---------- send-test-email ----------

def test_send_test_email_fails_cleanly_without_smtp(tmp_path, capsys):
    config = make_config(tmp_path, notify_email_to="", smtp_host="", smtp_user="", smtp_password="")
    assert cli.cmd_send_test_email(config) == 2
    assert "SMTP" in capsys.readouterr().out


def test_send_test_email_ok_with_mocked_send(tmp_path, monkeypatch, capsys):
    from notify import Notifier

    sent = {}
    monkeypatch.setattr(Notifier, "send", lambda self, subject, body: sent.update(subject=subject) or True)
    config = make_config(
        tmp_path, notify_email_to="trader@example.com", smtp_host="smtp.yandex.ru",
        smtp_user="bot@yandex.ru", smtp_password="secret",
    )
    assert cli.cmd_send_test_email(config) == 0
    assert sent["subject"] == "тест уведомлений"
    assert "trader@example.com" in capsys.readouterr().out


# ---------- backtest: честный exit-код ----------

def test_backtest_returns_1_when_all_tickers_skipped(tmp_path):
    config = make_config(tmp_path)  # БД пуста — тикер пропускается
    assert cli.cmd_backtest(config) == 1


# ---------- файловый лог с ротацией ----------

def test_attach_file_log_writes_rotating_file(tmp_path):
    config = make_config(tmp_path, log_file=tmp_path / "logs" / "bot.log",
                         log_max_bytes=1000, log_backups=2)
    try:
        cli._attach_file_log(config)
        logging.getLogger("tbank-test").warning("запись в файл")
        assert (tmp_path / "logs" / "bot.log").exists()
    finally:
        for handler in list(logging.root.handlers):
            if isinstance(handler, logging.handlers.RotatingFileHandler):
                handler.close()
                logging.root.removeHandler(handler)


def test_attach_file_log_noop_without_log_file(tmp_path):
    config = make_config(tmp_path, log_file=None)
    before = list(logging.root.handlers)
    cli._attach_file_log(config)
    assert list(logging.root.handlers) == before

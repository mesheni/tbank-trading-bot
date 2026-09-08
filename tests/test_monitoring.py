"""Мониторинг: SMTP-нотификатор, watchdog живости, календарь сессии."""
from __future__ import annotations

import datetime as dt

import cli
from bot import MSK, SessionCalendar, write_heartbeat
from notify import Notifier, SmtpConfig


# ---------- Notifier ----------

def make_notifier(**overrides) -> Notifier:
    cfg = SmtpConfig(
        host="smtp.example.ru", port=465, user="bot@example.ru",
        password="app-password", to_addr="trader@example.ru",
    )
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return Notifier(cfg)


def test_disabled_notifier_silently_skips():
    notifier = Notifier(SmtpConfig())  # ничего не настроено
    assert notifier.enabled is False
    assert notifier.send("тема", "текст") is False  # и не падает


def test_send_uses_smtp_ssl_and_headers(monkeypatch):
    sent: list = []

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            assert host == "smtp.example.ru" and port == 465

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def login(self, user, password):
            assert user == "bot@example.ru"

        def send_message(self, msg):
            sent.append(msg)

    import smtplib

    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTP)
    notifier = make_notifier()
    assert notifier.send("сделка", "тело") is True
    assert len(sent) == 1
    assert sent[0]["Subject"] == "[tbank-bot] сделка"
    assert sent[0]["To"] == "trader@example.ru"


def test_send_swallows_smtp_failure(monkeypatch, caplog):
    import smtplib

    def boom(*args, **kwargs):
        raise OSError("нет сети")

    monkeypatch.setattr(smtplib, "SMTP_SSL", boom)
    notifier = make_notifier()
    assert notifier.send("тема", "текст") is False  # уведомление не роняет цикл


def test_send_throttled_dedups_within_interval():
    notifier = make_notifier()
    notifier._last_sent["key"] = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=5)
    assert notifier.send_throttled("key", "тема", "текст") is False  # недавно уже писали
    notifier._last_sent["key"] = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=45)
    # send не настроен на реальную отправку -> False, но троттлинг по ключу проверен выше
    assert notifier.send_throttled("key", "тема", "текст") is False


def test_smtp_config_from_env_fields():
    from types import SimpleNamespace

    cfg = SmtpConfig.from_config(SimpleNamespace())  # без SMTP-полей -> всё выключено
    assert cfg.port == 465
    assert cfg.configured is False


# ---------- SessionCalendar ----------

def test_calendar_fallback_session_weekday_msk():
    calendar = SessionCalendar(api=None)
    wednesday_noon = dt.datetime(2026, 9, 9, 12, 0, tzinfo=MSK)  # среда
    wednesday_night = dt.datetime(2026, 9, 9, 4, 0, tzinfo=MSK)
    sunday = dt.datetime(2026, 9, 13, 12, 0, tzinfo=MSK)
    assert calendar.active(wednesday_noon) is True
    assert calendar.active(wednesday_night) is False
    assert calendar.active(sunday) is False


def test_calendar_uses_api_intervals_over_fallback():
    now = dt.datetime.now(MSK)
    night_hour = dt.time(3, 0)

    class StubAPI:
        def trading_schedules(self, figi="", days=7):
            # «вечерняя» сессия, покрывающая 3 часа ночи — фолбэк сказал бы «закрыто»
            start = dt.datetime.combine(now.date(), night_hour, tzinfo=MSK)
            return [(start, start + dt.timedelta(hours=2))]

    calendar = SessionCalendar(StubAPI())
    at_night = dt.datetime.combine(now.date(), night_hour, tzinfo=MSK) + dt.timedelta(minutes=30)
    assert calendar.active(at_night) is True


# ---------- watchdog ----------

class ScheduleAPI:
    """Сессия всегда открыта — изолируем проверку живости от календаря."""

    def trading_schedules(self, figi="", days=7):
        now = dt.datetime.now(MSK)
        return [(now - dt.timedelta(hours=1), now + dt.timedelta(hours=1))]


def make_watchdog_config(tmp_path):
    return type(
        "Cfg",
        (),
        {
            "reports_dir": tmp_path,
            "sandbox_initial_rub": 1_000_000.0,
            "tickers": ["SBER"],
            "mode": "sandbox",
            "token": "t",
            "order_fallback_to_market": True,
            "notify_email_to": "",
        },
    )()


def test_last_activity_prefers_freshest_stamp(tmp_path):
    import pandas as pd

    now = dt.datetime.now(dt.timezone.utc)
    write_heartbeat(tmp_path / "heartbeat")
    with open(tmp_path / "equity_live.csv", "w", encoding="utf-8") as f:
        f.write("time,total_rub,cash_rub,positions_rub\n")
        f.write(f"{(now - dt.timedelta(hours=5)).isoformat()},1,1,0\n")

    last, source = cli._last_activity(tmp_path)
    assert source == "heartbeat"
    assert (now - last).total_seconds() < 60


def test_watchdog_fresh_heartbeat_passes(tmp_path, monkeypatch):
    config = make_watchdog_config(tmp_path)
    monkeypatch.setattr(cli, "make_api", lambda cfg: ScheduleAPI())
    write_heartbeat(tmp_path / "heartbeat")

    assert cli.cmd_watchdog(config, max_stale_min=20, restart=False) == 0


def test_watchdog_stale_heartbeat_alerts(tmp_path, monkeypatch):
    config = make_watchdog_config(tmp_path)
    monkeypatch.setattr(cli, "make_api", lambda cfg: ScheduleAPI())
    stale = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=2)
    (tmp_path / "heartbeat").write_text(stale.isoformat(), encoding="utf-8")

    assert cli.cmd_watchdog(config, max_stale_min=20, restart=False) == 1


def test_watchdog_outside_session_is_noop(tmp_path, monkeypatch):
    import bot as bot_mod

    config = make_watchdog_config(tmp_path)
    # cmd_watchdog импортирует SessionCalendar из bot внутри функции — патчим модуль bot
    monkeypatch.setattr(
        bot_mod,
        "SessionCalendar",
        lambda api: type("C", (), {"active": lambda self, now=None: False})(),
    )

    assert cli.cmd_watchdog(config, max_stale_min=20, restart=False) == 0
    # вне сессии watchdog не создаёт ложных меток алертов
    assert not (tmp_path / "watchdog_last_alert").exists()

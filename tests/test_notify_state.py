"""Persistent notification throttle: окна троттлинга переживают рестарт (аудит F-C3.9)."""
from __future__ import annotations

import datetime as dt
import json
from types import SimpleNamespace

import notify
from notify import Notifier, SmtpConfig


def smtp_cfg() -> SmtpConfig:
    return SmtpConfig(
        host="smtp.example",
        user="user",
        password="pass",
        from_addr="from@example.com",
        to_addr="to@example.com",
    )


def fake_send_ok(monkeypatch) -> None:
    monkeypatch.setattr(Notifier, "send", lambda self, subject, body: True)


def test_throttle_state_survives_new_notifier(tmp_path, monkeypatch):
    fake_send_ok(monkeypatch)
    state_path = tmp_path / "notify_state.json"

    first = Notifier(smtp_cfg(), state_path=state_path)
    assert first.send_throttled("step-error:SBER", "ошибка", "текст") is True
    assert state_path.exists()

    # новый процесс/объект с тем же state_path: окно троттлинга уже открыто
    second = Notifier(smtp_cfg(), state_path=state_path)
    assert second.send_throttled("step-error:SBER", "ошибка", "текст") is False

    stored = json.loads(state_path.read_text(encoding="utf-8"))
    assert "step-error:SBER" in stored
    assert second._last_sent["step-error:SBER"].tzinfo is not None


def test_successful_send_after_window_persists(tmp_path, monkeypatch):
    fake_send_ok(monkeypatch)
    state_path = tmp_path / "notify_state.json"
    notifier = Notifier(smtp_cfg(), state_path=state_path)

    assert notifier.send_throttled("k", "s", "b", min_interval_sec=0) is True
    assert notifier.send_throttled("k", "s", "b", min_interval_sec=0) is True
    assert "k" in json.loads(state_path.read_text(encoding="utf-8"))


def test_failed_send_does_not_persist_key(tmp_path, monkeypatch):
    monkeypatch.setattr(Notifier, "send", lambda self, subject, body: False)
    state_path = tmp_path / "notify_state.json"
    notifier = Notifier(smtp_cfg(), state_path=state_path)

    assert notifier.send_throttled("k", "s", "b") is False
    assert notifier._last_sent == {}
    assert not state_path.exists()


def test_failed_send_is_not_retried_before_retry_window(monkeypatch):
    """SMTP недоступен: попытка блокирует цикл на таймаут — не чаще раза в FAILED_SEND_RETRY_SEC."""
    attempts: list[str] = []
    monkeypatch.setattr(Notifier, "send", lambda self, subject, body: attempts.append(subject) or False)
    notifier = Notifier(smtp_cfg())

    assert notifier.send_throttled("k", "s", "b") is False
    assert notifier.send_throttled("k", "s", "b") is False  # следующая итерация торгового цикла
    assert notifier.send_throttled("other", "s2", "b") is False  # чужие ключи не заглушены
    assert attempts == ["s", "s2"]

    notifier._last_failed["k"] -= dt.timedelta(seconds=notify.FAILED_SEND_RETRY_SEC + 1)
    assert notifier.send_throttled("k", "s", "b") is False
    assert attempts == ["s", "s2", "s"]  # окно прошло — пробуем снова


def test_success_after_failure_clears_failure_window(tmp_path, monkeypatch):
    outcomes = iter([False, True])
    monkeypatch.setattr(Notifier, "send", lambda self, subject, body: next(outcomes))
    state_path = tmp_path / "notify_state.json"
    notifier = Notifier(smtp_cfg(), state_path=state_path)

    assert notifier.send_throttled("k", "s", "b") is False
    notifier._last_failed.clear()  # как если бы окно повтора прошло
    assert notifier.send_throttled("k", "s", "b") is True

    assert "k" not in notifier._last_failed
    assert "k" in json.loads(state_path.read_text(encoding="utf-8"))


def test_failure_window_lives_only_in_memory(tmp_path, monkeypatch):
    """После рестарта одна попытка допустима: состояние неудач на диск не пишется."""
    attempts: list[str] = []
    monkeypatch.setattr(Notifier, "send", lambda self, subject, body: attempts.append(subject) or False)
    state_path = tmp_path / "notify_state.json"
    Notifier(smtp_cfg(), state_path=state_path).send_throttled("k", "s", "b")
    restarted = Notifier(smtp_cfg(), state_path=state_path)

    assert restarted._last_failed == {}
    assert restarted.send_throttled("k", "s", "b") is False
    assert len(attempts) == 2


def test_malformed_state_entries_are_ignored(tmp_path):
    state_path = tmp_path / "notify_state.json"
    state_path.write_text(
        json.dumps(
            {
                "bad-type": 42,
                "bad-date": "not-a-date",
                "naive": "2026-10-24T10:00:00",
                "good": "2026-10-24T10:00:00+03:00",
            }
        ),
        encoding="utf-8",
    )

    notifier = Notifier(SmtpConfig(), state_path=state_path)

    assert set(notifier._last_sent) == {"naive", "good"}
    assert notifier._last_sent["naive"].tzinfo is not None
    assert notifier._last_sent["good"].utcoffset().total_seconds() == 3 * 3600


def test_corrupt_state_file_does_not_raise(tmp_path):
    state_path = tmp_path / "notify_state.json"
    state_path.write_text("{not json", encoding="utf-8")

    notifier = Notifier(SmtpConfig(), state_path=state_path)

    assert notifier._last_sent == {}


def test_no_state_file_when_state_path_is_none(tmp_path, monkeypatch):
    fake_send_ok(monkeypatch)
    notifier = Notifier(smtp_cfg())
    assert notifier.state_path is None

    assert notifier.send_throttled("k", "s", "b") is True

    assert list(tmp_path.iterdir()) == []


def test_from_config_uses_reports_dir_state_path(tmp_path):
    config = SimpleNamespace(
        reports_dir=tmp_path,
        smtp_host="smtp.example",
        smtp_port=465,
        smtp_user="user",
        smtp_password="pass",
        smtp_from="",
        notify_email_to="to@example.com",
    )
    notifier = Notifier.from_config(config)

    assert notifier.state_path == tmp_path / "notify_state.json"


def test_from_config_without_reports_dir_keeps_state_disabled():
    config = SimpleNamespace(
        smtp_host="smtp.example",
        smtp_port=465,
        smtp_user="user",
        smtp_password="pass",
        smtp_from="",
        notify_email_to="to@example.com",
    )
    notifier = Notifier.from_config(config)

    assert notifier.state_path is None

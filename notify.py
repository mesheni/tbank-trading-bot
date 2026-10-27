"""Email-уведомления бота (SMTP): сделки, ошибки, watchdog, дайджесты.

Канал — обычный SMTP-сервер с паролем приложения (Яндекс/Mail.ru; Telegram
из РФ недоступен). Отправка обязана быть отказоустойчивой: любое исключение
логируется и проглатывается — уведомление не может уронить торговый цикл.

Конфигурация (.env):
  NOTIFY_EMAIL_TO   адрес получателя (пусто — уведомления выключены)
  SMTP_HOST         smtp.yandex.ru | smtp.mail.ru | ...
  SMTP_PORT         465 (SSL, по умолчанию) | 587 (STARTTLS)
  SMTP_USER         логин аккаунта
  SMTP_PASSWORD     пароль приложения (не пароль аккаунта!)
  SMTP_FROM         адрес отправителя (по умолчанию — SMTP_USER)
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import smtplib
from dataclasses import dataclass
from email.mime.text import MIMEText
from email.utils import formataddr
from pathlib import Path

log = logging.getLogger(__name__)


def throttle_key(action: str, kind: str, ticker: str) -> str:
    """Ключ троттлинга, изолированный по направлению сделки.

    bot.py сейчас строит ключи без направления (``order-not-filled:SBER``),
    поэтому письмо о неисполненной покупке глушит письмо о неисполненной
    продаже того же тикера. Формат ``<kind>:<action>:<ticker>`` разводит
    BUY и SELL по разным пространствам ключей.
    """
    return f"{kind}:{action}:{ticker}"


@dataclass
class SmtpConfig:
    host: str = ""
    port: int = 465
    user: str = ""
    password: str = ""
    from_addr: str = ""
    to_addr: str = ""
    timeout_sec: float = 15.0

    @property
    def configured(self) -> bool:
        return bool(self.host and self.user and self.password and self.to_addr)

    @classmethod
    def from_config(cls, config) -> "SmtpConfig":
        return cls(
            host=getattr(config, "smtp_host", ""),
            port=int(getattr(config, "smtp_port", 465)),
            user=getattr(config, "smtp_user", ""),
            password=getattr(config, "smtp_password", ""),
            from_addr=getattr(config, "smtp_from", "") or getattr(config, "smtp_user", ""),
            to_addr=getattr(config, "notify_email_to", ""),
        )


class Notifier:
    """Отправка коротких писем; исключения не покидают send()."""

    def __init__(self, cfg: SmtpConfig | None = None, state_path: Path | None = None):
        self.cfg = cfg or SmtpConfig()
        self.state_path = Path(state_path) if state_path is not None else None
        self._last_sent: dict[str, dt.datetime] = {}
        self._load_state()

    @classmethod
    def from_config(cls, config) -> "Notifier":
        reports_dir = getattr(config, "reports_dir", None)
        state_path = Path(reports_dir) / "notify_state.json" if reports_dir else None
        return cls(SmtpConfig.from_config(config), state_path=state_path)

    def _load_state(self) -> None:
        """Поднимает окна троттлинга прошлого запуска (некорректные записи игнорируются)."""
        if self.state_path is None:
            return
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            log.warning("Состояние троттлинга не загружено (%s): %s", self.state_path, exc)
            return
        if not isinstance(raw, dict):
            log.warning("Состояние троттлинга повреждено (%s): ожидался объект", self.state_path)
            return
        for key, value in raw.items():
            try:
                sent_at = dt.datetime.fromisoformat(value)
            except (TypeError, ValueError):
                continue
            if sent_at.tzinfo is None:
                sent_at = sent_at.replace(tzinfo=dt.timezone.utc)
            self._last_sent[key] = sent_at

    def _save_state(self) -> None:
        """Атомарно сохраняет окна троттлинга: временный файл + os.replace."""
        if self.state_path is None:
            return
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_name(f"{self.state_path.name}.{os.getpid()}.tmp")
            payload = {key: sent_at.isoformat() for key, sent_at in self._last_sent.items()}
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self.state_path)
        except OSError as exc:
            log.warning("Состояние троттлинга не сохранено (%s): %s", self.state_path, exc)

    @property
    def enabled(self) -> bool:
        return self.cfg.configured

    def send(self, subject: str, body: str) -> bool:
        if not self.enabled:
            return False
        try:
            msg = MIMEText(body, "plain", "utf-8")
            msg["Subject"] = f"[tbank-bot] {subject}"
            msg["From"] = formataddr(("tbank-bot", self.cfg.from_addr or self.cfg.user))
            msg["To"] = self.cfg.to_addr
            if self.cfg.port == 465:
                with smtplib.SMTP_SSL(self.cfg.host, self.cfg.port, timeout=self.cfg.timeout_sec) as smtp:
                    smtp.login(self.cfg.user, self.cfg.password)
                    smtp.send_message(msg)
            else:
                with smtplib.SMTP(self.cfg.host, self.cfg.port, timeout=self.cfg.timeout_sec) as smtp:
                    smtp.starttls()
                    smtp.login(self.cfg.user, self.cfg.password)
                    smtp.send_message(msg)
            return True
        except Exception as exc:
            log.warning("Email не отправлен (%s): %s", subject, exc)
            return False

    def send_throttled(self, key: str, subject: str, body: str, min_interval_sec: float = 1800.0) -> bool:
        """Повторяющиеся события (по ключу) не чаще раза в min_interval_sec.

        Защита от почтового спама, когда одна и та же ошибка падает каждую итерацию.
        """
        now = dt.datetime.now(dt.timezone.utc)
        last = self._last_sent.get(key)
        if last is not None and (now - last).total_seconds() < min_interval_sec:
            return False
        if self.send(subject, body):
            self._last_sent[key] = now
            self._save_state()
            return True
        return False

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
import logging
import smtplib
from dataclasses import dataclass
from email.mime.text import MIMEText
from email.utils import formataddr

log = logging.getLogger(__name__)


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

    def __init__(self, cfg: SmtpConfig | None = None):
        self.cfg = cfg or SmtpConfig()
        self._last_sent: dict[str, dt.datetime] = {}

    @classmethod
    def from_config(cls, config) -> "Notifier":
        return cls(SmtpConfig.from_config(config))

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
            return True
        return False

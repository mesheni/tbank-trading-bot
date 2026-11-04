"""Сервис управления веб-панели: те же гварды, что у TUI, плюс аудит (T5).

HTTP здесь нет. Роуты (T7) вызывают эти функции через ``asyncio.to_thread``:
остановка/сброс блокируются на время ожидания ack, но event loop не трогают.

Правила те же, что в TUI (``tui/app.py:156-268``), и логика гвардов не
копируется — импортируется из ``tui.controls``:

* stop — только для живого демона, затем команда ``stop`` и ожидание ack,
  подтверждённого наблюдаемым ``state=stopped``;
* kill_switch_reset — через демон, если он жив или статус повреждён (fail
  closed), иначе прямая очистка файла;
* start — ``TUI_SYSTEMD_MANAGED=1`` делегирует узкому systemctl-адаптеру,
  иначе проверка ``start_refusal`` и отсоединённый ``spawn_daemon``.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal

import bot
import control
import tui.controls as controls
import tui.data as data
from web import systemd

log = logging.getLogger(__name__)

AUDIT_FILENAME = "web_audit.jsonl"
# Быстрый отклик для тестового дедлайна 0.2 c и щадящая нагрузка на диск в бою.
ACK_POLL_INTERVAL_SEC = 0.1
# Одна строка аудита = один write; лок не даёт параллельным запросам перемешать байты.
_AUDIT_LOCK = threading.Lock()

AuditResult = Literal["ok", "refused", "timeout", "error"]


@dataclass(frozen=True, slots=True)
class ControlResult:
    """Итог действия для роута: текст статуса, id команды и метка аудита."""

    ok: bool
    message: str
    command_id: str | None
    audit_result: AuditResult


@dataclass(frozen=True, slots=True)
class _AuditContext:
    """Что фиксируем в аудите помимо исхода действия (секретов здесь нет)."""

    action: str
    user: str
    client_ip: str


@dataclass(frozen=True, slots=True)
class _PendingAck:
    """Параметры ожидания ack одной команды."""

    reports_dir: Path
    command_id: str
    timeout: float


def _require_reauth(reauth_ok: bool) -> None:
    """Защита в глубину: роут уже проверил пароль, сервис проверяет ещё раз."""
    if reauth_ok is not True:
        raise PermissionError("управляющее действие требует повторного ввода пароля")


def _wait_ack(pending: _PendingAck, observed: Callable[[], bool]) -> tuple[str, str]:
    """Ждёт ack и наблюдаемый результат; возвращает (ok|error|timeout, detail).

    ``ack ok=False`` — сразу ошибка с detail. ``ok=True`` засчитывается только
    когда ``observed()`` подтвердил результат: ack пишется до выполнения.
    """
    deadline = time.monotonic() + pending.timeout
    while True:
        try:
            ack = controls.read_ack(pending.reports_dir, pending.command_id)
        except (OSError, ValueError) as exc:
            return "error", str(exc)
        if isinstance(ack, dict):
            if not ack.get("ok", False):
                return "error", str(ack.get("detail") or "неизвестная ошибка")
            if observed():
                return "ok", ""
        if time.monotonic() >= deadline:
            return "timeout", ""
        time.sleep(ACK_POLL_INTERVAL_SEC)


def _append_audit(config, context: _AuditContext, result: ControlResult) -> None:
    """Одна JSON-строка на действие; сбой записи не меняет исход и не бросает."""
    entry = {
        "time": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "action": context.action,
        "user": str(context.user or ""),
        "client_ip": str(context.client_ip or ""),
        "result": result.audit_result,
        "command_id": result.command_id,
    }
    path = Path(config.reports_dir) / AUDIT_FILENAME
    line = json.dumps(entry, ensure_ascii=False) + "\n"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _AUDIT_LOCK, open(path, "a", encoding="utf-8") as stream:
            stream.write(line)
    except OSError as exc:
        log.warning("Не удалось записать аудит веб-панели %s: %s", path, exc)


def _finish(config, context: _AuditContext, result: ControlResult) -> ControlResult:
    """Фиксирует ровно одну строку аудита и возвращает исход без изменений."""
    _append_audit(config, context, result)
    return result


def request_stop(
    config,
    reauth_ok: bool,
    *,
    timeout: float = 60.0,
    user: str = "",
    client_ip: str = "",
) -> ControlResult:
    """Мягкая остановка демона: очередь ``stop`` + подтверждённый ``state=stopped``."""
    _require_reauth(reauth_ok)
    context = _AuditContext("stop", user, client_ip)
    reports_dir = Path(config.reports_dir)
    if not controls.daemon_online(data.daemon_state(reports_dir)):
        return _finish(
            config, context, ControlResult(False, controls.DAEMON_NOT_RUNNING, None, "refused")
        )
    try:
        command_id = control.enqueue_command(reports_dir, "stop")
    except (OSError, ValueError) as exc:
        return _finish(
            config, context, ControlResult(False, f"ошибка канала управления: {exc}", None, "error")
        )

    pending = _PendingAck(reports_dir, command_id, timeout)
    status, detail = _wait_ack(
        pending, lambda: data.daemon_state(reports_dir).get("state") == "stopped"
    )
    if status == "ok":
        return _finish(config, context, ControlResult(True, controls.STOPPED, command_id, "ok"))
    if status == "error":
        message = f"ошибка выполнения stop: {detail}"
        return _finish(config, context, ControlResult(False, message, command_id, "error"))
    return _finish(config, context, ControlResult(False, controls.STOP_TIMEOUT, command_id, "timeout"))


def request_kill_switch_reset(
    config,
    reauth_ok: bool,
    *,
    timeout: float = 60.0,
    user: str = "",
    client_ip: str = "",
) -> ControlResult:
    """Сброс kill-switch: через демон, если он жив/статус битый, иначе напрямую."""
    _require_reauth(reauth_ok)
    context = _AuditContext("kill_reset", user, client_ip)
    reports_dir = Path(config.reports_dir)

    if controls.kill_switch_needs_daemon(data.daemon_state(reports_dir)):
        try:
            command_id = control.enqueue_command(reports_dir, "kill_switch_reset")
        except (OSError, ValueError) as exc:
            return _finish(
                config,
                context,
                ControlResult(False, f"ошибка канала управления: {exc}", None, "error"),
            )
        pending = _PendingAck(reports_dir, command_id, timeout)
        status, detail = _wait_ack(pending, lambda: bot.read_kill_switch(reports_dir) is None)
        if status == "ok":
            return _finish(
                config, context, ControlResult(True, controls.KILL_SWITCH_RESET_CONFIRMED, command_id, "ok")
            )
        if status == "error":
            message = f"ошибка выполнения kill_switch_reset: {detail}"
            return _finish(config, context, ControlResult(False, message, command_id, "error"))
        return _finish(
            config, context, ControlResult(False, controls.KILL_SWITCH_TIMEOUT, command_id, "timeout")
        )

    # Демон офлайн: файл никто не перезапишет — чистим напрямую (как TUI).
    try:
        bot.clear_kill_switch(reports_dir)
    except OSError as exc:
        return _finish(
            config, context, ControlResult(False, f"ошибка записи kill-switch: {exc}", None, "error")
        )
    return _finish(config, context, ControlResult(True, controls.KILL_SWITCH_CLEARED, None, "ok"))


def request_start(
    config,
    reauth_ok: bool,
    *,
    user: str = "",
    client_ip: str = "",
) -> ControlResult:
    """Запуск демона: systemd-адаптер или отсоединённый процесс cli.py daemon run."""
    _require_reauth(reauth_ok)
    context = _AuditContext("start", user, client_ip)

    if controls.systemd_managed():
        try:
            outcome = systemd.run_systemctl("start")
        except (OSError, ValueError) as exc:
            return _finish(
                config, context, ControlResult(False, f"systemctl start: {exc}", None, "error")
            )
        if outcome.ok:
            return _finish(config, context, ControlResult(True, controls.SYSTEMD_START, None, "ok"))
        detail = outcome.detail or f"код возврата {outcome.returncode}"
        return _finish(
            config, context, ControlResult(False, f"systemctl start: {detail}", None, "error")
        )

    refusal = controls.start_refusal(config, data.daemon_state(config.reports_dir))
    if refusal is not None:
        return _finish(config, context, ControlResult(False, refusal, None, "refused"))
    try:
        controls.spawn_daemon()
    except OSError as exc:
        return _finish(
            config,
            context,
            ControlResult(False, f"не удалось запустить демон: {exc}", None, "error"),
        )
    return _finish(config, context, ControlResult(True, controls.STARTING, None, "ok"))

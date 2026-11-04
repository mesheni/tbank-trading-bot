"""Узкий адаптер systemctl для start/stop юнита бота (T6).

Разрешены только два действия и только безопасное имя юнита: никакого shell,
никаких произвольных команд. На не-POSIX хостах (Windows-разработка) вызов
возвращает отказ и не трогает subprocess.
"""
from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass

ALLOWED_ACTIONS = frozenset({"start", "stop"})
DEFAULT_UNIT = "tbank-bot"
UNIT_RE = re.compile(r"^[A-Za-z0-9_.@-]+$")
MAX_DETAIL = 200
TIMEOUT_DETAIL = "timeout"
UNAVAILABLE_DETAIL = "systemctl недоступен на этой ОС"


@dataclass(frozen=True, slots=True)
class SystemctlResult:
    """Итог вызова systemctl: успех, код возврата и усечённый stderr."""

    ok: bool
    returncode: int | None
    detail: str


def _resolve_unit(unit: str | None) -> str:
    candidate = unit if unit is not None else (os.getenv("SYSTEMD_UNIT", "").strip() or DEFAULT_UNIT)
    if not isinstance(candidate, str) or not UNIT_RE.fullmatch(candidate):
        raise ValueError(f"недопустимое имя systemd-юнита: {candidate!r}")
    return candidate


def _systemctl_argv(action: str, unit: str) -> list[str]:
    if os.geteuid() == 0:
        return ["/bin/systemctl", action, unit]
    return ["sudo", "-n", "/bin/systemctl", action, unit]


def run_systemctl(
    action: str, unit: str | None = None, *, timeout: float = 20.0
) -> SystemctlResult:
    """Выполнить ``systemctl start|stop`` для юнита без shell и с таймаутом.

    Args:
        action: строго ``"start"`` или ``"stop"``.
        unit: имя юнита; по умолчанию ``SYSTEMD_UNIT`` из env или ``tbank-bot``.
        timeout: предел ожидания процесса в секундах.

    Raises:
        ValueError: action вне {start, stop} или unit не соответствует ``^[A-Za-z0-9_.@-]+$``.
    """
    if not isinstance(action, str) or action not in ALLOWED_ACTIONS:
        raise ValueError(f"недопустимое действие systemctl: {action!r}")
    resolved_unit = _resolve_unit(unit)

    if os.name != "posix":
        return SystemctlResult(ok=False, returncode=None, detail=UNAVAILABLE_DETAIL)

    argv = _systemctl_argv(action, resolved_unit)
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=False,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return SystemctlResult(ok=False, returncode=None, detail=TIMEOUT_DETAIL)
    except OSError as exc:
        return SystemctlResult(ok=False, returncode=None, detail=str(exc)[:MAX_DETAIL])

    detail = (proc.stderr or "").strip()[:MAX_DETAIL].rstrip()
    return SystemctlResult(ok=proc.returncode == 0, returncode=proc.returncode, detail=detail)

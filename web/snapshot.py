"""Read-only snapshot model for the web panel (T2).

Composes the same readers the Textual dashboard uses (``tui.data``) into one
JSON-serialisable dict. The module only reads files; missing or corrupt data
degrades to empty values and never raises. Log lines are redacted before they
leave this module: anything shaped like ``token=value`` / ``password: value``
and every literal secret loaded in ``Config`` is replaced with ``[redacted]``.
"""
from __future__ import annotations

import datetime as dt
import math
import re
from pathlib import Path

import tui.data as data
from tui import _VAR_DIR

# Значение секрета после ключа заменяется до конца строки: лог только
# отображается, поэтому лишняя редакция безопаснее утечки.
_SECRET_PATTERN = re.compile(r"(token|password|secret|key)(\s*[=:])[^\r\n]*", re.IGNORECASE)
REDACTED = "[redacted]"

_SECRET_ATTRS = ("token", "smtp_password", "web_password_hash", "web_secret_key")


def _resolve_log_path(config) -> Path | None:
    """LOG_FILE из конфига, иначе var/daemon.log, иначе var/daemon.out.

    Точная копия tui/app.py:_resolve_log_path: web не должен импортировать
    Textual-приложение ради пяти строк, но путь обязан совпадать с TUI.
    """
    if config.log_file is not None:
        return Path(config.log_file)
    for name in ("daemon.log", "daemon.out"):
        candidate = _VAR_DIR / name
        if candidate.exists():
            return candidate
    return None


def _secret_values(config) -> list[str]:
    values: list[str] = []
    for attr in _SECRET_ATTRS:
        value = getattr(config, attr, "")
        if isinstance(value, str) and value:
            values.append(value)
    return values


def _redact_line(line: str, config) -> str:
    """Скрыть секрет после ключа и любые литеральные значения из Config."""
    redacted = _SECRET_PATTERN.sub(r"\1\2" + REDACTED, line)
    for value in _secret_values(config):
        redacted = redacted.replace(value, REDACTED)
    return redacted


def read_log_lines(config, n: int = 30) -> list[str]:
    """Последние ``n`` строк лога демона с редакцией секретов."""
    path = _resolve_log_path(config)
    if path is None:
        return []
    return [_redact_line(line, config) for line in data.tail_log(path, n)]


def _jsonable(value):
    """Рекурсивно привести значение к JSON-типам (datetime -> ISO-строка)."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        # NaN/Infinity не сериализуются (JSONResponse: allow_nan=False) — None.
        return value if math.isfinite(value) else None
    if isinstance(value, dt.datetime):
        return value.isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(item) for item in value]
    return str(value)


def build_snapshot(config, now: dt.datetime | None = None) -> dict:
    """Снимок состояния панели: демон, позиции, журнал, equity, лог.

    ``now`` нужен тестам для проверки границ свежести; по умолчанию — текущее
    UTC-время. Возвращаемый dict сериализуется ``json.dumps`` без ``default``.
    """
    reference = now or dt.datetime.now(dt.timezone.utc)
    reports_dir = config.reports_dir
    state_dict = data.daemon_state(reports_dir, now=reference)
    return {
        "generated_at": reference.isoformat(),
        "state": state_dict.get("state"),
        "badge": data.badge(state_dict, now=reference),
        "corrupt": bool(state_dict.get("corrupt")),
        "daemon": _jsonable(state_dict),
        "positions": _jsonable(data.positions(state_dict)),
        "journal": _jsonable(data.journal_tail(reports_dir)),
        "journal_last_age_min": _jsonable(data.journal_last_age_min(reports_dir, now=reference)),
        "equity": _jsonable(data.equity_last(reports_dir)),
        "session_pnl": _jsonable(data.session_pnl(reports_dir)),
        "pending_orders": _jsonable(data.pending_orders_count(reports_dir)),
        "kill_switch": _jsonable(data.kill_switch_state(reports_dir)),
        "log": read_log_lines(config),
    }

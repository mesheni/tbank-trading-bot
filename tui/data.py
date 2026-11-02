"""Чистые функции чтения состояния демона для TUI.

Здесь нет импортов Textual: модуль юнит-тестируется без терминала.
Все функции только читают файлы; битые и неполные данные возвращают
None/[]/"all", но никогда не бросают исключение.
"""
from __future__ import annotations

import csv
import datetime as dt
import json
from pathlib import Path

import control
from bot import read_kill_switch

# Пороги свежести heartbeat (минуты). Используются в badge() и переиспользуются
# в todo 9 (индикатор устаревания), поэтому живут здесь, а не в виджете.
FRESH_MAX_MIN: float = 5.0
STALE_MAX_MIN: float = 20.0

# Ключ-маркер повреждённого реестра заявок (tbank/trader.py:PENDING_CORRUPT_KEY).
PENDING_CORRUPT_KEY = "*"

_EQUITY_COLUMNS = ("time", "total_rub", "cash_rub", "positions_rub")
# Минимум колонок journal.csv, без которых строки бесполезны для дашборда
# (полный набор полей — tbank/trader.py:make_journal_row).
_JOURNAL_REQUIRED = ("time", "ticker", "action")


def _parse_stamp(raw: str) -> dt.datetime | None:
    """Разбирает ISO-время; наивное время считаем UTC, мусор -> None."""
    text = (raw or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        value = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value


def _last_activity(reports_dir) -> tuple[dt.datetime | None, str]:
    """Самая свежая отметка живости: heartbeat или последняя точка equity_live.csv.

    скопировано из cli.py:_last_activity, чтобы не тянуть cli в tui
    (импорт cli.py поднял бы argparse-парсер и все команды ради 10 строк).
    """
    stamps: list[tuple[dt.datetime, str]] = []
    root = Path(reports_dir)
    heartbeat = root / "heartbeat"
    if heartbeat.exists():
        try:
            stamp = _parse_stamp(heartbeat.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # UnicodeDecodeError — подкласс ValueError: нечитаемый heartbeat
            # (битые байты) не должен ронять дашборд, это просто «нет отметки».
            stamp = None
        if stamp is not None:
            stamps.append((stamp, "heartbeat"))
    equity = root / "equity_live.csv"
    if equity.exists():
        try:
            with open(equity, encoding="utf-8", newline="") as f:
                rows = list(csv.reader(f))
        except (OSError, UnicodeDecodeError, csv.Error):
            rows = []
        for row in reversed(rows[1:]):  # последняя строка с данными
            if row and row[0].strip():
                stamp = _parse_stamp(row[0])
                if stamp is not None:
                    stamps.append((stamp, "equity_live.csv"))
                break
    if not stamps:
        return None, ""
    return max(stamps, key=lambda item: item[0])


def daemon_state(reports_dir, now: dt.datetime | None = None) -> dict:
    """Снимок состояния: поля daemon_status.json + возраст отметки живости.

    ``corrupt`` выставляется только при битом daemon_status.json; отсутствие
    файла — это штатный «демон ещё не запускался», а не порча.
    """
    status = control.read_status(reports_dir)
    last, source = _last_activity(reports_dir)
    reference = now or dt.datetime.now(dt.timezone.utc)
    age_min = None
    if last is not None:
        age_min = (reference - last).total_seconds() / 60.0

    result: dict = {
        "status": status,
        "corrupt": bool(isinstance(status, dict) and status.get("corrupt")),
        "state": status.get("state") if isinstance(status, dict) else None,
        "heartbeat_age_min": age_min,
        "last_activity": last,
        "source": source,
    }
    if isinstance(status, dict) and not status.get("corrupt"):
        for key, value in status.items():
            result.setdefault(key, value)
    return result


def badge(state_dict: dict, now: dt.datetime | None = None) -> str:
    """Статус демона для индикатора: running | stale | offline | corrupt.

    Границы включительно (ровно 5.0 мин — ещё running, ровно 20.0 — ещё stale):
    ``age <= FRESH_MAX_MIN`` -> running, ``age <= STALE_MAX_MIN`` -> stale.
    """
    if state_dict.get("corrupt"):
        return "corrupt"
    age = state_dict.get("heartbeat_age_min")
    if age is None:
        last = state_dict.get("last_activity")
        if isinstance(last, dt.datetime):
            reference = now or dt.datetime.now(dt.timezone.utc)
            age = (reference - last).total_seconds() / 60.0
    if age is None:
        return "offline"
    if age <= FRESH_MAX_MIN:
        return "running"
    if age <= STALE_MAX_MIN:
        return "stale"
    return "offline"


def equity_last(reports_dir) -> dict | None:
    """Последняя точка reports/equity_live.csv или None (нет файла/строк/колонок)."""
    path = Path(reports_dir) / "equity_live.csv"
    if not path.exists():
        return None
    last: dict | None = None
    try:
        with open(path, encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            if any(column not in (reader.fieldnames or []) for column in _EQUITY_COLUMNS):
                return None
            for row in reader:
                last = row
    except (OSError, UnicodeDecodeError, csv.Error):
        return None
    if last is None:
        return None
    try:
        return {
            "time": last["time"],
            "total_rub": float(last["total_rub"]),
            "cash_rub": float(last["cash_rub"]),
            "positions_rub": float(last["positions_rub"]),
        }
    except (KeyError, TypeError, ValueError):
        return None


def journal_tail(reports_dir, n: int = 20) -> list[dict]:
    """Последние n строк reports/journal.csv; нет файла/битые данные -> []."""
    if n <= 0:
        return []
    path = Path(reports_dir) / "journal.csv"
    if not path.exists():
        return []
    try:
        with open(path, encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            if any(column not in (reader.fieldnames or []) for column in _JOURNAL_REQUIRED):
                return []
            rows = [dict(row) for row in reader]
    except (OSError, UnicodeDecodeError, csv.Error):
        return []
    return rows[-n:]


def kill_switch_state(reports_dir) -> dict | None:
    """Состояние reports/kill_switch.json через bot.read_kill_switch (None, если нет)."""
    return read_kill_switch(reports_dir)


def positions(state_dict: dict) -> list[dict]:
    """Позиции из снимка демона; нет/битый список -> [] (никогда не бросает)."""
    rows = state_dict.get("positions") if isinstance(state_dict, dict) else None
    if not isinstance(rows, list):
        return []
    return [dict(row) for row in rows if isinstance(row, dict)]


def session_pnl(reports_dir) -> dict | None:
    """P&L за живой прогон по equity_live.csv: last-first (None при < 2 точках)."""
    path = Path(reports_dir) / "equity_live.csv"
    if not path.exists():
        return None
    totals: list[float] = []
    try:
        with open(path, encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            if "total_rub" not in (reader.fieldnames or []):
                return None
            for row in reader:
                try:
                    totals.append(float(row.get("total_rub")))
                except (TypeError, ValueError):
                    continue
    except (OSError, UnicodeDecodeError, csv.Error):
        return None
    if len(totals) < 2:
        return None
    first, last = totals[0], totals[-1]
    abs_rub = last - first
    pct = (abs_rub / first) if first > 0 else None
    return {"abs_rub": abs_rub, "pct": pct}


def pending_orders_count(reports_dir) -> int | str:
    """Число незавершённых заявок; "all" — битый файл или fail-closed маркер '*'."""
    path = Path(reports_dir) / "pending_orders.json"
    if not path.exists():
        return 0
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "all"
    if not isinstance(payload, dict):
        return "all"
    if PENDING_CORRUPT_KEY in payload:
        return "all"
    return len(payload)

"""Formatting helpers shared by the web panel HTML views (T4).

Pure functions, no Textual import: the web process must not pull the TUI stack
just to print a ruble amount. Wording matches ``tui/controls.py`` and
``tui/app.py`` so the web dashboard reads exactly like the terminal one.
"""
from __future__ import annotations

import html

UNKNOWN = "--"
_BADGE_CLASSES = frozenset({"running", "stale", "offline", "corrupt"})


def esc(value: object) -> str:
    """Escape one dynamic value for HTML text or attribute context."""
    if value is None:
        return ""
    return html.escape(str(value), quote=True)


def dash(value: object) -> str:
    """Like :func:`esc` but shows ``--`` for an absent daemon field."""
    return esc(value) or UNKNOWN


def fmt_rub(value: object) -> str:
    """Ruble amount or ``--`` (same semantics as tui.controls.fmt_rub)."""
    if value is None:
        return UNKNOWN
    try:
        return f"{float(value):,.2f} ₽"
    except (TypeError, ValueError):
        return UNKNOWN


def fmt_signed_rub(value: object) -> str:
    """Signed ruble amount or ``--`` (same semantics as tui.controls.fmt_signed_rub)."""
    if value is None:
        return UNKNOWN
    try:
        return f"{float(value):+,.2f} ₽"
    except (TypeError, ValueError):
        return UNKNOWN


def fmt_age(age_min: object) -> str:
    """Heartbeat age in minutes (same wording as tui.controls.fmt_age)."""
    if age_min is None:
        return "нет отметки"
    try:
        return f"{float(age_min):.1f} мин назад"
    except (TypeError, ValueError):
        return "нет отметки"


def fmt_session_pnl(pnl: object) -> str:
    """Session P&L with rubles and percent (same wording as tui.controls)."""
    if not isinstance(pnl, dict):
        return "—"
    try:
        abs_rub = float(pnl["abs_rub"])
    except (KeyError, TypeError, ValueError):
        return "—"
    money = f"{abs_rub:+,.0f}".replace(",", " ")
    pct = pnl.get("pct")
    if pct is None:
        return f"{money} ₽"
    try:
        return f"{money} ₽ ({float(pct):+.1%})"
    except (TypeError, ValueError):
        return f"{money} ₽"


def kill_switch_text(kill_switch: object) -> str:
    """Kill-switch line text, mirroring tui/app.py _daemon_markup."""
    if kill_switch is None:
        return "нет"
    if not isinstance(kill_switch, dict) or kill_switch.get("corrupt"):
        return "файл повреждён"
    reason = kill_switch.get("reason") or kill_switch.get("kind") or "активен"
    return f"АКТИВЕН — {reason}"


def badge_class(badge: object) -> str:
    """Whitelisted CSS class for the daemon badge (never raw user-ish text)."""
    text = str(badge or "")
    return text if text in _BADGE_CLASSES else "unknown"


def mode_text(config) -> tuple[str, str]:
    """Mode banner text and its CSS class, mirroring tui/app.py _mode_banner."""
    if str(getattr(config, "mode", "")) == "real":
        return "РЕЖИМ: REAL", "mode-real"
    return "РЕЖИМ: SANDBOX", "mode-sandbox"


def refresh_ms(config) -> int:
    """Dashboard poll interval in milliseconds from ``tui_refresh_sec``."""
    try:
        seconds = float(getattr(config, "tui_refresh_sec", 5.0))
    except (TypeError, ValueError):
        seconds = 5.0
    return max(int(seconds * 1000), 1000)

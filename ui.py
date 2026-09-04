"""Консольный UI: ANSI-цвета, форматирование чисел и таблиц.

Цвета включаются автоматически, когда поток — терминал (на Windows включается
VT-режим консоли), и отключаются при перенаправлении вывода в файл — поэтому
лог-файлы и отчёты остаются чистыми. Уважается переменная NO_COLOR;
FORCE_COLOR=1 включает цвета принудительно.
"""
from __future__ import annotations

import ctypes
import os
import re
import sys

_RESET = "\033[0m"
_ANSI_RE = re.compile(r"\033\[[0-9;]*m")

# палитра
GREEN = "\033[32m"
BRIGHT_GREEN = "\033[92m"
RED = "\033[91m"
BRIGHT_RED = "\033[1;91m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
BOLD = "\033[1m"
DIM = "\033[2m"

_enabled: dict[int, bool] = {}


def _enable_windows_vt(stream) -> bool:
    """Включает обработку ANSI-кодов в консоли Windows; False — если вывод перенаправлен."""
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11 if stream is sys.stdout else -12)
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
    except Exception:
        return False


def _detect(stream) -> bool:
    if os.getenv("NO_COLOR"):
        return False
    if os.getenv("FORCE_COLOR"):
        return True
    try:
        if not stream.isatty():
            return False
    except (AttributeError, ValueError):
        return False
    if os.name == "nt":
        return _enable_windows_vt(stream)
    return os.getenv("TERM", "") != "dumb"


def color_enabled(stream=None) -> bool:
    stream = stream or sys.stdout
    key = id(stream)
    if key not in _enabled:
        _enabled[key] = _detect(stream)
    return _enabled[key]


def set_colors(enabled: bool, stream=None) -> None:
    """Принудительно включает/выключает цвета для потока (для тестов и отладки)."""
    stream = stream or sys.stdout
    _enabled[id(stream)] = enabled


def paint(text: str, *codes: str, stream=None) -> str:
    """Красит текст, если для потока включены цвета; иначе возвращает как есть."""
    if not codes or not color_enabled(stream or sys.stdout):
        return text
    return "".join(codes) + text + _RESET


def paint_log(text: str, *codes: str) -> str:
    """Красит текст для строк лога (stderr, куда пишет logging)."""
    return paint(text, *codes, stream=sys.stderr)


def _sign_codes(value: float) -> tuple[str, ...]:
    if value > 0:
        return (BRIGHT_GREEN,)
    if value < 0:
        return (RED,)
    return ()


def fmt_signed(
    value: float,
    fmt: str = "{:+.2%}",
    stream=None,
    zero_neutral: bool = True,
) -> str:
    """Форматирует число со знаком и красит по знаку: плюс — зелёный, минус — красный."""
    text = fmt.format(value)
    codes = () if (zero_neutral and value == 0) else _sign_codes(value)
    return paint(text, *codes, stream=stream)


def fmt_signed_log(value: float, fmt: str = "{:+.2%}") -> str:
    return fmt_signed(value, fmt, stream=sys.stderr)


def fmt_money(value: float) -> str:
    """1 000 000.4 -> '1 000 000' (рубли, тонкий разделитель разрядов)."""
    return f"{value:,.0f}".replace(",", "\u2009" if color_enabled() else " ")


# ---------- Таблицы ----------

def _visible_len(text: str) -> int:
    """Длина строки без ANSI-кодов (для выравнивания уже окрашенных ячеек)."""
    return len(_ANSI_RE.sub("", text))


def render_table(
    headers: list[str],
    rows: list[list[str]],
    aligns: list[str] | None = None,
    paint_cell=None,
) -> str:
    """Моношрифтовая таблица с выравниванием.

    aligns: список 'l'/'r' по колонкам (по умолчанию всё вправо).
    paint_cell: callable(строка_таблицы, колонка, готовый_текст) -> окрашенный текст;
    вызывается уже после выравнивания, поэтому коды цветов не ломают ширину колонок.
    Ячейки, окрашенные заранее, тоже выравниваются корректно.
    """
    aligns = aligns or ["r"] * len(headers)
    cells = [[str(c) for c in row] for row in rows]
    widths = [_visible_len(str(h)) for h in headers]
    for row in cells:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], _visible_len(cell))

    def fmt_cell(cell: str, i: int) -> str:
        pad = " " * (widths[i] - _visible_len(cell))
        return (cell + pad) if aligns[i] == "l" else (pad + cell)

    def join(row_cells: list[str]) -> str:
        return " | ".join(fmt_cell(c, i) for i, c in enumerate(row_cells))

    lines = [
        paint(join([str(h) for h in headers]), BOLD),
        paint("-+-".join("-" * w for w in widths), DIM),
    ]
    for r, row in enumerate(cells):
        padded = [fmt_cell(c, i) for i, c in enumerate(row)]
        if paint_cell is not None and color_enabled():
            padded = [paint_cell(r, i, text) or text for i, text in enumerate(padded)]
        lines.append(" | ".join(padded))
    return "\n".join(lines)


def header(title: str, width: int = 64, stream=None) -> str:
    """'── Заголовок ──────────────' — цветной разделитель секций."""
    line = f"-- {title} ".ljust(width, "-")
    return paint(line, CYAN, BOLD, stream=stream)

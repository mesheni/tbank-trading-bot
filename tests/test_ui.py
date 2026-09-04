"""Тесты консольного UI: цвета, форматирование, таблицы."""
from __future__ import annotations

import pytest

import ui


@pytest.fixture(autouse=True)
def reset_color_state():
    ui._enabled.clear()
    yield
    ui._enabled.clear()


def test_paint_disabled_returns_plain():
    ui.set_colors(False)
    assert ui.paint("текст", ui.RED) == "текст"
    assert ui.paint("текст") == "текст"


def test_paint_enabled_wraps_with_ansi():
    ui.set_colors(True)
    out = ui.paint("текст", ui.RED)
    assert out.startswith("\033[91m") and out.endswith("\033[0m")
    assert "текст" in out


def test_fmt_signed_colors_by_sign():
    ui.set_colors(True)
    assert "\033[92m" in ui.fmt_signed(0.05)          # плюс — зелёный
    assert "\033[91m" in ui.fmt_signed(-0.05)         # минус — красный
    assert ui.fmt_signed(0.0) == "+0.00%"             # ноль нейтральный (по дизайну всегда)

    ui.set_colors(False)
    assert ui.fmt_signed(-0.05) == "-5.00%"


def test_fmt_money_thousands():
    ui.set_colors(False)
    assert ui.fmt_money(1_000_000.4) == "1 000 000"
    assert ui.fmt_money(0) == "0"
    assert ui.fmt_money(-5_000) == "-5 000"


def test_render_table_alignment():
    ui.set_colors(False)
    table = ui.render_table(
        ["модель", "sharpe"],
        [["lgbm", "+1.20"], ["naive_zero", "0.00"]],
        aligns=["l", "r"],
    )
    lines = table.splitlines()
    assert lines[0].strip().startswith("модель")
    assert set(lines[1]) <= {"-", "+", " "}
    # выравнивание: колонки разделены ' | ', текст слева, число справа
    data = [line.split(" | ") for line in lines]
    assert data[2][0].strip() == "lgbm"
    assert data[2][1].strip() == "+1.20"
    assert data[3][0].strip() == "naive_zero"
    assert all(len(ln) == len(lines[1]) for ln in lines)


def test_render_table_with_precolored_cells_aligns_by_visible_length():
    ui.set_colors(True)
    colored = ui.paint("+5.00%", ui.BRIGHT_GREEN)
    plain = "-5.00%"
    table = ui.render_table(["PnL"], [[colored], [plain]], aligns=["l"])
    lines = table.splitlines()
    import re

    visible = [re.sub(r"\033\[[0-9;]*m", "", ln) for ln in lines]
    # видимая длина всех строк таблицы одинакова, несмотря на ANSI-коды
    assert len({len(v) for v in visible}) == 1


def test_render_table_paint_cell_callback():
    ui.set_colors(True)
    table = ui.render_table(
        ["a", "b"],
        [["1", "2"]],
        paint_cell=lambda r, c, text: ui.paint(text, ui.BOLD) if c == 1 else text,
    )
    assert "\033[1m2\033[0m" in table
    assert "\033[1m1" not in table.replace("\033[1m2\033[0m", "")


def test_header_line():
    ui.set_colors(False)
    h = ui.header("ОТЧЁТ", width=40)
    assert h.startswith("-- ОТЧЁТ ")
    assert len(h) == 40

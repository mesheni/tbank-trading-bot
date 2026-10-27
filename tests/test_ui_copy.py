"""fmt_money: обычный пробел в разрядах, чтобы числа копировались без сюрпризов."""
from __future__ import annotations

import pytest

import ui


@pytest.fixture(autouse=True)
def _reset_colors():
    ui._enabled.clear()
    yield
    ui.set_colors(False)
    ui._enabled.clear()


@pytest.mark.parametrize("enabled", [True, False])
def test_fmt_money_uses_regular_space_with_any_color_mode(enabled):
    ui.set_colors(enabled)
    out = ui.fmt_money(1_000_000.4)
    assert out == "1 000 000"
    assert "\u2009" not in out


def test_fmt_money_negative_uses_regular_space():
    ui.set_colors(False)
    out = ui.fmt_money(-5_000)
    assert out == "-5 000"
    assert "\u2009" not in out

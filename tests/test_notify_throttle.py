"""Ключи троттлинга уведомлений: BUY и SELL одного тикера не сталкиваются."""
from __future__ import annotations

from notify import throttle_key


def test_throttle_key_format_includes_kind_action_and_ticker():
    assert throttle_key("BUY", "order-not-filled", "SBER") == "order-not-filled:BUY:SBER"


def test_buy_and_sell_keys_differ_for_same_ticker_and_kind():
    buy_key = throttle_key("BUY", "order-not-filled", "SBER")
    sell_key = throttle_key("SELL", "order-not-filled", "SBER")
    assert buy_key != sell_key


def test_all_namespace_dimensions_are_distinct():
    keys = {
        throttle_key("BUY", "order-not-filled", "SBER"),
        throttle_key("SELL", "order-not-filled", "SBER"),
        throttle_key("BUY", "order-not-filled", "GAZP"),
        throttle_key("BUY", "partial-fill", "SBER"),
        throttle_key("SELL", "partial-fill", "SBER"),
    }
    assert len(keys) == 5

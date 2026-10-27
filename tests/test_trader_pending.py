"""Trader: живая заявка по инструменту блокирует новую, пока не станет финальной."""
from __future__ import annotations

import pytest

from tbank.trader import FINAL_STATUSES, Trader


def order_state(
    order_id: str,
    status: str,
    lots_requested: int = 5,
    lots_executed: int = 0,
) -> dict:
    return {
        "order_id": order_id,
        "status": status,
        "lots_requested": lots_requested,
        "lots_executed": lots_executed,
        "avg_exec_price": 100.0 if status == "fill" else 0.0,
        "commission": 0.0,
        "total_amount": 0.0,
        "message": "",
    }


class FakeAPI:
    is_sandbox = True

    def __init__(self, post_status: str = "new"):
        self.post_status = post_status
        self.posts: list[dict] = []
        self.cancels: list[str] = []
        self.state_queue: list[dict] = []
        self.default_status = "partiallyfill"
        self.cancel_error: Exception | None = None
        self.state_error: Exception | None = None

    def get_accounts(self) -> list[dict]:
        return [{"id": "acc-1"}]

    def post_order(self, account_id, instrument_id, lots, direction, price=None) -> dict:
        self.posts.append(
            {
                "account_id": account_id,
                "instrument_id": instrument_id,
                "lots": lots,
                "direction": direction,
                "price": price,
            }
        )
        order_id = f"o{len(self.posts)}"
        executed = lots if self.post_status in FINAL_STATUSES else 0
        return order_state(order_id, self.post_status, lots_requested=lots, lots_executed=executed)

    def get_order_state(self, account_id, order_id) -> dict:
        if self.state_error is not None:
            raise self.state_error
        if self.state_queue:
            return self.state_queue.pop(0)
        return order_state(order_id, self.default_status)

    def cancel_order(self, account_id, order_id) -> dict:
        self.cancels.append(order_id)
        if self.cancel_error is not None:
            raise self.cancel_error
        return {}


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    import tbank.trader as trader_mod

    monkeypatch.setattr(trader_mod.time, "sleep", lambda seconds: None)


def make_trader(api: FakeAPI, tmp_path) -> Trader:
    return Trader(api, tmp_path / "journal.csv")


def leave_pending_order(api: FakeAPI, trader: Trader, instrument_id: str = "SBER-UID") -> dict:
    """Первая покупка застревает в partiallyfill, отмена падает — остаётся pending."""
    api.cancel_error = RuntimeError("cancel rejected")
    return trader.buy(instrument_id, 5, 100.0)


def test_cancel_failure_blocks_next_buy_for_same_instrument(tmp_path):
    api = FakeAPI()
    trader = make_trader(api, tmp_path)

    first = leave_pending_order(api, trader)

    assert first["status"] == "partiallyfill"
    assert trader._pending_orders == {"SBER-UID": "o1"}
    assert api.cancels == ["o1"]

    blocked = trader.buy("SBER-UID", 5, 101.0)

    assert blocked["status"] == "pending_unresolved"
    assert blocked["order_id"] == "o1"
    assert blocked["lots_requested"] == 5
    assert blocked["lots_executed"] == 0
    assert blocked["avg_exec_price"] == 0.0
    assert blocked["commission"] == 0.0
    assert blocked["total_amount"] == 0.0
    assert blocked["message"] == "previous order still open: o1"
    assert len(api.posts) == 1  # новая заявка не выставлялась


def test_final_state_clears_pending_and_next_buy_posts(tmp_path):
    api = FakeAPI()
    trader = make_trader(api, tmp_path)
    leave_pending_order(api, trader)
    assert "SBER-UID" in trader._pending_orders

    api.state_queue = [order_state("o1", "fill", lots_requested=5, lots_executed=5)]
    api.post_status = "fill"

    result = trader.buy("SBER-UID", 5, 101.0)

    assert result["status"] == "fill"
    assert "SBER-UID" not in trader._pending_orders
    assert [post["instrument_id"] for post in api.posts] == ["SBER-UID", "SBER-UID"]
    assert api.posts[1]["price"] == 101.0


def test_pending_order_does_not_block_other_instrument(tmp_path):
    api = FakeAPI()
    trader = make_trader(api, tmp_path)
    leave_pending_order(api, trader)

    api.post_status = "fill"
    result = trader.buy("GAZP-UID", 3, 200.0)

    assert result["status"] == "fill"
    assert [post["instrument_id"] for post in api.posts] == ["SBER-UID", "GAZP-UID"]
    assert trader._pending_orders == {"SBER-UID": "o1"}


def test_state_lookup_error_fails_closed(tmp_path):
    api = FakeAPI()
    trader = make_trader(api, tmp_path)
    leave_pending_order(api, trader)

    api.state_error = RuntimeError("network down")
    blocked = trader.buy("SBER-UID", 5, 101.0)

    assert blocked["status"] == "pending_unresolved"
    assert blocked["message"] == "previous order still open: o1"
    assert len(api.posts) == 1

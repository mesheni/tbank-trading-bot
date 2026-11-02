"""Trader: незавершённые заявки переживают перезапуск процесса (pending_orders.json)."""
from __future__ import annotations

import json

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


def make_trader(api: FakeAPI, directory) -> Trader:
    return Trader(api, directory / "journal.csv")


def leave_pending_order(api: FakeAPI, trader: Trader, instrument_id: str = "SBER-UID") -> dict:
    """Первая покупка застревает в partiallyfill, отмена падает — остаётся pending."""
    api.cancel_error = RuntimeError("cancel rejected")
    return trader.buy(instrument_id, 5, 100.0)


def pending_path(directory):
    return directory / "pending_orders.json"


def read_pending(directory) -> dict:
    return json.loads(pending_path(directory).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# (1) Характеризация текущего поведения: блокировка живёт в памяти процесса.
# Этот тест зелёный ДО добавления персиста.
# ---------------------------------------------------------------------------

def test_pending_order_blocks_only_same_instrument_in_memory(tmp_path):
    api = FakeAPI()
    trader = make_trader(api, tmp_path)

    first = leave_pending_order(api, trader)
    assert first["status"] == "partiallyfill"

    blocked = trader.buy("SBER-UID", 5, 101.0)
    assert blocked["status"] == "pending_unresolved"
    assert blocked["order_id"] == "o1"
    assert len(api.posts) == 1

    api.post_status = "fill"
    other = trader.buy("GAZP-UID", 3, 200.0)
    assert other["status"] == "fill"
    assert [post["instrument_id"] for post in api.posts] == ["SBER-UID", "GAZP-UID"]

# ---------------------------------------------------------------------------
# (2) Новое поведение D5: незавершённые заявки сохраняются на диск.
# ---------------------------------------------------------------------------

def test_unresolved_order_writes_pending_orders_file(tmp_path):
    api = FakeAPI()
    trader = make_trader(api, tmp_path)

    leave_pending_order(api, trader)

    assert read_pending(tmp_path) == {"SBER-UID": "o1"}


def test_restarted_trader_sees_pending_block(tmp_path):
    api = FakeAPI()
    trader = make_trader(api, tmp_path)
    leave_pending_order(api, trader)

    restarted_api = FakeAPI()
    restarted = make_trader(restarted_api, tmp_path)
    blocked = restarted.buy("SBER-UID", 5, 101.0)

    assert blocked["status"] == "pending_unresolved"
    assert blocked["order_id"] == "o1"
    assert blocked["message"] == "previous order still open: o1"
    assert restarted_api.posts == []


def test_corrupt_json_blocks_every_instrument(tmp_path):
    pending_path(tmp_path).write_text("{broken", encoding="utf-8")
    api = FakeAPI()
    trader = make_trader(api, tmp_path)

    for ticker in ("SBER-UID", "GAZP-UID", "ANY-UID"):
        result = trader.buy(ticker, 1, 10.0)
        assert result["status"] == "pending_unresolved"
        assert result["order_id"] == "corrupt-pending-file"

    assert api.posts == []


def test_non_dict_json_blocks_every_instrument(tmp_path):
    pending_path(tmp_path).write_text("[]", encoding="utf-8")
    api = FakeAPI()
    trader = make_trader(api, tmp_path)

    result = trader.buy("GAZP-UID", 1, 10.0)

    assert result["status"] == "pending_unresolved"
    assert result["order_id"] == "corrupt-pending-file"
    assert api.posts == []


def test_final_status_removes_entry_from_file(tmp_path):
    api = FakeAPI()
    trader = make_trader(api, tmp_path)
    leave_pending_order(api, trader)
    assert read_pending(tmp_path) == {"SBER-UID": "o1"}

    api.state_queue = [order_state("o1", "fill", lots_requested=5, lots_executed=5)]
    api.post_status = "fill"
    result = trader.buy("SBER-UID", 5, 101.0)

    assert result["status"] == "fill"
    assert read_pending(tmp_path) == {}


def test_file_matches_registry_after_each_mutation(tmp_path):
    api = FakeAPI()
    trader = make_trader(api, tmp_path)
    leave_pending_order(api, trader)
    assert read_pending(tmp_path) == {"SBER-UID": "o1"}

    api.cancel_error = RuntimeError("cancel rejected")
    second = trader.buy("GAZP-UID", 3, 200.0)

    assert second["status"] == "partiallyfill"
    assert trader._pending_orders == {"SBER-UID": "o1", "GAZP-UID": "o2"}
    assert read_pending(tmp_path) == {"SBER-UID": "o1", "GAZP-UID": "o2"}


def test_first_write_creates_missing_parent_dir(tmp_path):
    api = FakeAPI()
    reports = tmp_path / "nested" / "reports"
    trader = make_trader(api, reports)

    leave_pending_order(api, trader)

    assert read_pending(reports) == {"SBER-UID": "o1"}


def test_deleting_corrupt_file_clears_sentinel_for_new_trader(tmp_path):
    pending_path(tmp_path).write_text("{broken", encoding="utf-8")
    make_trader(FakeAPI(), tmp_path)
    pending_path(tmp_path).unlink()

    api = FakeAPI(post_status="fill")
    trader = make_trader(api, tmp_path)
    result = trader.buy("SBER-UID", 1, 10.0)

    assert result["status"] == "fill"
    assert [post["instrument_id"] for post in api.posts] == ["SBER-UID"]


def test_valid_file_rewrite_clears_corrupt_sentinel(tmp_path):
    pending_path(tmp_path).write_text("{broken", encoding="utf-8")
    make_trader(FakeAPI(), tmp_path)

    pending_path(tmp_path).write_text(json.dumps({"GAZP-UID": "o9"}), encoding="utf-8")
    api = FakeAPI(post_status="fill")
    trader = make_trader(api, tmp_path)

    blocked = trader.buy("GAZP-UID", 1, 10.0)
    assert blocked["status"] == "pending_unresolved"
    assert blocked["order_id"] == "o9"

    other = trader.buy("SBER-UID", 1, 10.0)
    assert other["status"] == "fill"
    assert [post["instrument_id"] for post in api.posts] == ["SBER-UID"]

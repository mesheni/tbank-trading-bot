"""MODE=real: отклонённая лимитка не должна превращаться в рыночную заявку."""
from __future__ import annotations

import pytest

from tbank.api import TBankAPI
from tbank.rest import APIError


class FakeClient:
    """HTTP-клиент с заранее заданной очередью ответов."""

    def __init__(self, mode: str, responses: list[object]):
        self.mode = mode
        self.host = (
            "https://sandbox-invest-public-api.tbank.ru"
            if mode == "sandbox"
            else "https://invest-public-api.tbank.ru"
        )
        self.responses = list(responses)
        self.posts: list[tuple[str, dict]] = []

    def post(self, service_method: str, payload: dict | None = None) -> dict:
        self.posts.append((service_method, dict(payload or {})))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def fill_response(order_id: str = "o1") -> dict:
    return {
        "orderId": order_id,
        "executionReportStatus": "EXECUTION_REPORT_STATUS_FILL",
        "lotsRequested": "1",
        "lotsExecuted": "1",
        "executedOrderPrice": {"units": "100", "nano": 0},
    }


def test_real_mode_rejected_limit_reraises_without_second_post():
    client = FakeClient("real", [APIError(400, "client error", "price too far")])
    api = TBankAPI(client, order_market_fallback=True)

    with pytest.raises(APIError):
        api.post_order("acc-1", "uid-sber", 1, "ORDER_DIRECTION_BUY", price=100.0)

    assert len(client.posts) == 1
    assert client.posts[0][1]["orderType"] == "ORDER_TYPE_LIMIT"


def test_real_mode_blocked_fallback_warns_only_once(caplog):
    client = FakeClient(
        "real",
        [APIError(400, "client error", "x"), APIError(400, "client error", "x")],
    )
    api = TBankAPI(client, order_market_fallback=True)

    for _ in range(2):
        with pytest.raises(APIError):
            api.post_order("acc-1", "uid-sber", 1, "ORDER_DIRECTION_BUY", price=100.0)

    warnings = [
        record
        for record in caplog.records
        if record.levelname == "WARNING" and "fallback" in record.getMessage()
    ]
    assert len(warnings) == 1
    assert len(client.posts) == 2  # по одному PostOrder на попытку, без рыночной переподачи


def test_sandbox_mode_still_falls_back_to_market():
    client = FakeClient("sandbox", [APIError(400, "client error", "x"), fill_response()])
    api = TBankAPI(client, order_market_fallback=True)

    state = api.post_order("acc-1", "uid-sber", 1, "ORDER_DIRECTION_BUY", price=100.0)

    assert state["status"] == "fill"
    assert len(client.posts) == 2
    assert client.posts[1][1]["orderType"] == "ORDER_TYPE_MARKET"
    assert "price" not in client.posts[1][1]


def test_rest_client_exposes_read_only_mode(monkeypatch):
    monkeypatch.setenv("TLS_INSECURE", "1")

    from tbank.rest import TBankRestClient

    client = TBankRestClient(token="dummy", mode="real")

    assert client.mode == "real"
    with pytest.raises(AttributeError):
        setattr(client, "mode", "sandbox")

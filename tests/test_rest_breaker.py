"""Circuit breaker TBankRestClient: серия ошибок приостанавливает запросы."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
import requests

import tbank.rest as rest_mod
from tbank.rest import APIError, TBankRestClient


class FakeTime:
    def __init__(self, now: float = 1_000.0):
        self.now = now
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)


@pytest.fixture
def fake_time(monkeypatch):
    fake = FakeTime()
    monkeypatch.setattr(rest_mod, "time", fake)
    return fake


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("TLS_INSECURE", "1")
    return TBankRestClient(token="dummy", mode="sandbox", min_request_interval=0.0)


def _response(status: int, payload: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        status_code=status,
        headers={},
        content=b"" if payload is None else b"{}",
        text="",
        json=lambda: payload or {},
    )


class PostStub:
    """Подменяет session.post: считает вызовы и отдаёт заданный статус."""

    def __init__(self, status: int = 500, payload: dict | None = None):
        self.status = status
        self.payload = payload
        self.calls = 0

    def __call__(self, *args, **kwargs):
        self.calls += 1
        return _response(self.status, self.payload)


def test_five_consecutive_500_open_breaker(client, fake_time, monkeypatch):
    stub = PostStub(500)
    monkeypatch.setattr(client.session, "post", stub)

    for _ in range(5):
        with pytest.raises(APIError) as exc:
            client.post("UsersService/GetUserInfo", {})
        assert exc.value.status == 500

    calls_before = stub.calls
    with pytest.raises(APIError) as exc:
        client.post("UsersService/GetUserInfo", {})
    assert exc.value.status == 503
    assert exc.value.message == "circuit open"
    assert "слишком много ошибок подряд" in exc.value.details
    assert stub.calls == calls_before  # открытый breaker не ходит в сеть


def test_after_cooldown_one_call_allowed_and_success_closes(client, fake_time, monkeypatch):
    stub = PostStub(500)
    monkeypatch.setattr(client.session, "post", stub)
    for _ in range(5):
        with pytest.raises(APIError):
            client.post("X/Y", {})
    with pytest.raises(APIError) as exc:
        client.post("X/Y", {})
    assert exc.value.status == 503

    fake_time.now += client.breaker_cooldown_sec + 1.0
    stub.status = 200
    stub.payload = {"ok": True}
    assert client.post("X/Y", {}) == {"ok": True}
    # успех закрыл breaker: следующий запрос тоже уходит в сеть
    assert client.post("X/Y", {}) == {"ok": True}


def test_after_cooldown_failure_reopens_breaker(client, fake_time, monkeypatch):
    stub = PostStub(500)
    monkeypatch.setattr(client.session, "post", stub)
    for _ in range(5):
        with pytest.raises(APIError):
            client.post("X/Y", {})

    fake_time.now += client.breaker_cooldown_sec + 1.0
    calls_before = stub.calls
    with pytest.raises(APIError) as exc:
        client.post("X/Y", {})
    assert exc.value.status == 500
    assert stub.calls > calls_before  # half-open: запрос был разрешён
    with pytest.raises(APIError) as exc:
        client.post("X/Y", {})
    assert exc.value.status == 503  # одна неудача снова открыла breaker


def test_http_400_does_not_count(client, fake_time, monkeypatch):
    stub = PostStub(400)
    monkeypatch.setattr(client.session, "post", stub)
    for _ in range(10):
        with pytest.raises(APIError) as exc:
            client.post("X/Y", {})
        assert exc.value.status == 400

    with pytest.raises(APIError) as exc:
        client.post("X/Y", {})
    assert exc.value.status == 400  # breaker не открылся
    assert stub.calls == 11


def test_network_error_after_retries_counts_as_failure(client, fake_time, monkeypatch):
    calls = {"n": 0}

    def failing_post(*args, **kwargs):
        calls["n"] += 1
        raise requests.ConnectionError("boom")

    monkeypatch.setattr(client.session, "post", failing_post)
    with pytest.raises(requests.ConnectionError):
        client.post("X/Y", {})
    assert calls["n"] == client.max_retries
    assert client._consecutive_failures == 1

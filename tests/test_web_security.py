"""T3: auth core — argon2 verify, constant-time credentials, trusted-proxy IP
resolution, per-IP login throttle, session idle policy, CSRF helpers.

The SQL-free suite pins the security contracts the FastAPI layer (T4/T7) relies on.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest
from argon2 import PasswordHasher
from starlette.middleware.sessions import SessionMiddleware

from web.security import (
    LoginThrottle,
    check_credentials,
    csrf_valid,
    new_csrf_token,
    parse_trusted_proxies,
    resolve_client_ip,
    session_expired,
    session_middleware_options,
    verify_password,
)

PASSWORD = "correct horse battery staple"
PASSWORD_HASH = PasswordHasher().hash(PASSWORD)


def _config(**overrides) -> SimpleNamespace:
    values = {
        "web_user": "admin",
        "web_password_hash": PASSWORD_HASH,
        "web_secret_key": "s" * 40,
        "web_session_idle_min": 30,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class Clock:
    """Injected monotonic clock so throttle windows never sleep."""

    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# --------------------------------------------------------------------------- argon2


def test_verify_password_accepts_correct_password() -> None:
    assert verify_password(PASSWORD_HASH, PASSWORD) is True


def test_verify_password_wrong_password_returns_false_without_raising() -> None:
    assert verify_password(PASSWORD_HASH, "wrong-password") is False


@pytest.mark.parametrize("bad_hash", ["", "not-a-hash", "$argon2id$broken", "$2b$12$abcdef"])
def test_verify_password_malformed_hash_returns_false(bad_hash: str) -> None:
    assert verify_password(bad_hash, PASSWORD) is False


def test_verify_password_never_logs_password(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG):
        verify_password(PASSWORD_HASH, PASSWORD)
        verify_password(PASSWORD_HASH, "wrong-password")
    assert PASSWORD not in caplog.text
    assert "wrong-password" not in caplog.text


# --------------------------------------------------------------------- credentials


def test_check_credentials_true_for_matching_user_and_password() -> None:
    assert check_credentials(_config(), "admin", PASSWORD) is True


def test_check_credentials_wrong_user_with_right_password_returns_false() -> None:
    assert check_credentials(_config(), "intruder", PASSWORD) is False


def test_check_credentials_wrong_password_with_right_user_returns_false() -> None:
    assert check_credentials(_config(), "admin", "wrong-password") is False


@pytest.mark.parametrize("username,password", [("", ""), ("", PASSWORD), ("admin", "")])
def test_check_credentials_empty_inputs_return_false(username: str, password: str) -> None:
    assert check_credentials(_config(), username, password) is False


def test_check_credentials_unconfigured_user_returns_false() -> None:
    config = _config(web_user="", web_password_hash="")
    assert check_credentials(config, "", "") is False


def test_check_credentials_verifies_password_even_when_user_is_wrong(monkeypatch) -> None:
    calls: list[str] = []

    def fake_verify(password_hash: str, password: str) -> bool:
        calls.append(password)
        return True

    monkeypatch.setattr("web.security.verify_password", fake_verify)
    assert check_credentials(_config(), "intruder", PASSWORD) is False
    assert calls == [PASSWORD]


# ------------------------------------------------------------------------ client ip


def test_resolve_client_ip_ignores_forwarded_for_from_untrusted_peer() -> None:
    assert resolve_client_ip("10.0.0.9", "1.2.3.4", {"127.0.0.1"}) == "10.0.0.9"
    assert resolve_client_ip("10.0.0.9", "1.2.3.4", set()) == "10.0.0.9"


def test_resolve_client_ip_uses_rightmost_untrusted_hop_for_trusted_peer() -> None:
    assert (
        resolve_client_ip("127.0.0.1", "1.2.3.4, 127.0.0.1", {"127.0.0.1"})
        == "1.2.3.4"
    )


def test_resolve_client_ip_walks_past_trusted_chain() -> None:
    trusted = {"127.0.0.1", "10.0.0.1"}
    header = "203.0.113.7, 10.0.0.1, 127.0.0.1"
    assert resolve_client_ip("127.0.0.1", header, trusted) == "203.0.113.7"


def test_resolve_client_ip_falls_back_to_peer_when_all_hops_trusted() -> None:
    trusted = {"127.0.0.1", "10.0.0.1"}
    assert resolve_client_ip("127.0.0.1", "10.0.0.1, 127.0.0.1", trusted) == "127.0.0.1"


@pytest.mark.parametrize("header", [None, "", "   ", ",,,"])
def test_resolve_client_ip_falls_back_to_peer_without_usable_header(header) -> None:
    assert resolve_client_ip("127.0.0.1", header, {"127.0.0.1"}) == "127.0.0.1"


def test_parse_trusted_proxies_splits_and_trims() -> None:
    assert parse_trusted_proxies(" 127.0.0.1, 10.0.0.1 ,,") == {"127.0.0.1", "10.0.0.1"}


@pytest.mark.parametrize("raw", ["", "   ", None])
def test_parse_trusted_proxies_empty_means_trust_nobody(raw) -> None:
    assert parse_trusted_proxies(raw) == set()


# ------------------------------------------------------------------------- throttle


def test_throttle_blocks_after_max_failures_within_window() -> None:
    clock = Clock()
    throttle = LoginThrottle(5, 900, clock=clock)

    for _ in range(4):
        throttle.record_failure("9.9.9.9")
        assert throttle.allow("9.9.9.9") is True

    throttle.record_failure("9.9.9.9")
    assert throttle.allow("9.9.9.9") is False


def test_throttle_other_ip_is_unaffected() -> None:
    clock = Clock()
    throttle = LoginThrottle(5, 900, clock=clock)
    for _ in range(5):
        throttle.record_failure("9.9.9.9")

    assert throttle.allow("9.9.9.9") is False
    assert throttle.allow("8.8.8.8") is True


def test_throttle_allows_again_after_window_passes() -> None:
    clock = Clock()
    throttle = LoginThrottle(5, 900, clock=clock)
    for _ in range(5):
        throttle.record_failure("9.9.9.9")
    assert throttle.allow("9.9.9.9") is False

    clock.advance(899)
    assert throttle.allow("9.9.9.9") is False

    clock.advance(2)
    assert throttle.allow("9.9.9.9") is True


def test_throttle_reset_clears_failures_for_ip() -> None:
    clock = Clock()
    throttle = LoginThrottle(5, 900, clock=clock)
    for _ in range(5):
        throttle.record_failure("9.9.9.9")

    throttle.reset("9.9.9.9")

    assert throttle.allow("9.9.9.9") is True


def test_throttle_window_is_sliding_not_fixed_bucket() -> None:
    clock = Clock()
    throttle = LoginThrottle(3, 100, clock=clock)
    throttle.record_failure("1.1.1.1")
    clock.advance(60)
    throttle.record_failure("1.1.1.1")
    clock.advance(30)
    throttle.record_failure("1.1.1.1")
    assert throttle.allow("1.1.1.1") is False

    clock.advance(11)  # first failure aged out, two remain
    assert throttle.allow("1.1.1.1") is True


# --------------------------------------------------------------------- session idle


def test_session_expired_boundary_is_not_expired() -> None:
    last_seen = 1_000.0
    assert session_expired(last_seen, 30, last_seen + 1_800) is False
    assert session_expired(last_seen, 30, last_seen + 1_801) is True


def test_session_expired_false_for_clock_skew_and_fresh_session() -> None:
    now = 1_000.0
    assert session_expired(now + 5, 30, now) is False
    assert session_expired(now, 30, now) is False
    assert session_expired(now, 0, now + 1) is True


# ----------------------------------------------------------------------------- csrf


def test_new_csrf_token_is_random_urlsafe() -> None:
    first = new_csrf_token()
    second = new_csrf_token()
    assert first != second
    assert len(first) >= 32
    assert all(ch.isalnum() or ch in "-_" for ch in first)


def test_csrf_valid_accepts_matching_token() -> None:
    token = new_csrf_token()
    assert csrf_valid(token, token) is True


@pytest.mark.parametrize(
    "expected,submitted",
    [(None, None), (None, "abc"), ("abc", None), ("", ""), ("", "abc"), ("abc", "")],
)
def test_csrf_valid_false_for_missing_or_empty(expected, submitted) -> None:
    assert csrf_valid(expected, submitted) is False


def test_csrf_valid_false_for_mismatch() -> None:
    assert csrf_valid(new_csrf_token(), new_csrf_token()) is False


# ------------------------------------------------------------ middleware options


def test_session_middleware_options_are_https_and_strict() -> None:
    config = _config()
    options = session_middleware_options(config)

    assert options == {
        "secret_key": config.web_secret_key,
        "https_only": True,
        "same_site": "strict",
        "max_age": None,
    }


def test_session_middleware_options_construct_real_middleware() -> None:
    async def app(scope, receive, send):  # pragma: no cover - never called
        return None

    middleware = SessionMiddleware(app=app, **session_middleware_options(_config()))
    assert middleware is not None

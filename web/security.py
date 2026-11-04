"""T3: auth core for the web panel.

Argon2id password verification, constant-time credential check, trusted-proxy
client-IP resolution, a shared in-memory login throttle, the session idle policy
and CSRF helpers. Nothing here logs or returns a password, hash or secret.

The FastAPI layer (T4) builds ``SessionMiddleware(**session_middleware_options(
config))`` and reuses one ``LoginThrottle`` instance for both login and re-auth.
"""
from __future__ import annotations

import hmac
import secrets
import threading
import time
from collections.abc import Callable
from typing import Protocol

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

_PASSWORD_HASHER = PasswordHasher()

#: Safety valve for the throttle memory: keys are attacker-influenced behind a proxy.
_MAX_TRACKED_IPS = 10_000


class AuthConfig(Protocol):
    """The WEB_* config surface consumed here (config.Config at runtime)."""

    web_user: str
    web_password_hash: str
    web_secret_key: str


def verify_password(password_hash: str, password: str) -> bool:
    """Return True only for a well-formed argon2 hash matching ``password``.

    Malformed hashes (including an empty one) and mismatches are reported as
    False instead of raising; the password value itself is never logged.
    """
    if not password_hash:
        return False
    try:
        return _PASSWORD_HASHER.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHashError, VerificationError):
        return False


def check_credentials(config: AuthConfig, username: str, password: str) -> bool:
    """Verify both the username and the password, in constant time.

    The password hash is always verified, even when the username is wrong, so
    response timing does not reveal whether the username exists.
    """
    expected_user = config.web_user or ""
    username_ok = bool(expected_user) and hmac.compare_digest(
        (username or "").encode("utf-8"), expected_user.encode("utf-8")
    )
    password_ok = verify_password(config.web_password_hash or "", password or "")
    return bool(username_ok and password_ok)


def parse_trusted_proxies(raw: str | None) -> set[str]:
    """Split WEB_TRUSTED_PROXIES (comma separated) into a trimmed set."""
    if not raw:
        return set()
    return {item.strip() for item in raw.split(",") if item.strip()}


def resolve_client_ip(
    peer_host: str,
    forwarded_for: str | None,
    trusted_proxies: set[str],
) -> str:
    """Resolve the real client IP, honouring X-Forwarded-For only behind a trusted peer.

    An untrusted peer gets its own address and the header is ignored entirely.
    For a trusted peer, walk the X-Forwarded-For chain right-to-left and return
    the first hop that is not itself a trusted proxy; fall back to the peer.
    """
    if not peer_host or peer_host not in trusted_proxies:
        return peer_host
    if not forwarded_for:
        return peer_host
    hops = [hop.strip() for hop in forwarded_for.split(",") if hop.strip()]
    for hop in reversed(hops):
        if hop not in trusted_proxies:
            return hop
    return peer_host


class LoginThrottle:
    """Thread-safe sliding-window failure counter, one window per IP.

    A single instance is shared by login and password re-entry; both use
    ``allow`` before verifying and ``record_failure`` after a bad password.
    """

    def __init__(
        self,
        max_attempts: int,
        window_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be > 0")
        self._max_attempts = max_attempts
        self._window_seconds = float(window_seconds)
        self._clock = clock
        self._failures: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def allow(self, ip: str) -> bool:
        """True while the IP has fewer than ``max_attempts`` failures in the window."""
        with self._lock:
            return len(self._prune_locked(ip, self._clock())) < self._max_attempts

    def record_failure(self, ip: str) -> None:
        with self._lock:
            now = self._clock()
            stamps = self._prune_locked(ip, now)
            stamps.append(now)
            self._failures[ip] = stamps
            if len(self._failures) > _MAX_TRACKED_IPS:
                self._drop_stale_locked(now)

    def reset(self, ip: str) -> None:
        """Forget all failures for ``ip`` (used after a successful login/re-auth)."""
        with self._lock:
            self._failures.pop(ip, None)

    def _prune_locked(self, ip: str, now: float) -> list[float]:
        cutoff = now - self._window_seconds
        stamps = [stamp for stamp in self._failures.get(ip, ()) if stamp > cutoff]
        if stamps:
            self._failures[ip] = stamps
        else:
            self._failures.pop(ip, None)
        return stamps

    def _drop_stale_locked(self, now: float) -> None:
        cutoff = now - self._window_seconds
        stale = [
            ip
            for ip, stamps in self._failures.items()
            if not stamps or max(stamps) <= cutoff
        ]
        for ip in stale:
            self._failures.pop(ip, None)


def session_expired(last_seen_epoch: float, idle_minutes: int, now: float) -> bool:
    """True once the session sat idle longer than ``idle_minutes``.

    Exactly ``idle_minutes`` of inactivity is still valid; one second more expires.
    Clock skew (last seen in the future) is treated as not expired.
    """
    idle_seconds = max(int(idle_minutes), 0) * 60
    return now - last_seen_epoch > idle_seconds


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def csrf_valid(expected: str | None, submitted: str | None) -> bool:
    """Constant-time CSRF comparison; a missing/empty token on either side is invalid."""
    if not expected or not submitted:
        return False
    return hmac.compare_digest(expected.encode("utf-8"), submitted.encode("utf-8"))


def session_middleware_options(config: AuthConfig) -> dict[str, object]:
    """Starlette SessionMiddleware kwargs: HTTPS-only, strict cookie, browser-session lifetime.

    Verified against Starlette 1.10 docs: secret_key, max_age, same_site,
    https_only are the real parameter names; HttpOnly is always set by
    SessionMiddleware itself.
    """
    return {
        "secret_key": config.web_secret_key,
        "https_only": True,
        "same_site": "strict",
        "max_age": None,
    }

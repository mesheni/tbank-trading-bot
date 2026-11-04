"""T4: FastAPI app factory for the read-only web panel.

Routes: login/logout plus the HTML dashboard and two read-only JSON endpoints.
The password-gated control routes (T7) are installed from
:mod:`web.control_routes`; this module itself never writes reports and never
talks to the trading API. Anonymous HTML requests redirect to /login, anonymous
API requests get 401. Every response carries no-store + anti-framing +
no-referrer + CSP headers, and all HTML is assembled with ``html.escape`` in
:mod:`web.views`.
"""
from __future__ import annotations

import time
from functools import lru_cache
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.middleware.sessions import SessionMiddleware

from web import views
from web.control_routes import install_control_routes
from web.security import (
    LoginThrottle,
    check_credentials,
    csrf_valid,
    new_csrf_token,
    parse_trusted_proxies,
    resolve_client_ip,
    session_expired,
    session_middleware_options,
)
from web.snapshot import build_snapshot, read_log_lines

#: Patchable wall clock used for every session timestamp (tests replace it).
_now = time.time

SESSION_USER = "user"
SESSION_LAST_SEEN = "last_seen"
SESSION_CSRF = "csrf"
SESSION_FLASH = "flash"

#: Applied to every response by ``_security_headers`` middleware.
_SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'"
    ),
}

_DASHBOARD_JS_PATH = Path(__file__).resolve().parent / "static" / "dashboard.js"


@lru_cache(maxsize=1)
def _dashboard_js() -> str:
    """The external dashboard script (CSP forbids inline script)."""
    try:
        return _DASHBOARD_JS_PATH.read_text(encoding="utf-8")
    except OSError:  # pragma: no cover - packaging accident, page still renders
        return "/* dashboard.js missing */"


def _peer_ip(request: Request, trusted_proxies: set[str]) -> str:
    """Peer address, or the untrusted X-Forwarded-For hop behind a trusted proxy."""
    peer_host = request.client.host if request.client is not None else ""
    return resolve_client_ip(
        peer_host, request.headers.get("x-forwarded-for"), trusted_proxies
    )


def _session_csrf(request: Request) -> str:
    """Current CSRF token, issued into the session when absent."""
    token = request.session.get(SESSION_CSRF)
    if not isinstance(token, str) or not token:
        token = new_csrf_token()
        request.session[SESSION_CSRF] = token
    return token


def _is_authenticated(request: Request) -> bool:
    """Session check with idle expiry; refreshes ``last_seen`` on success."""
    config = request.app.state.config
    session = request.session
    if not session.get(SESSION_USER):
        return False
    last_seen = session.get(SESSION_LAST_SEEN)
    now = _now()
    if not isinstance(last_seen, (int, float)) or session_expired(
        float(last_seen), int(config.web_session_idle_min), now
    ):
        session.clear()
        return False
    session[SESSION_LAST_SEEN] = now
    return True


def _forbidden(message: str) -> HTMLResponse:
    return HTMLResponse(views.error_page(message), status_code=403)


def create_app(config) -> FastAPI:
    """Build the web panel application around one ``Config`` instance."""
    app = FastAPI(
        title="tbank-trading-bot web panel",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.config = config
    app.state.login_throttle = LoginThrottle(
        max_attempts=int(config.web_login_max_attempts),
        window_seconds=float(config.web_login_window_min) * 60.0,
    )
    trusted_proxies = parse_trusted_proxies(config.web_trusted_proxies)

    @app.middleware("http")
    async def _security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.update(_SECURITY_HEADERS)
        return response

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request) -> HTMLResponse:
        return HTMLResponse(views.login_page(_session_csrf(request)))

    @app.post("/login")
    def login_submit(
        request: Request,
        username: str = Form(...),
        password: str = Form(...),
        csrf_token: str = Form(...),
    ):
        if not csrf_valid(request.session.get(SESSION_CSRF), csrf_token):
            return _forbidden("неверный CSRF-токен")

        throttle: LoginThrottle = request.app.state.login_throttle
        client_ip = _peer_ip(request, trusted_proxies)
        if not throttle.allow(client_ip):
            return HTMLResponse(
                views.error_page("слишком много попыток входа, попробуйте позже"),
                status_code=429,
            )

        if not check_credentials(config, username, password):
            throttle.record_failure(client_ip)
            return HTMLResponse(
                views.login_page(_session_csrf(request), "неверный логин или пароль"),
                status_code=401,
            )

        throttle.reset(client_ip)
        request.session.clear()
        request.session[SESSION_USER] = config.web_user
        request.session[SESSION_LAST_SEEN] = _now()
        request.session[SESSION_CSRF] = new_csrf_token()
        return RedirectResponse(url="/", status_code=303)

    @app.post("/logout")
    def logout(request: Request, csrf_token: str = Form("")):
        if not _is_authenticated(request):
            request.session.clear()
            return RedirectResponse(url="/login", status_code=303)
        if not csrf_valid(request.session.get(SESSION_CSRF), csrf_token):
            return _forbidden("неверный CSRF-токен")
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request):
        if not _is_authenticated(request):
            return RedirectResponse(url="/login", status_code=303)
        flash = request.session.pop(SESSION_FLASH, None)
        snapshot = build_snapshot(config)
        return HTMLResponse(
            views.dashboard_page(
                config,
                snapshot,
                _session_csrf(request),
                flash=flash if isinstance(flash, str) else None,
            )
        )

    @app.get("/api/state")
    def api_state(request: Request):
        if not _is_authenticated(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse(build_snapshot(config))

    @app.get("/api/log")
    def api_log(request: Request):
        if not _is_authenticated(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse({"log": read_log_lines(config)})

    @app.get("/static/dashboard.js")
    def dashboard_script() -> Response:
        return Response(content=_dashboard_js(), media_type="text/javascript")

    # T7: POST-only, password re-entry routes (GET on them stays 405).
    install_control_routes(app, config, trusted_proxies)

    # Added last: SessionMiddleware wraps the routes so ``request.session`` is
    # available everywhere, and it appends Set-Cookie after the inner response.
    app.add_middleware(SessionMiddleware, **session_middleware_options(config))
    return app

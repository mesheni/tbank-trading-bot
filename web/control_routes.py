"""T7: password re-entry control routes for the web panel.

Three POST endpoints — ``/control/stop``, ``/control/start`` and
``/control/kill-reset`` — each require an authenticated session, a CSRF token
and the account password typed again. The password is checked with
``verify_password`` and throttled by the same ``LoginThrottle`` instance as
``POST /login`` (keyed by the same resolved client IP, refused with 429 before
the password is even looked at). On success the matching T5 service is called
in a worker thread (it polls the daemon ack) and its ``ControlResult.message``
is flashed into the session for the next dashboard render. The route layer
never interprets the result: refusals, timeouts and errors pass through
verbatim. No GET handler exists for these paths, so GET answers 405.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from web import control as web_control
from web import views
from web.security import csrf_valid, verify_password

#: Generic refusals: they reveal nothing about the user or the password hash.
REAUTH_DENIED = "повторный ввод пароля неверен"
THROTTLE_DENIED = "слишком много попыток входа, попробуйте позже"


@dataclass(frozen=True, slots=True)
class _ReauthForm:
    """The two form fields both gates need (never logged or stored)."""

    password: str
    csrf_token: str


def install_control_routes(app: FastAPI, config, trusted_proxies: set[str]) -> None:
    """Register the three control routes on ``app``.

    The T5 service is looked up by attribute at request time, so tests can
    monkeypatch ``web.control.request_*`` without rebuilding the application.
    """
    # Deferred import: web.app imports this module while defining create_app.
    from web import app as web_app

    async def _handle(request: Request, action: str, form: _ReauthForm):
        if not web_app._is_authenticated(request):
            return RedirectResponse(url="/login", status_code=303)

        if not csrf_valid(request.session.get(web_app.SESSION_CSRF), form.csrf_token):
            return web_app._forbidden("неверный CSRF-токен")

        throttle = request.app.state.login_throttle
        client_ip = web_app._peer_ip(request, trusted_proxies)
        if not throttle.allow(client_ip):
            return HTMLResponse(views.error_page(THROTTLE_DENIED), status_code=429)

        if not verify_password(config.web_password_hash or "", form.password or ""):
            throttle.record_failure(client_ip)
            return web_app._forbidden(REAUTH_DENIED)

        throttle.reset(client_ip)
        user = str(request.session.get(web_app.SESSION_USER) or "")
        result = await asyncio.to_thread(
            getattr(web_control, action),
            config,
            True,
            user=user,
            client_ip=client_ip,
        )
        # One-shot flash; the dashboard pops and escapes it on the next render.
        request.session[web_app.SESSION_FLASH] = result.message
        return RedirectResponse(url="/", status_code=303)

    @app.post("/control/stop")
    async def control_stop(
        request: Request,
        password: str = Form(""),
        csrf_token: str = Form(""),
    ):
        return await _handle(request, "request_stop", _ReauthForm(password, csrf_token))

    @app.post("/control/start")
    async def control_start(
        request: Request,
        password: str = Form(""),
        csrf_token: str = Form(""),
    ):
        return await _handle(request, "request_start", _ReauthForm(password, csrf_token))

    @app.post("/control/kill-reset")
    async def control_kill_reset(
        request: Request,
        password: str = Form(""),
        csrf_token: str = Form(""),
    ):
        return await _handle(
            request, "request_kill_switch_reset", _ReauthForm(password, csrf_token)
        )

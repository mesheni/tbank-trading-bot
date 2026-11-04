"""T7: password-gated control routes (stop/start/kill-reset) on the web panel.

FastAPI TestClient only: no real daemon, no systemctl, no network. The stop
flow runs the real T5 service with a fake ack writer (the same pattern as
``tests/test_web_control.py``), so the route's pass-through of the
``ControlResult`` and the audit line are exercised end to end. Start and
kill-reset wiring is checked with the T5 functions monkeypatched at the module
level.
"""
from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

import control
import tui.controls as tui_controls
import web.control as web_control
from config import Config
from web.app import create_app
from web.control import ControlResult

PASSWORD = "testpass12345"
PASSWORD_HASH = PasswordHasher().hash(PASSWORD)
SECRET_KEY = "t7-test-secret-key-" + "x" * 40
GENERIC_REAUTH_ERROR = "повторный ввод пароля неверен"

CSRF_RE = re.compile(r'name="csrf_token"[^>]*value="([^"]+)"')

CONTROL_PATHS = ("/control/stop", "/control/start", "/control/kill-reset")


def make_config(tmp_path: Path, **overrides) -> Config:
    values: dict = {
        "token": "",
        "smtp_password": "",
        "web_user": "admin",
        "web_password_hash": PASSWORD_HASH,
        "web_secret_key": SECRET_KEY,
        "web_trusted_proxies": "",
        "web_session_idle_min": 30,
        "web_login_max_attempts": 5,
        "web_login_window_min": 15,
        "reports_dir": tmp_path / "reports",
        "log_file": tmp_path / "daemon.log",
        "tui_refresh_sec": 5.0,
    }
    values.update(overrides)
    return Config(**values)


def make_client(config: Config) -> TestClient:
    # HTTPS base URL: SessionMiddleware sets Secure; httpx only sends such
    # cookies over an https origin (a browser behaves the same way).
    return TestClient(create_app(config), base_url="https://testserver")


def hidden_csrf(html_text: str) -> str:
    match = CSRF_RE.search(html_text)
    assert match, "CSRF token missing from HTML form"
    return match.group(1)


def login_csrf(client: TestClient) -> str:
    response = client.get("/login")
    assert response.status_code == 200
    return hidden_csrf(response.text)


def post_login(client: TestClient, token: str, password: str = PASSWORD):
    return client.post(
        "/login",
        data={"username": "admin", "password": password, "csrf_token": token},
        follow_redirects=False,
    )


def login(client: TestClient) -> str:
    token = login_csrf(client)
    response = post_login(client, token)
    assert response.status_code == 303
    # The dashboard carries the CSRF token of the authenticated session.
    dashboard = client.get("/")
    assert dashboard.status_code == 200
    return hidden_csrf(dashboard.text)


def write_running(reports: Path, **overrides) -> None:
    payload = {"pid": 4242, "state": "running", "mode": "sandbox", "iteration": 1}
    payload.update(overrides)
    reports.mkdir(parents=True, exist_ok=True)
    control.write_status(reports, payload)
    (reports / "heartbeat").write_text(
        dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), encoding="utf-8"
    )


def command_files(reports: Path) -> list[Path]:
    directory = reports / "commands"
    return sorted(directory.glob("*.json")) if directory.exists() else []


def command_names(reports: Path) -> list[str]:
    return [
        json.loads(path.read_text(encoding="utf-8"))["name"]
        for path in command_files(reports)
    ]


def audit_lines(reports: Path) -> list[dict]:
    path = reports / "web_audit.jsonl"
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def fake_ack_on_enqueue(monkeypatch, *, stop_state: bool = True) -> list[str]:
    """Fake daemon: write the ack and the observed state right after enqueue."""
    real_enqueue = control.enqueue_command
    seen: list[str] = []

    def enqueue(reports_dir, name: str) -> str:
        command_id = real_enqueue(reports_dir, name)
        acks_dir = control.paths(reports_dir)["acks"]
        acks_dir.mkdir(parents=True, exist_ok=True)
        (acks_dir / f"{command_id}.json").write_text(
            json.dumps({"ok": True, "detail": ""}), encoding="utf-8"
        )
        if stop_state and name == "stop":
            control.write_status(
                reports_dir, {"state": "stopped", "iteration": 2, "mode": "sandbox"}
            )
        seen.append(command_id)
        return command_id

    monkeypatch.setattr(web_control.control, "enqueue_command", enqueue)
    return seen


def post_control(client: TestClient, path: str, token: str, password: str | None = PASSWORD):
    data: dict = {"csrf_token": token}
    if password is not None:
        data["password"] = password
    return client.post(path, data=data, follow_redirects=False)


# ---------------------------------------------------------------- re-entry gate


def test_post_stop_without_password_is_403_and_enqueues_nothing(tmp_path):
    config = make_config(tmp_path)
    client = make_client(config)
    token = login(client)

    response = post_control(client, "/control/stop", token, password=None)

    assert response.status_code == 403
    assert GENERIC_REAUTH_ERROR in response.text
    assert command_files(config.reports_dir) == []


def test_post_stop_with_wrong_password_is_generic_403(tmp_path):
    config = make_config(tmp_path)
    client = make_client(config)
    token = login(client)

    response = post_control(client, "/control/stop", token, password="wrong-password")

    assert response.status_code == 403
    assert GENERIC_REAUTH_ERROR in response.text
    assert "wrong-password" not in response.text
    assert command_files(config.reports_dir) == []


def test_wrong_password_counts_toward_shared_throttle_and_sixth_is_429(tmp_path):
    config = make_config(tmp_path)
    write_running(config.reports_dir)
    client = make_client(config)
    token = login(client)

    statuses = [
        post_control(client, "/control/stop", token, password=f"bad-{index}").status_code
        for index in range(5)
    ]
    sixth = post_control(client, "/control/stop", token, password=PASSWORD)

    assert statuses == [403] * 5
    assert sixth.status_code == 429
    assert client.app.state.login_throttle.allow("testclient") is False
    assert command_files(config.reports_dir) == []


def test_control_shares_throttle_with_login(tmp_path):
    """Five bad re-auths exhaust the window shared with POST /login."""
    config = make_config(tmp_path)
    client = make_client(config)
    token = login(client)

    for index in range(5):
        assert post_control(client, "/control/stop", token, password=f"bad-{index}").status_code == 403

    login_page = client.post(
        "/login",
        data={"username": "admin", "password": "bad", "csrf_token": token},
        follow_redirects=False,
    )

    assert login_page.status_code == 429


# --------------------------------------------------------------- happy path


def test_correct_password_stop_enqueues_once_and_flashes_result(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    write_running(config.reports_dir)
    acked = fake_ack_on_enqueue(monkeypatch)
    client = make_client(config)
    token = login(client)

    response = post_control(client, "/control/stop", token)

    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert command_names(config.reports_dir) == ["stop"]
    assert len(command_files(config.reports_dir)) == 1

    audit = audit_lines(config.reports_dir)
    assert len(audit) == 1
    assert audit[0]["action"] == "stop"
    assert audit[0]["result"] == "ok"
    assert audit[0]["command_id"] == acked[0]
    assert audit[0]["user"] == "admin"
    assert audit[0]["client_ip"] == "testclient"
    assert PASSWORD not in (config.reports_dir / "web_audit.jsonl").read_text(encoding="utf-8")

    dashboard = client.get("/")
    assert dashboard.status_code == 200
    assert tui_controls.STOPPED in dashboard.text

    # One-shot: the flash is consumed by the first dashboard render.
    assert tui_controls.STOPPED not in client.get("/").text


def test_no_daemon_stop_is_refused_not_reported_as_success(tmp_path, monkeypatch):
    """Stale/absent daemon: the route passes the T5 refusal through, no fake ok."""
    config = make_config(tmp_path)
    client = make_client(config)
    token = login(client)

    response = post_control(client, "/control/stop", token)
    dashboard = client.get("/")

    assert response.status_code == 303
    assert tui_controls.DAEMON_NOT_RUNNING in dashboard.text
    assert tui_controls.STOPPED not in dashboard.text
    assert command_files(config.reports_dir) == []
    assert audit_lines(config.reports_dir)[0]["result"] == "refused"


# ------------------------------------------------------- all three actions wired


@pytest.mark.parametrize(
    ("path", "function_name", "message"),
    [
        ("/control/stop", "request_stop", "stop-fake"),
        ("/control/start", "request_start", "start-fake"),
        ("/control/kill-reset", "request_kill_switch_reset", "kill-reset-fake"),
    ],
)
def test_each_control_route_calls_its_service_with_reentry(
    tmp_path, monkeypatch, path, function_name, message
):
    config = make_config(tmp_path)
    write_running(config.reports_dir)
    calls: list[dict] = []

    def fake(config_arg, reauth_ok, *, user="", client_ip=""):
        calls.append(
            {"config": config_arg, "reauth_ok": reauth_ok, "user": user, "client_ip": client_ip}
        )
        return ControlResult(True, message, None, "ok")

    monkeypatch.setattr(web_control, function_name, fake)
    client = make_client(config)
    token = login(client)

    response = post_control(client, path, token)
    dashboard = client.get("/")

    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert len(calls) == 1
    assert calls[0]["reauth_ok"] is True
    assert calls[0]["user"] == "admin"
    assert calls[0]["client_ip"] == "testclient"
    assert message in dashboard.text


def test_flash_message_is_html_escaped(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    write_running(config.reports_dir)
    hostile = "<script>alert('flash')</script>"
    monkeypatch.setattr(
        web_control,
        "request_stop",
        lambda config_arg, reauth_ok, *, user="", client_ip="": ControlResult(
            True, hostile, None, "ok"
        ),
    )
    client = make_client(config)
    token = login(client)

    assert post_control(client, "/control/stop", token).status_code == 303
    body = client.get("/").text

    assert "&lt;script&gt;alert(&#x27;flash&#x27;)&lt;/script&gt;" in body
    assert "<script>alert('flash')" not in body


# ------------------------------------------------------------------ CSRF / GET


def test_missing_csrf_is_403_and_enqueues_nothing(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    write_running(config.reports_dir)
    fake_ack_on_enqueue(monkeypatch)
    client = make_client(config)
    login(client)

    response = client.post(
        "/control/stop", data={"password": PASSWORD}, follow_redirects=False
    )

    assert response.status_code == 403
    assert command_files(config.reports_dir) == []


def test_wrong_csrf_is_403_and_enqueues_nothing(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    write_running(config.reports_dir)
    fake_ack_on_enqueue(monkeypatch)
    client = make_client(config)
    login(client)

    response = post_control(client, "/control/stop", "definitely-not-the-token")

    assert response.status_code == 403
    assert command_files(config.reports_dir) == []


@pytest.mark.parametrize("path", CONTROL_PATHS)
def test_get_control_paths_is_405(tmp_path, path):
    config = make_config(tmp_path)
    client = make_client(config)

    response = client.get(path, follow_redirects=False)

    assert response.status_code == 405


def test_unauthenticated_post_redirects_to_login_and_does_nothing(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    write_running(config.reports_dir)
    acked = fake_ack_on_enqueue(monkeypatch)
    client = make_client(config)

    response = post_control(client, "/control/stop", "whatever")

    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    assert acked == []
    assert command_files(config.reports_dir) == []
    assert audit_lines(config.reports_dir) == []

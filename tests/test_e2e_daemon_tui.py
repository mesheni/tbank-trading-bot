"""E2E smoke (todo 11): real `cli.py daemon run` subprocess + TUI under Pilot.

Marked ``e2e`` and skipped unless ``TBANK_E2E=1``; the full suite stays offline
by default. Only file-level facts are asserted (status JSON, kill-switch file,
ack files, lock/pid removal, process count) so a green UI cannot mask a broken
control channel.

Temp dirs are the pytest ``tmp_path`` (under %TEMP% on Windows); every poll has
a hard deadline and the daemon is killed in ``finally`` on any failure. The
failure case launches a child shim with a raising fake API вЂ” test-only runtime
patch, no product code changes.
"""
from __future__ import annotations

import asyncio
import csv
import json
import os
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import NamedTuple

import pytest
from textual.widgets import Button, RichLog, Static

import control
import tui.controls as tui_controls
from config import Config
from tui.app import TbankApp
from tui.controls import ConfirmScreen

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "cli.py"

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        os.getenv("TBANK_E2E") != "1",
        reason="e2e: real daemon subprocess; set TBANK_E2E=1 to run",
    ),
]

STARTUP_TIMEOUT = 15.0  # interpreter + pandas import + first daemon iteration
COMMAND_TIMEOUT = 10.0  # TUI command -> daemon drain -> ack
STOP_TIMEOUT = 15.0  # TUI stop -> clean process exit
POLL = 0.2


def _env_overrides(tmp_path: Path) -> dict[str, str]:
    """Single source of truth shared by the daemon subprocess and the TUI Config."""
    return {
        "T_INVEST_TOKEN": "e2e-token", "MODE": "sandbox", "CONFIRM_REAL_TRADING": "",
        "TICKERS": "SBER", "CANDLE_INTERVAL": "hour", "FORECAST_HORIZON": "1",
        # LOOP_INTERVAL_SEC is parsed as int (config._env_int): "0.2" would
        # silently fall back to 300. One second + a 50 ms tick stays responsive.
        "LOOP_INTERVAL_SEC": "1", "DAEMON_TICK_SEC": "0.05", "TUI_REFRESH_SEC": "0.05",
        "TBANK_FAKE_API": "1", "NLP_EMBEDDER": "0", "NLP_SENTIMENT": "lexicon",
        "NOTIFY_EMAIL_TO": "", "ACCOUNT_BUDGET_RUB": "100000", "SANDBOX_INITIAL_RUB": "100000",
        "DB_PATH": str(tmp_path / "market.sqlite"), "MODELS_DIR": str(tmp_path / "models"),
        "REPORTS_DIR": str(tmp_path / "reports"), "LOG_FILE": str(tmp_path / "daemon.log"),
    }


def _tui_config(monkeypatch, tmp_path: Path) -> Config:
    """Config for the in-process TUI; built from the same env as the daemon."""
    monkeypatch.delenv("TUI_SYSTEMD_MANAGED", raising=False)  # s/x must stay active
    for name, value in _env_overrides(tmp_path).items():
        monkeypatch.setenv(name, value)
    return Config()


def _tail(path: Path, limit: int = 40) -> str:
    try:
        return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:])
    except OSError:
        return "<no daemon output>"


def _python_pids() -> set[int]:
    """PIDs of python images (psutil is forbidden): tasklist on Windows, pgrep on POSIX."""
    if os.name != "nt":
        # POSIX fallback (/proc alternative: scan /proc/<pid>/cmdline for "python").
        out = subprocess.run(["pgrep", "-f", "python"], capture_output=True, text=True, check=False).stdout
        return {int(part) for part in out.split() if part.isdigit()}
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq python.exe", "/FO", "CSV", "/NH"],
                         capture_output=True, text=True, timeout=30, check=False).stdout
    return {int(row[1]) for row in csv.reader(out.splitlines())
            if len(row) >= 2 and row[0].strip().lower() == "python.exe" and row[1].strip().isdigit()}


class _Daemon(NamedTuple):
    proc: subprocess.Popen
    out_path: Path


async def _await_for(predicate, timeout: float, what: str, diagnostics=None) -> None:
    """Hard-deadline poll: raises AssertionError on timeout, never hangs."""
    deadline = time.monotonic() + timeout
    last_error: object = None
    while True:
        try:
            if predicate():
                return
        except (OSError, ValueError) as exc:
            last_error = exc
        if time.monotonic() >= deadline:
            detail = f"; diagnostics:\n{diagnostics()}" if diagnostics is not None else ""
            raise AssertionError(f"{what}: not satisfied within {timeout:g}s (last_error={last_error!r}){detail}")
        await asyncio.sleep(POLL)


def _text(app: TbankApp, selector: str) -> str:
    return str(app.query_one(selector, Static).render())


def _ack_names(reports: Path) -> set[str]:
    return {
        json.loads(path.read_text(encoding="utf-8")).get("name")
        for path in control.paths(reports)["acks"].glob("*.json")
    }


def _running_status(daemon: _Daemon, reports: Path) -> bool:
    code = daemon.proc.poll()
    if code is not None:
        raise AssertionError(f"daemon exited early rc={code}; output:\n{_tail(daemon.out_path)}")
    payload = control.read_status(reports)
    return (
        isinstance(payload, dict) and not payload.get("corrupt")
        and payload.get("state") == "running" and int(payload.get("iteration") or 0) >= 1
    )


@contextmanager
def _daemon_process(tmp_path: Path, argv: list[str]):
    """Start the daemon, guarantee kill + stream close + temp cleanup."""
    out_path = tmp_path / "daemon.out"
    stream = open(out_path, "ab")
    env = os.environ.copy()
    env.pop("TUI_SYSTEMD_MANAGED", None)  # a leaked systemd flag would silence s/x
    env.update(_env_overrides(tmp_path))
    proc = subprocess.Popen(argv, cwd=str(ROOT), env=env, stdin=subprocess.DEVNULL,
                            stdout=stream, stderr=subprocess.STDOUT)
    try:
        yield _Daemon(proc, out_path)
    finally:
        if proc.poll() is None:
            proc.kill()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
        stream.close()
        if os.getenv("TBANK_E2E_KEEP_TMP") != "1":
            shutil.rmtree(tmp_path, ignore_errors=True)


async def _confirm(app: TbankApp, pilot) -> None:
    """Press the confirm button of the currently open modal."""
    await pilot.pause()
    assert isinstance(app.screen, ConfirmScreen), f"expected confirm modal, got {app.screen!r}"
    app.screen.query_one("#confirm", Button).press()
    await pilot.pause(0.3)


async def _stop_via_tui(app: TbankApp, pilot, daemon: _Daemon) -> None:
    await pilot.press("x")
    await _confirm(app, pilot)
    await _await_for(
        lambda: daemon.proc.poll() is not None, STOP_TIMEOUT,
        "daemon did not exit after TUI x+confirm", lambda: _tail(daemon.out_path),
    )
    daemon.proc.wait(timeout=5)
    assert daemon.proc.returncode == 0, f"daemon rc={daemon.proc.returncode}: {_tail(daemon.out_path)}"
    await _await_for(
        lambda: tui_controls.STOPPED in _text(app, "#status"),
        COMMAND_TIMEOUT, "TUI did not observe the stop ack",
    )


def _assert_stopped(reports: Path, expected_acks: set[str]) -> dict:
    final = control.read_status(reports)
    assert final is not None and not final.get("corrupt")
    assert final["state"] == "stopped" and int(final["iteration"]) >= 1
    assert list(control.paths(reports)["commands"].glob("*.json")) == []
    assert _ack_names(reports) == expected_acks
    assert not control.paths(reports)["lock"].exists(), "daemon.lock not removed"
    assert not control.paths(reports)["pid"].exists(), "daemon.pid not removed"
    return final


def _assert_no_process_leak(daemon: _Daemon, before: set[int]) -> None:
    """Ни одного НОВОГО python-процесса; чужие процессы, жившие до теста, не мешают."""
    def no_leak() -> bool:
        current = _python_pids()
        return daemon.proc.pid not in current and current - before == set()

    asyncio.run(_await_for(
        no_leak, STOP_TIMEOUT,
        f"python process leak: before={sorted(before)} after={sorted(_python_pids())}",
    ))


@pytest.mark.parametrize("_attempt", [1, 2], ids=["first", "repeat"])
def test_tui_controls_real_daemon_end_to_end(tmp_path, monkeypatch, _attempt):
    # Running twice in one pytest session proves the finally-kill path leaves
    # no daemon behind (the second run's process baseline equals the first's).
    reports = tmp_path / "reports"
    reports.mkdir(parents=True)
    kill_switch = reports / "kill_switch.json"
    kill_switch.write_text(json.dumps({"triggered_at": "2026-01-01T00:00:00+00:00", "reason": "e2e pre-seed"}), encoding="utf-8")
    before = _python_pids()
    app = TbankApp(_tui_config(monkeypatch, tmp_path))
    with _daemon_process(tmp_path, [sys.executable, str(CLI), "daemon", "run"]) as daemon:
        async def scenario() -> None:
            async with app.run_test(size=(140, 45)) as pilot:
                await _await_for(
                    lambda: _running_status(daemon, reports), STARTUP_TIMEOUT,
                    "daemon_status.json did not reach state=running, iteration>=1",
                    lambda: _tail(daemon.out_path),
                )
                # TUI refreshes on its own interval: wait for its render instead
                # of assuming one frame is enough (mount may precede the file).
                await _await_for(
                    lambda: "running" in _text(app, "#daemon"), STARTUP_TIMEOUT,
                    "TUI card did not refresh to running", lambda: _tail(daemon.out_path),
                )
                card = _text(app, "#daemon")
                assert "running" in card, f"badge/card is not running: {card!r}"
                assert "kill-switch" in card, f"kill-switch line missing: {card!r}"
                startup = control.read_status(reports)
                assert startup is not None and startup["kill_switch_active"] is True

                # k: reset kill-switch through the command channel
                await pilot.press("k")
                await _confirm(app, pilot)
                await _await_for(
                    lambda: not kill_switch.exists(), COMMAND_TIMEOUT,
                    "kill_switch.json was not cleared by the daemon after TUI k+confirm",
                    lambda: _tail(daemon.out_path),
                )
                await _await_for(
                    lambda: tui_controls.KILL_SWITCH_RESET_CONFIRMED in _text(app, "#status"),
                    COMMAND_TIMEOUT, "TUI did not observe the kill_switch_reset ack",
                )
                await _stop_via_tui(app, pilot, daemon)

        asyncio.run(scenario())

        # File-level truth (UI text alone is not trusted) + no leaked process.
        assert not kill_switch.exists()
        final = _assert_stopped(reports, {"kill_switch_reset", "stop"})
        assert final["kill_switch_active"] is False
        _assert_no_process_leak(daemon, before)


# Child-process shim: a raising fake API without touching product code.
# update_news swallows get_news errors (bot.py:385-392), so get_portfolio raises
# once iteration>=1 (inside run_forever's per-iteration try -> _daemon_event(error=...))
# and the in-session branch is forced so the result never depends on exchange hours.
_FAILING_DAEMON_CODE = r"""
import os, sys
from pathlib import Path

import bot, cli, control

_reports = Path(os.environ["REPORTS_DIR"])
_original_get_portfolio = bot.FakeTBankAPI.get_portfolio


def _failing_get_portfolio(self, account_id):
    if int((control.read_status(_reports) or {}).get("iteration") or 0) >= 1:
        raise RuntimeError("fake api failure")
    return _original_get_portfolio(self, account_id)


bot.FakeTBankAPI.get_portfolio = _failing_get_portfolio
bot.TradingBot.in_trading_session = lambda self, now=None: True
sys.argv = ["cli.py", "daemon", "run"]
raise SystemExit(cli.main())
"""


def test_daemon_iteration_error_is_published_and_visible_to_tui(tmp_path, monkeypatch):
    """Failure path: raising fake API -> last_error in status; TUI survives and shows it."""
    reports = tmp_path / "reports"
    reports.mkdir(parents=True)
    before = _python_pids()
    app = TbankApp(_tui_config(monkeypatch, tmp_path))
    with _daemon_process(tmp_path, [sys.executable, "-c", _FAILING_DAEMON_CODE]) as daemon:
        async def scenario() -> None:
            async with app.run_test(size=(140, 45)) as pilot:
                await _await_for(
                    lambda: _running_status(daemon, reports), STARTUP_TIMEOUT,
                    "daemon did not publish state=running", lambda: _tail(daemon.out_path),
                )
                await _await_for(
                    lambda: "fake api failure" in str((control.read_status(reports) or {}).get("last_error") or ""),
                    STARTUP_TIMEOUT, "daemon did not publish last_error=fake api failure",
                    lambda: _tail(daemon.out_path),
                )
                await _await_for(
                    lambda: "running" in _text(app, "#daemon"), STARTUP_TIMEOUT,
                    "TUI card did not refresh to running", lambda: _tail(daemon.out_path),
                )
                await _await_for(
                    lambda: "fake api failure" in "\n".join(
                        strip.text for strip in app.query_one("#log", RichLog).lines),
                    COMMAND_TIMEOUT, "TUI #log panel does not show the iteration error",
                    lambda: _tail(daemon.out_path),
                )
                await _stop_via_tui(app, pilot, daemon)

        asyncio.run(scenario())

        final = _assert_stopped(reports, {"stop"})
        assert "fake api failure" in str(final["last_error"] or "")
        _assert_no_process_leak(daemon, before)

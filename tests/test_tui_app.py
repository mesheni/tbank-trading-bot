"""tui.app: Pilot-сценарии read-only дашборда и реальный SVG-артефакт.

pytest-asyncio в окружении нет, поэтому каждый сценарий — sync-тест,
оборачивающий Textual Pilot через asyncio.run. Ожидание обновления — pilot.pause(),
без time.sleep и без привязки к wall-clock в утверждениях.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
from pathlib import Path

import pytest
from textual.widgets import Button, DataTable, RichLog, Static

import tui.app as tui_app
from config import Config
from tui.app import TbankApp

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_SVG = ROOT / ".omo" / "evidence" / "task-5-cli-ux-ui-improvements.svg"


def _write(reports: Path, name: str, text: str) -> None:
    path = reports / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _write_status(reports: Path, **overrides) -> None:
    payload = {
        "pid": 4242,
        "state": "running",
        "started_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "mode": "sandbox",
        "iteration": 3,
        "equity_rub": 1234.5,
        "cash_rub": 1000.0,
        "positions_count": 2,
        "kill_switch_active": False,
        "kill_switch_reason": None,
        "last_error": None,
    }
    payload.update(overrides)
    _write(reports, "daemon_status.json", json.dumps(payload, ensure_ascii=False))


def _write_fresh_heartbeat(reports: Path) -> None:
    _write(reports, "heartbeat", dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"))


def _write_journal(reports: Path, count: int = 2) -> None:
    lines = ["time,ticker,action,lots,price,reason,order_id"]
    for index in range(count):
        lines.append(f"2026-01-01T10:00:{index:02d}+00:00,SBER,BUY,1,300.0,test,o{index}")
    _write(reports, "journal.csv", "\n".join(lines) + "\n")


def _config(tmp_path: Path, refresh: float = 0.02) -> Config:
    config = Config()
    config.reports_dir = tmp_path
    config.log_file = None
    config.tui_refresh_sec = refresh
    return config


def _text(app: TbankApp, selector: str) -> str:
    return str(app.query_one(selector, Static).render())


def _status(app: TbankApp) -> str:
    return _text(app, "#status")


def _log_lines(app: TbankApp) -> list[str]:
    return [strip.text for strip in app.query_one("#log", RichLog).lines]


def test_app_renders_running_daemon_and_journal(tmp_path):
    _write_status(tmp_path)
    _write_fresh_heartbeat(tmp_path)
    _write_journal(tmp_path, count=2)
    app = TbankApp(_config(tmp_path))

    async def scenario() -> None:
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            card = _text(app, "#daemon")
            assert "running" in card
            assert "итерация: 3" in card
            assert "1,234.50" in card
            assert "1,000.00" in card
            assert "позиций: 2" in card
            table = app.query_one("#journal", DataTable)
            assert table.row_count == 2
            assert "SBER" in str(table.get_row_at(0))
            assert "BUY" in str(table.get_row_at(0))
            assert "SANDBOX" in _text(app, "#mode")

    asyncio.run(scenario())


def test_app_fresh_status_without_heartbeat_shows_running(tmp_path, monkeypatch):
    """Todo-7 gap: вне сессии демон не пишет heartbeat, но статус свежий."""
    monkeypatch.setattr(tui_app, "_VAR_DIR", tmp_path / "var")
    _write_status(tmp_path)  # updated_at = now
    assert not (tmp_path / "heartbeat").exists()
    app = TbankApp(_config(tmp_path))

    async def scenario() -> None:
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            assert "ДЕМОН: running" in _text(app, "#daemon")

    asyncio.run(scenario())


def test_app_refreshes_after_status_rewrite(tmp_path):
    _write_status(tmp_path, state="running", iteration=1, equity_rub=None, cash_rub=None)
    _write_fresh_heartbeat(tmp_path)
    app = TbankApp(_config(tmp_path))

    async def scenario() -> None:
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            assert "итерация: 1" in _text(app, "#daemon")
            assert "капитал: --" in _text(app, "#daemon")

            _write_status(tmp_path, state="stopped", iteration=9)
            await pilot.pause(0.2)  # несколько interval-тиков refresh

            card = _text(app, "#daemon")
            assert "итерация: 9" in card
            assert "состояние: stopped" in card

    asyncio.run(scenario())


def test_app_real_mode_banner_is_red_bold(tmp_path):
    config = _config(tmp_path)
    config.mode = "real"
    assert TbankApp(config)._mode_banner() == "[bold red] РЕЖИМ: REAL [/]"

    config.mode = "sandbox"
    assert TbankApp(config)._mode_banner() == " РЕЖИМ: SANDBOX "


def test_app_empty_reports_dir_is_offline_without_crash(tmp_path, monkeypatch):
    monkeypatch.setattr(tui_app, "_VAR_DIR", tmp_path / "var")
    app = TbankApp(_config(tmp_path))

    async def scenario() -> None:
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            card = _text(app, "#daemon")
            assert "offline" in card
            assert "последняя сделка: сделок нет" in card
            assert app.query_one("#journal", DataTable).row_count == 0
            assert "лог не найден" in "\n".join(_log_lines(app))

    asyncio.run(scenario())


def test_app_renders_positions_and_session_pnl(tmp_path):
    _write_status(
        tmp_path,
        positions=[
            {"ticker": "SBER", "lots": 10, "price": 110.0, "pnl_rub": 100.0},
            {"ticker": "GAZP", "lots": 2, "price": 250.0, "pnl_rub": -10.0},
        ],
    )
    _write_fresh_heartbeat(tmp_path)
    _write(
        tmp_path,
        "equity_live.csv",
        "time,total_rub,cash_rub,positions_rub\n"
        "2026-01-01T10:00:00+00:00,1000,900,100\n"
        "2026-01-01T11:00:00+00:00,1012,900,112\n",
    )
    app = TbankApp(_config(tmp_path))

    async def scenario() -> None:
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            table = app.query_one("#positions", DataTable)
            assert table.row_count == 2
            assert "SBER" in str(table.get_row_at(0))
            assert "GAZP" in str(table.get_row_at(1))
            assert app.query_one("#positions-empty", Static).display is False
            assert "P&L сессии: +12 ₽ (+1.2%)" in _text(app, "#daemon")

    asyncio.run(scenario())


def test_app_non_utf8_heartbeat_does_not_crash(tmp_path):
    (tmp_path / "heartbeat").write_bytes(bytes([255, 254]))
    app = TbankApp(_config(tmp_path))

    async def scenario() -> None:
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            assert "offline" in _text(app, "#daemon")
            assert "нет данных (вне сессии)" in _text(app, "#positions-empty")

    asyncio.run(scenario())


# ---------- log panel and last trade (todo 9) ----------

def test_app_log_panel_shows_last_30_lines_of_config_log(tmp_path, monkeypatch):
    monkeypatch.setattr(tui_app, "_VAR_DIR", tmp_path / "var")
    _write_status(tmp_path)
    log_path = tmp_path / "app.log"
    log_path.write_text("".join(f"line {index:03d}\n" for index in range(40)), encoding="utf-8")
    config = _config(tmp_path)
    config.log_file = log_path
    app = TbankApp(config)

    async def scenario() -> None:
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            log = app.query_one("#log", RichLog)
            lines = [strip.text for strip in log.lines]
            assert lines[0].startswith("ЛОГ:")
            assert str(log_path) in lines[0]
            body = lines[1:]
            assert len(body) == 30
            assert body[0] == "line 010"
            assert body[-1] == "line 039"
            # Хвост длиннее видимой области: панель проскроллена до конца,
            # новая строка (line 039) реально достижима, а не только в render().
            assert log.max_scroll_y > 0
            assert log.scroll_y == log.max_scroll_y

    asyncio.run(scenario())


def test_app_log_panel_falls_back_to_var_daemon_log(tmp_path, monkeypatch):
    var_dir = tmp_path / "var"
    var_dir.mkdir()
    (var_dir / "daemon.log").write_text("daemon log line\n", encoding="utf-8")
    (var_dir / "daemon.out").write_text("daemon out line\n", encoding="utf-8")
    monkeypatch.setattr(tui_app, "_VAR_DIR", var_dir)
    _write_status(tmp_path)
    app = TbankApp(_config(tmp_path))

    async def scenario() -> None:
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            text = "\n".join(_log_lines(app))
            assert "daemon log line" in text
            assert "daemon out line" not in text

    asyncio.run(scenario())


def test_app_log_panel_falls_back_to_daemon_out(tmp_path, monkeypatch):
    var_dir = tmp_path / "var"
    var_dir.mkdir()
    (var_dir / "daemon.out").write_text("daemon out line\n", encoding="utf-8")
    monkeypatch.setattr(tui_app, "_VAR_DIR", var_dir)
    _write_status(tmp_path)
    app = TbankApp(_config(tmp_path))

    async def scenario() -> None:
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            assert "daemon out line" in "\n".join(_log_lines(app))

    asyncio.run(scenario())


def test_app_log_panel_does_not_parse_rich_markup(tmp_path, monkeypatch):
    monkeypatch.setattr(tui_app, "_VAR_DIR", tmp_path / "var")
    _write_status(tmp_path)
    log_path = tmp_path / "app.log"
    log_path.write_text("[INFO] started [broken\n[red]not markup\n", encoding="utf-8")
    config = _config(tmp_path)
    config.log_file = log_path
    app = TbankApp(config)

    async def scenario() -> None:
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            text = "\n".join(_log_lines(app))
            assert "[INFO] started [broken" in text
            assert "[red]not markup" in text

    asyncio.run(scenario())


def test_app_shows_last_trade_age(tmp_path, monkeypatch):
    monkeypatch.setattr(tui_app, "_VAR_DIR", tmp_path / "var")
    _write_status(tmp_path)
    stamp = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=2)).isoformat(
        timespec="seconds"
    )
    _write(
        tmp_path,
        "journal.csv",
        "time,ticker,action,lots,price,reason,order_id\n"
        f"{stamp},SBER,BUY,1,300.0,test,o1\n",
    )
    app = TbankApp(_config(tmp_path))

    async def scenario() -> None:
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            assert "последняя сделка: 2 ч назад" in _text(app, "#daemon")

    asyncio.run(scenario())


# ---------- control-channel error guards (verifier should-fix from todo 7) ----------

@pytest.mark.parametrize("exc_type", [OSError, ValueError])
def test_enqueue_error_shows_status_and_keeps_app_alive(tmp_path, monkeypatch, exc_type):
    monkeypatch.setattr(tui_app, "_VAR_DIR", tmp_path / "var")
    _write_status(tmp_path)

    def boom(*args, **kwargs):
        raise exc_type("disk full")

    monkeypatch.setattr(tui_app.control, "enqueue_command", boom)
    app = TbankApp(_config(tmp_path))

    async def scenario() -> None:
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.press("x")
            await pilot.pause()
            app.screen.query_one("#confirm", Button).press()
            await pilot.pause(0.2)
            assert "ошибка" in _status(app)
            assert app.query_one("#daemon", Static) is not None

    asyncio.run(scenario())


def test_ack_poll_error_shows_status_and_keeps_app_alive(tmp_path, monkeypatch):
    monkeypatch.setattr(tui_app, "_VAR_DIR", tmp_path / "var")
    _write_status(tmp_path)

    def boom(reports_dir, command_id):
        raise OSError("ack read failed")

    monkeypatch.setattr(tui_app, "_ack_received", boom)
    app = TbankApp(_config(tmp_path))

    async def scenario() -> None:
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.press("x")
            await pilot.pause()
            app.screen.query_one("#confirm", Button).press()
            await pilot.pause(0.3)
            assert "ошибка" in _status(app)
            assert app.query_one("#daemon", Static) is not None

    asyncio.run(scenario())


def test_kill_switch_clear_error_shows_status_and_keeps_app_alive(tmp_path, monkeypatch):
    monkeypatch.setattr(tui_app, "_VAR_DIR", tmp_path / "var")

    def boom(reports_dir):
        raise OSError("read-only reports")

    monkeypatch.setattr(tui_app.bot, "clear_kill_switch", boom)
    app = TbankApp(_config(tmp_path))

    async def scenario() -> None:
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.press("k")
            await pilot.pause()
            app.screen.query_one("#confirm", Button).press()
            await pilot.pause(0.2)
            assert "ошибка" in _status(app)
            assert app.query_one("#daemon", Static) is not None

    asyncio.run(scenario())


def test_app_exports_svg_evidence(tmp_path):
    _write_status(
        tmp_path,
        positions=[
            {"ticker": "SBER", "lots": 10, "price": 110.0, "pnl_rub": 100.0},
            {"ticker": "GAZP", "lots": 2, "price": 250.0, "pnl_rub": -10.0},
        ],
    )
    _write_fresh_heartbeat(tmp_path)
    _write_journal(tmp_path, count=2)
    _write(
        tmp_path,
        "equity_live.csv",
        "time,total_rub,cash_rub,positions_rub\n"
        "2026-01-01T10:00:00+00:00,1000,900,100\n"
        "2026-01-01T11:00:00+00:00,1012,900,112\n",
    )
    app = TbankApp(_config(tmp_path))

    async def scenario() -> str:
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            return app.export_screenshot()

    svg = asyncio.run(scenario())
    assert svg.lstrip().startswith("<svg")
    EVIDENCE_SVG.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE_SVG.write_text(svg, encoding="utf-8")
    assert EVIDENCE_SVG.exists()
    assert EVIDENCE_SVG.stat().st_size > 500

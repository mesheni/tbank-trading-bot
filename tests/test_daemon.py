"""D1: headless-демон — tick-цикл, lock, статус, команды, REAL-гейт.

Блок «Характеризация» фиксирует поведение run_forever ДО реструктуризации сна
и проходит на неизменённом коде. Блок «Новое поведение» написан red-first.
"""
from __future__ import annotations

import json
import sys
import threading
import time
from types import SimpleNamespace

import pytest

import bot as bot_module
import cli
import control
from bot import FakeTBankAPI, TradingBot, run_headless
from config import Config
from strategy import PortfolioState


def make_config(tmp_path, **kwargs) -> Config:
    defaults = dict(
        token="t",
        mode="sandbox",
        tickers=["SBER"],
        db_path=tmp_path / "market.sqlite",
        models_dir=tmp_path / "artifacts",
        reports_dir=tmp_path / "reports",
        log_file=tmp_path / "daemon.log",
        loop_interval_sec=0.01,
        daemon_tick_sec=0.01,
        nlp_embedder=False,
        nlp_sentiment="lexicon",
        notify_email_to="",
        account_budget_rub=100_000.0,
        sandbox_initial_rub=100_000.0,
    )
    return Config(**{**defaults, **kwargs})


def make_fake_bot(config) -> TradingBot:
    return TradingBot(config, api=FakeTBankAPI(config.tickers))


# =====================================================================
# (1) Характеризация: heartbeat пишется ровно раз на итерацию
# =====================================================================

def test_run_forever_writes_heartbeat_once_per_iteration(monkeypatch, tmp_path):
    """Текущее поведение: N итераций -> N heartbeat (pin до изменения sleep-структуры)."""
    heartbeats: list[object] = []
    monkeypatch.setattr(bot_module, "write_heartbeat", lambda path: heartbeats.append(path))

    bot = TradingBot.__new__(TradingBot)
    bot.config = SimpleNamespace(
        loop_interval_sec=0,
        reports_dir=tmp_path,
        mode="sandbox",
        candle_interval="hour",
        forecast_horizon=1,
        budget_rub=100_000.0,
    )
    bot.instruments = {"SBER": {"ticker": "SBER", "figi": "BBG_TEST", "lot": 1}}
    bot._budget_rub = 100_000.0
    bot._entries_allowed = True
    bot._running = False
    bot.trader = SimpleNamespace(
        initialize_balance=lambda budget: None,
        portfolio=lambda: {"total_amount_rub": 100_000.0, "cash_rub": 100_000.0, "positions": {}},
    )
    bot._setup_capital_profile = lambda: None
    bot.in_trading_session = lambda: True
    bot.update_news = lambda: None
    bot._load_portfolio = lambda: PortfolioState(cash=100_000.0, equity=100_000.0, positions={})
    bot._check_kill_switch = lambda equity: None
    bot.step_ticker = lambda ticker, instrument, portfolio, entries_allowed: None
    bot.conn = SimpleNamespace(close=lambda: None)
    bot.notifier = SimpleNamespace(send_throttled=lambda *a, **k: None, send=lambda *a, **k: None)

    bot.run_forever(max_iterations=3)

    assert len(heartbeats) == 3


# =====================================================================
# (2) Новое поведение (red-first)
# =====================================================================

def test_daemon_run_writes_status_and_stops_after_max_iterations(tmp_path):
    config = make_config(tmp_path)
    bot = make_fake_bot(config)

    assert run_headless(bot, max_iterations=3, reports_dir=config.reports_dir) == 0

    payload = control.read_status(config.reports_dir)
    assert payload is not None and not payload.get("corrupt")
    assert payload["state"] == "stopped"
    assert payload["iteration"] >= 3
    assert payload["mode"] == "sandbox"
    assert payload["pid"] is not None


def test_status_written_every_iteration_with_distinct_snapshots(tmp_path, monkeypatch):
    """Статус пишется после КАЖДОЙ итерации: iteration растёт, updated_at и equity меняются."""
    config = make_config(tmp_path, loop_interval_sec=0.001, daemon_tick_sec=0.001)

    class TickingAPI(FakeTBankAPI):
        def __init__(self, tickers):
            super().__init__(tickers)
            self.calls = 0

        def get_portfolio(self, account_id):
            self.calls += 1
            cash = 100_000.0 + self.calls
            return {"positions": {}, "total_amount_rub": cash, "cash_rub": cash}

    bot = TradingBot(config, api=TickingAPI(config.tickers))
    bot.in_trading_session = lambda: True  # детерминированно: не зависим от времени МСК

    real_write = control.write_status
    timestamps = iter(range(1000))
    monkeypatch.setattr(
        control, "_utc_now", lambda: f"2026-01-01T00:00:{next(timestamps):02d}+00:00"
    )
    recorded: list[dict] = []

    def capture(reports_dir, payload):
        real_write(reports_dir, payload)
        recorded.append(json.loads(control.paths(reports_dir)["status"].read_text(encoding="utf-8")))

    monkeypatch.setattr(control, "write_status", capture)

    assert run_headless(bot, max_iterations=3, reports_dir=config.reports_dir) == 0

    running = [row for row in recorded if row["state"] == "running" and row["iteration"] >= 1]
    assert {row["iteration"] for row in running} >= {1, 2, 3}
    assert len({row["updated_at"] for row in running}) == len(running)  # updated_at растёт
    with_snapshot = [row for row in running if row["equity_rub"] is not None]
    assert len({row["equity_rub"] for row in with_snapshot}) > 1  # разные снимки портфеля


def test_stop_command_ends_loop_within_a_few_ticks(tmp_path):
    config = make_config(tmp_path, loop_interval_sec=5.0, daemon_tick_sec=0.02)
    bot = make_fake_bot(config)
    timer = threading.Timer(0.15, lambda: control.enqueue_command(config.reports_dir, "stop"))
    timer.start()
    started = time.monotonic()
    try:
        rc = run_headless(bot, max_iterations=None, reports_dir=config.reports_dir)
    finally:
        timer.cancel()
    elapsed = time.monotonic() - started

    assert rc == 0
    assert elapsed < 2.0  # 5-секундный «сон» разбит на тики, стоп ловится за 2-3 тика
    payload = control.read_status(config.reports_dir)
    assert payload["state"] == "stopped"
    acks = list(control.paths(config.reports_dir)["acks"].glob("*.json"))
    assert len(acks) == 1


def test_kill_switch_reset_command_clears_file_and_restores_entries(tmp_path):
    config = make_config(tmp_path)
    config.reports_dir.mkdir(parents=True)
    bot_module._write_kill_switch(config.reports_dir, {"triggered_at": "2026-01-01T00:00:00+00:00"})
    control.enqueue_command(config.reports_dir, "kill_switch_reset")
    bot = make_fake_bot(config)

    assert bot._entries_allowed is False

    assert run_headless(bot, max_iterations=2, reports_dir=config.reports_dir) == 0

    assert not bot_module.kill_switch_path(config.reports_dir).exists()
    assert bot._entries_allowed is True
    payload = control.read_status(config.reports_dir)
    assert payload["kill_switch_active"] is False


def test_second_daemon_instance_refused_by_lock(tmp_path):
    reports = tmp_path / "reports"
    assert control.acquire_lock(reports) is True
    try:
        assert run_headless(object(), max_iterations=1, reports_dir=reports) == 3
    finally:
        control.release_lock(reports)


def test_cli_daemon_run_second_instance_exits_3(tmp_path, monkeypatch, capsys):
    config = make_config(tmp_path)
    monkeypatch.setattr(cli.control, "acquire_lock", lambda reports_dir: False)

    assert cli.cmd_daemon_run(config, max_iterations=1) == 3

    assert "уже запущен" in capsys.readouterr().err.lower()


def test_daemon_run_real_without_confirm_exits_2_and_builds_no_bot(tmp_path, monkeypatch, capsys):
    config = make_config(tmp_path, mode="real")
    config.confirm_real_trading = False
    built: list[str] = []
    monkeypatch.setattr(bot_module, "TradingBot", lambda *a, **k: built.append("bot"))

    assert cli.cmd_daemon_run(config, max_iterations=1) == 2

    assert built == []
    assert "CONFIRM_REAL_TRADING=YES" in capsys.readouterr().err


def test_daemon_status_without_state_says_not_running(tmp_path, capsys):
    config = make_config(tmp_path)

    assert cli.cmd_daemon_status(config) == 0

    out = capsys.readouterr().out
    assert "SANDBOX" in out  # баннер режима первой строкой
    assert "демон не запущен" in out.lower()


def test_daemon_status_with_corrupt_state_is_documented_not_crashing(tmp_path, capsys):
    config = make_config(tmp_path)
    config.reports_dir.mkdir(parents=True)
    control.paths(config.reports_dir)["status"].write_text("{broken", encoding="utf-8")

    assert cli.cmd_daemon_status(config) == 0

    out = capsys.readouterr().out.lower()
    assert "демон не запущен" in out or "поврежд" in out


def test_daemon_status_prints_snapshot_fields(tmp_path, capsys):
    config = make_config(tmp_path)
    control.write_status(
        config.reports_dir,
        {"state": "running", "iteration": 7, "mode": "sandbox", "pid": 4321, "last_error": "boom"},
    )

    assert cli.cmd_daemon_status(config) == 0

    out = capsys.readouterr()
    combined = out.out + out.err
    assert "running" in combined
    assert "7" in combined and "4321" in combined
    assert "boom" in combined


def test_daemon_stop_times_out_gracefully_without_ack(tmp_path, capsys):
    config = make_config(tmp_path)
    control.write_status(config.reports_dir, {"state": "running", "iteration": 1, "mode": "sandbox"})

    started = time.monotonic()
    rc = cli.cmd_daemon_stop(config, timeout=0.2, poll_interval=0.02)
    elapsed = time.monotonic() - started

    assert rc == 1
    assert elapsed < 2.0
    assert "не подтвердил" in capsys.readouterr().err


def test_daemon_stop_success_when_ack_and_stopped_state(tmp_path, monkeypatch, capsys):
    config = make_config(tmp_path)
    control.write_status(config.reports_dir, {"state": "running", "iteration": 1, "mode": "sandbox"})

    def fake_enqueue(reports_dir, name):
        command_id = "20260101T000000000000Z-fake"
        acks = control.paths(reports_dir)["acks"]
        acks.mkdir(parents=True, exist_ok=True)
        (acks / f"{command_id}.json").write_text("{}", encoding="utf-8")
        control.write_status(reports_dir, {"state": "stopped", "iteration": 2, "mode": "sandbox"})
        return command_id

    monkeypatch.setattr(cli.control, "enqueue_command", fake_enqueue)

    assert cli.cmd_daemon_stop(config, timeout=2.0, poll_interval=0.01) == 0

    assert "остановлен" in capsys.readouterr().out.lower()


def test_daemon_defaults_file_log_when_unset(tmp_path, monkeypatch):
    config = make_config(tmp_path, log_file=None)
    attached: list[object] = []
    monkeypatch.setattr(cli, "_attach_file_log", lambda cfg: attached.append(cfg.log_file))

    cli._ensure_daemon_log(config)

    assert config.log_file == cli.BASE_DIR / "var" / "daemon.log"
    assert attached == [config.log_file]


def test_fake_api_is_offline_and_deterministic():
    api = FakeTBankAPI(["SBER"])

    assert api.get_accounts() == [{"id": "fake-account"}]
    assert api.trading_schedules() == []
    portfolio = api.get_portfolio("fake-account")
    assert portfolio["positions"] == {}
    state = api.post_order("fake-account", "FAKE-SBER", 1, "ORDER_DIRECTION_BUY", price=100.0)
    assert state["status"] == "fill"
    assert state["lots_executed"] == 1


# =====================================================================
# (3) pythonw.exe: sys.stdout/sys.stderr are None at logon (no console)
# =====================================================================

@pytest.mark.parametrize("entry", [cli.cmd_daemon_run, cli.cmd_run])
@pytest.mark.parametrize("missing", [("stdout",), ("stderr",), ("stdout", "stderr")])
def test_daemon_entry_survives_missing_std_streams(tmp_path, monkeypatch, entry, missing):
    """pythonw: потоки None — daemon-run не падает и подставляет devnull.

    monkeypatch восстанавливает sys.stdout/sys.stderr и при падении теста.
    """
    config = make_config(tmp_path, tbank_fake_api=True)
    for name in missing:
        monkeypatch.setattr(sys, name, None)

    rc = entry(config, max_iterations=1)

    assert rc == 0
    assert sys.stdout is not None and sys.stderr is not None
    sys.stdout.write("")  # devnull-поток принимает записи без исключений
    sys.stderr.write("")
    status_path = control.paths(config.reports_dir)["status"]
    assert status_path.exists()  # misleading_success: файл статуса, а не только rc
    payload = json.loads(status_path.read_text(encoding="utf-8"))
    assert payload["state"] == "stopped"
    assert payload["iteration"] >= 1

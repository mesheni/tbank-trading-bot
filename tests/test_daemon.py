"""D1: headless-демон — tick-цикл, lock, статус, команды, REAL-гейт.

Блок «Характеризация» фиксирует поведение run_forever ДО реструктуризации сна
и проходит на неизменённом коде. Блок «Новое поведение» написан red-first.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import signal
import sys
import threading
import time
from types import SimpleNamespace

import pytest

import bot as bot_module
import cli
import control
import tui.data as tui_data
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


def _enqueue_when_iteration_started(config, name: str, iteration: int = 1) -> list[str]:
    """Кладёт команду, как только демон опубликует итерацию ``iteration``.

    Команда адресована РАБОТАЮЩЕМУ демону: оставленная до старта, она была бы
    отброшена как прошлая (см. test_stale_command_from_previous_run_is_discarded).
    Возвращает список id положенных команд (заполняется из потока).
    """
    enqueued: list[str] = []

    def worker() -> None:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            status = control.read_status(config.reports_dir)
            if isinstance(status, dict) and (status.get("iteration") or 0) >= iteration:
                enqueued.append(control.enqueue_command(config.reports_dir, name))
                return
            time.sleep(0.002)

    threading.Thread(target=worker, daemon=True).start()
    return enqueued


def _stub_bot(tmp_path) -> TradingBot:
    """Бот без сети и моделей (как в блоке характеризации): цикл гоняется на заглушках."""
    bot = TradingBot.__new__(TradingBot)
    bot.config = SimpleNamespace(
        loop_interval_sec=0,
        reports_dir=tmp_path,
        mode="sandbox",
        candle_interval="hour",
        forecast_horizon=1,
        budget_rub=100_000.0,
    )
    bot.instruments = {
        "SBER": {"ticker": "SBER", "figi": "BBG_S"},
        "GAZP": {"ticker": "GAZP", "figi": "BBG_G"},
        "LKOH": {"ticker": "LKOH", "figi": "BBG_L"},
    }
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
    return bot


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


def test_iteration_error_is_published_as_last_error(tmp_path):
    """Ошибка API внутри итерации -> last_error в daemon_status.json.

    Пока итерация обрабатывается, status остаётся state=running; к концу
    run_headless штатно публикуется state=stopped с сохранённым last_error.
    update_news сам глушит ошибки get_news (bot.py:385-392), поэтому в тесте
    зовём fake напрямую: так per-iteration except run_forever действительно
    видит RuntimeError и публикует его через _daemon_event(error=...).
    """

    class ExplodingAPI(FakeTBankAPI):
        def get_news(self, instrument_ids=None, max_pages=5):
            raise RuntimeError("boom")

    config = make_config(tmp_path, loop_interval_sec=0.001, daemon_tick_sec=0.001)
    bot = TradingBot(config, api=ExplodingAPI(config.tickers))
    bot.in_trading_session = lambda: True  # не зависит от часов работы биржи
    bot.update_news = lambda: bot.api.get_news(max_pages=3)  # без глушения ошибки

    assert run_headless(bot, max_iterations=1, reports_dir=config.reports_dir) == 0

    payload = control.read_status(config.reports_dir)
    assert payload is not None and not payload.get("corrupt")
    assert payload["state"] == "stopped"  # штатное завершение после обработанной ошибки
    assert "RuntimeError: boom" in str(payload["last_error"] or "")


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
    config = make_config(tmp_path, loop_interval_sec=0.5, daemon_tick_sec=0.01)
    config.reports_dir.mkdir(parents=True)
    bot_module._write_kill_switch(config.reports_dir, {"triggered_at": "2026-01-01T00:00:00+00:00"})
    bot = make_fake_bot(config)
    enqueued = _enqueue_when_iteration_started(config, "kill_switch_reset")

    assert bot._entries_allowed is False

    assert run_headless(bot, max_iterations=2, reports_dir=config.reports_dir) == 0

    assert len(enqueued) == 1  # команда дошла до очереди работающего демона
    assert not bot_module.kill_switch_path(config.reports_dir).exists()
    assert bot._entries_allowed is True
    payload = control.read_status(config.reports_dir)
    assert payload["kill_switch_active"] is False


def test_kill_switch_reset_failure_rewrites_ack_and_publishes_error(tmp_path):
    """F1: сорванный reset -> ack ok=False с причиной и last_error в статусе."""
    config = make_config(tmp_path, loop_interval_sec=0.5, daemon_tick_sec=0.01)
    config.reports_dir.mkdir(parents=True)
    bot_module._write_kill_switch(config.reports_dir, {"triggered_at": "2026-01-01T00:00:00+00:00"})
    bot = make_fake_bot(config)

    def boom() -> None:
        raise RuntimeError("reset denied")

    bot.reset_kill_switch = boom
    enqueued = _enqueue_when_iteration_started(config, "kill_switch_reset")

    assert run_headless(bot, max_iterations=2, reports_dir=config.reports_dir) == 0

    command_id = enqueued[0]
    ack_path = control.paths(config.reports_dir)["acks"] / f"{command_id}.json"
    ack = json.loads(ack_path.read_text(encoding="utf-8"))
    assert ack["ok"] is False
    assert "RuntimeError: reset denied" in ack["detail"]
    assert bot_module.kill_switch_path(config.reports_dir).exists()  # флаг остался активным
    payload = control.read_status(config.reports_dir)
    assert "RuntimeError: reset denied" in str(payload["last_error"] or "")


def test_on_tick_publishes_fresh_status_every_tick(tmp_path, monkeypatch):
    """F2: каждый тик двигает updated_at, иначе бейдж устаревает между итерациями."""
    config = make_config(tmp_path)
    runner = bot_module._DaemonRunner(make_fake_bot(config), config.reports_dir)
    base = dt.datetime(2026, 1, 1, 0, 0, 0, tzinfo=dt.timezone.utc)
    monkeypatch.setattr(control, "_utc_now", lambda: base.isoformat(timespec="seconds"))
    runner.publish(state="running")
    first = control.read_status(config.reports_dir)["updated_at"]
    assert first == "2026-01-01T00:00:00+00:00"

    late = base + dt.timedelta(seconds=tui_data.STATUS_FRESH_SEC + 5.0)
    monkeypatch.setattr(control, "_utc_now", lambda: late.isoformat(timespec="seconds"))
    runner.on_tick()

    second = control.read_status(config.reports_dir)["updated_at"]
    assert second == late.isoformat(timespec="seconds")
    assert second != first
    state = tui_data.daemon_state(config.reports_dir, now=late)
    assert tui_data.badge(state, now=late) == "running"  # свежесть восстановлена тиком


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


# =====================================================================
# (4) Positions in the daemon snapshot (TUI positions table, D3)
# =====================================================================

def test_daemon_status_includes_positions_in_session(tmp_path):
    """In-session: позиции из портфеля попадают в daemon_status.json для TUI."""
    config = make_config(tmp_path, loop_interval_sec=0.001, daemon_tick_sec=0.001)

    class PositionAPI(FakeTBankAPI):
        def get_portfolio(self, account_id):
            return {
                "positions": {
                    "FAKE-SBER": {
                        "figi": "FAKE-SBER",
                        "quantity": 10.0,
                        "average_position_price": 100.0,
                        "current_price": 110.0,
                    }
                },
                "total_amount_rub": 100_010.0,
                "cash_rub": 100_000.0,
            }

    bot = TradingBot(config, api=PositionAPI(config.tickers))
    bot.in_trading_session = lambda: True
    bot.step_ticker = lambda ticker, instrument, portfolio, entries_allowed: None

    assert run_headless(bot, max_iterations=1, reports_dir=config.reports_dir) == 0

    payload = control.read_status(config.reports_dir)
    assert payload["positions"] == [
        {"ticker": "SBER", "lots": 10, "price": 110.0, "pnl_rub": 100.0}
    ]
    assert payload["positions_count"] == 1


def test_daemon_status_positions_empty_out_of_session(tmp_path):
    """Out-of-session: снимка портфеля нет — positions пустой список, не отсутствует."""
    config = make_config(tmp_path, loop_interval_sec=0.001, daemon_tick_sec=0.001)
    bot = make_fake_bot(config)
    bot.in_trading_session = lambda: False

    assert run_headless(bot, max_iterations=1, reports_dir=config.reports_dir) == 0

    payload = control.read_status(config.reports_dir)
    assert payload["positions"] == []


# =====================================================================
# (5) SIGTERM (systemctl stop/restart): мягкая остановка вместо смерти мимо finally
# =====================================================================

def test_sigterm_requests_soft_stop_and_releases_lock(tmp_path):
    """Без обработчика сигнал убивает процесс мимо finally: статус навсегда running, lock не снят."""
    config = make_config(tmp_path, loop_interval_sec=30.0, daemon_tick_sec=0.02)
    bot = make_fake_bot(config)
    handler_before = signal.getsignal(signal.SIGTERM)

    def fire_sigterm() -> None:
        handler = signal.getsignal(signal.SIGTERM)
        assert callable(handler)
        handler(signal.SIGTERM, None)  # как это сделал бы POSIX при systemctl stop

    timer = threading.Timer(0.15, fire_sigterm)
    timer.start()
    started = time.monotonic()
    try:
        rc = run_headless(bot, max_iterations=None, reports_dir=config.reports_dir)
    finally:
        timer.cancel()
    elapsed = time.monotonic() - started

    assert rc == 0
    assert elapsed < 2.0  # 30-секундный «сон» прерван ближайшим тиком
    payload = control.read_status(config.reports_dir)
    assert payload["state"] == "stopped"
    assert not control.paths(config.reports_dir)["lock"].exists()  # finally снял lock
    assert signal.getsignal(signal.SIGTERM) == handler_before  # обработчик восстановлен


def test_sigterm_mid_iteration_skips_remaining_tickers(tmp_path):
    """Сигнал посреди итерации: текущий тикер дорабатывает, по остальным заявки не выставляются."""
    steps: list[str] = []
    bot = _stub_bot(tmp_path)
    bot._daemon_runner = bot_module._DaemonRunner(bot, tmp_path)

    def step(ticker, instrument, portfolio, entries_allowed) -> None:
        steps.append(ticker)
        bot._daemon_runner.stop_requested = True  # сигнал пришёл во время шага первого тикера

    bot.step_ticker = step
    bot.run_forever(max_iterations=None)

    assert steps == ["SBER"]  # GAZP и LKOH пропущены


def test_stop_requested_before_loop_runs_no_iteration(tmp_path):
    """SIGTERM на старте (до входа в цикл): итерация с заявками не выполняется вовсе."""
    steps: list[str] = []
    bot = _stub_bot(tmp_path)
    bot.step_ticker = lambda ticker, instrument, portfolio, entries_allowed: steps.append(ticker)
    bot._daemon_runner = bot_module._DaemonRunner(bot, tmp_path)
    bot._daemon_runner.stop_requested = True

    bot.run_forever(max_iterations=3)

    assert steps == []
    assert control.read_status(tmp_path)["state"] == "stopped"


# =====================================================================
# (6) Очередь команд: прошлое не исполняется, последнее не теряется
# =====================================================================

def test_stale_command_from_previous_run_is_discarded_not_executed(tmp_path):
    """stop от убитого демона не должен останавливать свежезапущенный после первой итерации."""
    config = make_config(tmp_path)
    stale = control.enqueue_command(config.reports_dir, "stop")
    bot = make_fake_bot(config)

    assert run_headless(bot, max_iterations=3, reports_dir=config.reports_dir) == 0

    payload = control.read_status(config.reports_dir)
    assert payload["iteration"] >= 3  # отработал все итерации
    ack = json.loads(
        (control.paths(config.reports_dir)["acks"] / f"{stale}.json").read_text(encoding="utf-8")
    )
    assert ack["ok"] is False  # клиент видит честный отказ, а не «принято»
    assert "прошлому запуску" in ack["detail"]


def test_command_during_last_iteration_is_drained_not_left_in_queue(tmp_path):
    """`--iterations N`: последняя итерация кончается без тика — очередь дренируется в конце."""
    config = make_config(tmp_path, loop_interval_sec=30.0, daemon_tick_sec=0.01)
    bot = make_fake_bot(config)
    bot.in_trading_session = lambda: True
    enqueued = _enqueue_when_iteration_started(config, "stop")

    def slow_news() -> None:
        # команда приходит, пока итерация в работе (после публикации iteration=1)
        deadline = time.monotonic() + 5.0
        while not enqueued and time.monotonic() < deadline:
            time.sleep(0.002)

    bot.update_news = slow_news

    assert run_headless(bot, max_iterations=1, reports_dir=config.reports_dir) == 0

    assert len(enqueued) == 1
    assert list(control.paths(config.reports_dir)["commands"].glob("*.json")) == []
    ack = json.loads(
        (control.paths(config.reports_dir)["acks"] / f"{enqueued[0]}.json").read_text(encoding="utf-8")
    )
    assert ack["ok"] is True


# =====================================================================
# (7) daemon stop: мёртвый процесс и systemd
# =====================================================================

def test_daemon_stop_when_process_died_returns_immediately_and_queues_nothing(tmp_path, monkeypatch, capsys):
    """Демон умер мимо finally: статус заморожен в running — ждать ack нечего."""
    config = make_config(tmp_path)
    control.write_status(
        config.reports_dir, {"state": "running", "iteration": 1, "mode": "sandbox", "pid": 999999}
    )
    monkeypatch.setattr(control, "_pid_alive", lambda pid: pid == os.getpid())  # 999999 мёртв

    started = time.monotonic()
    rc = cli.cmd_daemon_stop(config, timeout=30.0, poll_interval=0.01)
    elapsed = time.monotonic() - started

    assert rc == 0
    assert elapsed < 1.0  # не полные 30 секунд таймаута
    assert "не запущен" in capsys.readouterr().out.lower()
    assert not control.paths(config.reports_dir)["commands"].exists()  # команда не останется в очереди


def test_daemon_stop_under_systemd_refuses_and_leaves_queue_alone(tmp_path, monkeypatch, capsys):
    """Restart=always перезапустил бы демона через RestartSec: останавливает только systemctl."""
    config = make_config(tmp_path)
    control.write_status(
        config.reports_dir, {"state": "running", "iteration": 1, "mode": "sandbox", "pid": 4242}
    )
    monkeypatch.setattr(control, "daemon_pid_alive", lambda status: True)
    monkeypatch.setattr(control, "systemd_unit_of_pid", lambda pid: "tbank-bot.service")

    assert cli.cmd_daemon_stop(config, timeout=5.0, poll_interval=0.01) == 2

    err = capsys.readouterr().err
    assert "systemctl stop tbank-bot.service" in err
    assert not control.paths(config.reports_dir)["commands"].exists()


def test_daemon_stop_ignores_foreign_systemd_unit(tmp_path, monkeypatch, capsys):
    """Демон живёт не в своём юните (другое имя) — не угадываем, а останавливаем как обычно."""
    config = make_config(tmp_path)
    control.write_status(config.reports_dir, {"state": "running", "iteration": 1, "mode": "sandbox"})
    monkeypatch.setattr(control, "systemd_unit_of_pid", lambda pid: "actions-runner.service")
    enqueued: list[str] = []
    monkeypatch.setattr(
        cli.control, "enqueue_command", lambda reports_dir, name: enqueued.append(name) or "cmd-1"
    )

    rc = cli.cmd_daemon_stop(config, timeout=0.05, poll_interval=0.01)

    assert enqueued == ["stop"]
    assert rc == 1  # ack не пришёл: честный таймаут, а не отказ «под systemd»


# =====================================================================
# (8) Kill-switch после сброса: запрет не взводится сам собой
# =====================================================================

def test_kill_switch_reset_does_not_retrigger_below_old_budget_threshold(tmp_path):
    """Капитал всё ещё ниже бюджет×(1−порог): сброс переносит якорь, а не дарит лишнее письмо."""
    config = make_config(tmp_path, loop_interval_sec=0.5, daemon_tick_sec=0.01)
    config.reports_dir.mkdir(parents=True)
    bot_module._write_kill_switch(config.reports_dir, {"triggered_at": "2026-01-01T00:00:00+00:00"})
    bot = TradingBot(config, api=FakeTBankAPI(config.tickers, cash_rub=50_000.0))  # < 100_000 × 0.85
    bot.in_trading_session = lambda: True
    bot.step_ticker = lambda ticker, instrument, portfolio, entries_allowed: None
    enqueued = _enqueue_when_iteration_started(config, "kill_switch_reset")

    assert bot._entries_allowed is False

    assert run_headless(bot, max_iterations=3, reports_dir=config.reports_dir) == 0

    assert len(enqueued) == 1
    assert bot._entries_allowed is True
    assert not bot_module.kill_switch_path(config.reports_dir).exists()  # итерации 2–3 не взвели запрет
    assert bot_module.read_kill_switch_anchor(config.reports_dir)["anchor_rub"] == pytest.approx(50_000.0)
    assert control.read_status(config.reports_dir)["kill_switch_active"] is False

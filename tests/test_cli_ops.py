"""Операторские команды CLI: check-config, kill-switch-reset, real-guard, --no-color, status --json."""
from __future__ import annotations

import csv
import datetime as dt
import json
import os
import sys
from types import SimpleNamespace

import pytest

import bot
import cli
import control
import notify
from config import Config


def make_config(tmp_path, **kwargs) -> Config:
    defaults = dict(
        token="t",
        mode="sandbox",
        tickers=["SBER"],
        db_path=tmp_path / "market.sqlite",
        models_dir=tmp_path / "artifacts",
        reports_dir=tmp_path / "reports",
    )
    return Config(**{**defaults, **kwargs})


# ---------- check-config ----------

def test_check_config_success(tmp_path, monkeypatch, capsys):
    config = make_config(tmp_path)
    monkeypatch.setattr(Config, "validate", lambda self: None)

    assert cli.cmd_check_config(config) == 0

    captured = capsys.readouterr()
    assert "Конфигурация корректна" in captured.out
    assert captured.err == ""


def test_check_config_failure_returns_2(tmp_path, monkeypatch, capsys):
    config = make_config(tmp_path)

    def boom(self):
        raise ValueError("stop_loss_pct вне диапазона")

    monkeypatch.setattr(Config, "validate", boom)

    assert cli.cmd_check_config(config) == 2

    captured = capsys.readouterr()
    assert "stop_loss_pct вне диапазона" in captured.err
    assert captured.out == ""


def test_main_dispatches_check_config(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["cli.py", "check-config"])
    monkeypatch.setattr(Config, "validate", lambda self: None)

    assert cli.main() == 0
    assert "Конфигурация корректна" in capsys.readouterr().out


# ---------- kill-switch-reset ----------

def test_kill_switch_reset_clears_active_file(tmp_path, capsys):
    config = make_config(tmp_path)
    config.reports_dir.mkdir(parents=True)
    switch = bot.kill_switch_path(config.reports_dir)
    switch.write_text(json.dumps({"triggered_at": "2026-01-01T00:00:00+00:00"}), encoding="utf-8")

    assert cli.cmd_kill_switch_reset(config) == 0

    assert not switch.exists()
    assert "Kill-switch сброшен" in capsys.readouterr().out


def test_kill_switch_reset_when_not_active(tmp_path, capsys):
    config = make_config(tmp_path)
    config.reports_dir.mkdir(parents=True)

    assert cli.cmd_kill_switch_reset(config) == 0

    captured = capsys.readouterr()
    assert "Kill-switch не был активен" in captured.err
    assert captured.out == ""


def _active_switch(config):
    """Файл активного kill-switch в reports/ конфигурации."""
    config.reports_dir.mkdir(parents=True, exist_ok=True)
    switch = bot.kill_switch_path(config.reports_dir)
    switch.write_text(json.dumps({"triggered_at": "2026-01-01T00:00:00+00:00"}), encoding="utf-8")
    return switch


def test_kill_switch_reset_goes_through_live_daemon(tmp_path, monkeypatch, capsys):
    """Живой демон держит запрет в памяти: файл напрямую не удаляем, шлём команду."""
    config = make_config(tmp_path)
    switch = _active_switch(config)
    control.write_status(config.reports_dir, {"state": "running", "iteration": 1, "mode": "sandbox"})
    commands: list[str] = []

    def fake_daemon(reports_dir, name):
        commands.append(name)
        command_id = "20260101T000000000000Z-fake"
        acks = control.paths(reports_dir)["acks"]
        acks.mkdir(parents=True, exist_ok=True)
        switch.unlink()  # демон снял флаг в памяти и удалил файл
        (acks / f"{command_id}.json").write_text(json.dumps({"ok": True}), encoding="utf-8")
        return command_id

    monkeypatch.setattr(cli.control, "enqueue_command", fake_daemon)

    assert cli.cmd_kill_switch_reset(config, timeout=1.0, poll_interval=0.01) == 0

    assert commands == ["kill_switch_reset"]
    assert not switch.exists()
    assert "сброшен демоном" in capsys.readouterr().out.lower()


def test_kill_switch_reset_reports_daemon_timeout_and_keeps_file(tmp_path, monkeypatch, capsys):
    config = make_config(tmp_path)
    switch = _active_switch(config)
    control.write_status(config.reports_dir, {"state": "running", "iteration": 1, "mode": "sandbox"})
    monkeypatch.setattr(cli.control, "enqueue_command", lambda reports_dir, name: "cmd-unanswered")

    assert cli.cmd_kill_switch_reset(config, timeout=0.05, poll_interval=0.01) == 1

    assert "не подтвердил" in capsys.readouterr().err
    assert switch.exists()  # напрямую файл не трогали: демон держит запрет в памяти


def test_kill_switch_reset_with_dead_daemon_and_stale_status_clears_file(tmp_path, monkeypatch, capsys):
    config = make_config(tmp_path)
    switch = _active_switch(config)
    control.write_status(
        config.reports_dir, {"state": "running", "iteration": 1, "mode": "sandbox", "pid": 999999}
    )
    monkeypatch.setattr(control, "_pid_alive", lambda pid: pid == os.getpid())  # 999999 мёртв
    monkeypatch.setattr(
        cli.control, "enqueue_command", lambda *a, **k: pytest.fail("мёртвому демону команду не шлём")
    )

    assert cli.cmd_kill_switch_reset(config) == 0

    assert not switch.exists()
    assert bot.read_kill_switch_anchor(config.reports_dir)["anchor_rub"] is None  # якорь — капитал при старте


# ---------- MODE=real: подтверждение до любых обращений к API ----------

def test_cmd_run_real_without_confirm_returns_2_before_api(tmp_path, monkeypatch, capsys):
    config = make_config(tmp_path, mode="real")
    config.confirm_real_trading = False
    called: list[str] = []

    monkeypatch.setattr(cli, "artifact_path", lambda *a, **k: called.append("artifact"))
    monkeypatch.setattr(bot, "TradingBot", lambda cfg: called.append("bot"))

    assert cli.cmd_run(config, max_iterations=1) == 2

    captured = capsys.readouterr()
    assert "CONFIRM_REAL_TRADING=YES" in captured.err
    assert called == []  # гейт стоит до моделей и до TradingBot


# ---------- train/backtest: без токена, но с проверкой риск-параметров ----------

@pytest.mark.parametrize("command", ["train", "backtest"])
def test_main_train_and_backtest_validate_risk_without_token(monkeypatch, capsys, command):
    monkeypatch.delenv("T_INVEST_TOKEN", raising=False)
    monkeypatch.setenv("MAX_DRAWDOWN_PCT", "15")  # проценты вместо доли
    monkeypatch.setattr(
        cli, f"cmd_{command}", lambda *a, **k: pytest.fail("с кривым риском обучение не запускается")
    )
    monkeypatch.setattr(sys, "argv", ["cli.py", command])

    assert cli.main() == 2

    assert "max_drawdown_pct" in capsys.readouterr().err


# ---------- --no-color ----------

def test_no_color_global_flag_parses():
    args = cli._build_parser().parse_args(["--no-color", "status", "--json"])

    assert args.no_color is True
    assert args.command == "status" and args.json is True


def test_main_applies_no_color_to_stdout_and_stderr(monkeypatch):
    calls: list[tuple[bool, object]] = []
    monkeypatch.setattr(cli.ui, "set_colors", lambda enabled, stream=None: calls.append((enabled, stream)))
    monkeypatch.setattr(sys, "argv", ["cli.py", "--no-color", "check-config"])
    monkeypatch.setattr(Config, "validate", lambda self: None)

    assert cli.main() == 0
    assert (False, None) in calls
    assert (False, sys.stderr) in calls


# ---------- status --json ----------

def test_status_json_is_valid_and_nan_safe(tmp_path, monkeypatch, capsys):
    config = make_config(tmp_path, tickers=["SBER", "GAZP"])
    config.reports_dir.mkdir(parents=True)
    (config.reports_dir / "heartbeat").write_text(
        dt.datetime.now(dt.timezone.utc).isoformat(), encoding="utf-8"
    )
    with (config.reports_dir / "journal.csv").open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["time", "ticker", "action", "lots", "price", "reason"])
        writer.writeheader()
        writer.writerow(
            {
                "time": "2026-01-01T10:00:00+00:00",
                "ticker": "SBER",
                "action": "BUY",
                "lots": "1",
                "price": "100.0",
                "reason": "прогноз",
            }
        )

    def fake_model_info(cfg, ticker):
        if ticker == "SBER":
            return {
                "ticker": ticker,
                "kind": "lgbm",
                "threshold": 0.001,
                "dir_acc": float("nan"),
                "age_days": 1,
            }
        return None

    monkeypatch.setattr(cli, "_model_info", fake_model_info)

    assert cli.cmd_status(config, json_output=True) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "sandbox"
    assert payload["capital_profile"] == "auto"
    assert payload["budget_rub"] == config.budget_rub
    assert payload["tickers"] == ["SBER", "GAZP"]
    assert payload["models"] == [{"ticker": "SBER", "kind": "lgbm", "dir_acc": None}]
    assert isinstance(payload["heartbeat_age_min"], float)
    assert payload["last_trade"]["ticker"] == "SBER"


def test_main_status_json_passes_flag(monkeypatch):
    seen: dict[str, bool] = {}

    def fake_status(config, json_output=False):
        seen["json"] = json_output
        return 0

    monkeypatch.setattr(sys, "argv", ["cli.py", "status", "--json"])
    monkeypatch.setattr(cli, "cmd_status", fake_status)

    assert cli.main() == 0
    assert seen["json"] is True


# ---------- digest: дата в теме и покрытие по equity ----------

def test_compose_digest_includes_equity_coverage(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    config.reports_dir.mkdir(parents=True)
    (config.reports_dir / "equity_live.csv").write_text(
        "time,total_rub,cash_rub,positions_rub\n"
        "2026-01-01T10:00:00+00:00,100000,100000,0\n"
        "2026-01-11T10:00:00+00:00,110000,110000,0\n",
        encoding="utf-8",
    )

    class _API:
        def get_accounts(self):
            return [{"id": "acc-1"}]

        def get_portfolio(self, account_id):
            return {"total_amount_rub": 110000.0, "cash_rub": 110000.0, "positions": {}}

        def resolve_instruments(self, tickers):
            return {}

    monkeypatch.setattr(cli, "make_api", lambda cfg: _API())

    body = cli.compose_digest(config)

    assert "Данные за 10 дн. (с 01.01.2026)" in body


def test_cmd_digest_subject_has_utc_date(tmp_path, monkeypatch):
    config = make_config(
        tmp_path,
        notify_email_to="trader@example.com",
        smtp_host="smtp.yandex.ru",
        smtp_user="bot@yandex.ru",
        smtp_password="secret",
    )
    monkeypatch.setattr(cli, "compose_digest", lambda cfg: "body")
    sent: dict[str, str] = {}
    monkeypatch.setattr(
        notify.Notifier, "send", lambda self, subject, body: sent.update(subject=subject) or True
    )

    assert cli.cmd_digest(config) == 0
    assert sent["subject"] == f"дайджест {dt.datetime.now(dt.timezone.utc):%Y-%m-%d}"


# ---------- watchdog: диск и ошибки systemctl ----------

class _ActiveCalendar:
    def __init__(self, api):
        pass

    def active(self):
        return True


def _stale_watchdog_config(tmp_path) -> Config:
    config = make_config(tmp_path)
    config.reports_dir.mkdir(parents=True, exist_ok=True)
    (config.reports_dir / "heartbeat").write_text(
        (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=2)).isoformat(), encoding="utf-8"
    )
    return config


def test_watchdog_warns_when_disk_low(tmp_path, monkeypatch, capsys):
    config = make_config(tmp_path)
    config.reports_dir.mkdir(parents=True)
    (config.reports_dir / "heartbeat").write_text(
        dt.datetime.now(dt.timezone.utc).isoformat(), encoding="utf-8"
    )
    monkeypatch.setattr(cli, "make_api", lambda cfg: object())
    monkeypatch.setattr(bot, "SessionCalendar", _ActiveCalendar)
    monkeypatch.setattr(cli.shutil, "disk_usage", lambda path: SimpleNamespace(free=100 * 1024 * 1024))

    assert cli.cmd_watchdog(config, max_stale_min=20, restart=False) == 0
    assert "мало свободного места" in capsys.readouterr().err


def test_watchdog_restart_missing_systemctl_returns_1(tmp_path, monkeypatch, capsys):
    config = _stale_watchdog_config(tmp_path)
    monkeypatch.setattr(cli, "make_api", lambda cfg: object())
    monkeypatch.setattr(bot, "SessionCalendar", _ActiveCalendar)

    def _missing(*args, **kwargs):
        raise FileNotFoundError

    monkeypatch.setattr("subprocess.run", _missing)

    assert cli.cmd_watchdog(config, max_stale_min=20, restart=True) == 1
    assert "systemctl не найден" in capsys.readouterr().err


def test_watchdog_restart_nonzero_exit_returns_1(tmp_path, monkeypatch, capsys):
    config = _stale_watchdog_config(tmp_path)
    monkeypatch.setattr(cli, "make_api", lambda cfg: object())
    monkeypatch.setattr(bot, "SessionCalendar", _ActiveCalendar)
    monkeypatch.setattr(
        "subprocess.run", lambda *args, **kwargs: SimpleNamespace(returncode=1, stderr="Unit not found")
    )

    assert cli.cmd_watchdog(config, max_stale_min=20, restart=True) == 1
    assert "Unit not found" in capsys.readouterr().err


# ---------- _pick_account ----------

def test_pick_account_warns_when_several_accounts(tmp_path, capsys):
    config = make_config(tmp_path)
    config.account_id = ""

    assert cli._pick_account(config, [{"id": "acc-1"}, {"id": "acc-2"}]) == "acc-1"
    assert "ACCOUNT_ID" in capsys.readouterr().err

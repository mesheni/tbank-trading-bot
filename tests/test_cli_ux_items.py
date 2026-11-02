"""UX-пункты CLI: U1 (общий обработчик ошибок), U3 (баннер режима),
U4 (сгруппированный --help), U5 (--json для report и check-config).

Блок «Характеризация» фиксирует поведение ДО изменений и проходит на исходном
коде. Блок «Новое поведение» написан red-first по TDD-контракту.
"""
from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest

import cli
import ui
from config import Config

ALL_COMMANDS = {
    "setup",
    "smoke",
    "download",
    "news",
    "train",
    "backtest",
    "run",
    "report",
    "status",
    "check-config",
    "watchdog",
    "digest",
    "send-test-email",
    "normalize",
    "kill-switch-reset",
    "reset-sandbox",
}


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


@pytest.fixture(autouse=True)
def _reset_color_state():
    ui._enabled.clear()
    yield
    ui._enabled.clear()


# =====================================================================
# (1) Характеризация: поведение на НЕИЗМЕНЁННОМ коде
# =====================================================================

def test_help_lists_all_sixteen_commands(capsys) -> None:
    """--help перечисляет все 16 существующих команд (до U4)."""
    with pytest.raises(SystemExit) as excinfo:
        cli._build_parser().parse_args(["--help"])

    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    missing = sorted(command for command in ALL_COMMANDS if command not in out)
    assert missing == [], f"в --help нет команд: {missing}"


def test_check_config_current_output_characterization(tmp_path, monkeypatch, capsys) -> None:
    """Текущие ключевые строки check-config (до U5): [OK] и пустой stderr."""
    config = make_config(tmp_path)
    monkeypatch.setattr(Config, "validate", lambda self: None)

    assert cli.cmd_check_config(config) == 0

    captured = capsys.readouterr()
    assert "Конфигурация корректна" in captured.out
    assert captured.err == ""


# cmd_run в real без CONFIRM_REAL_TRADING -> 2 уже покрыт существующим тестом:
# tests/test_cli_ops.py::test_cmd_run_real_without_confirm_returns_2_before_api


# =====================================================================
# (2) Новое поведение (red-first): U1 / U3 / U4 / U5
# =====================================================================

# ---------- U1: общий обработчик непредвиденных ошибок ----------

def test_main_unexpected_error_returns_1_without_traceback(monkeypatch, capsys) -> None:
    def boom(config):
        raise ValueError("boom")

    monkeypatch.setattr(sys, "argv", ["cli.py", "status"])
    monkeypatch.setattr(cli, "cmd_status", boom)

    assert cli.main() == 1

    captured = capsys.readouterr()
    assert "Непредвиденная ошибка" in captured.err
    assert "ValueError" in captured.err and "boom" in captured.err
    assert "лог не настроен (LOG_FILE)" in captured.err
    # без LOG_FILE трассировка не должна попадать даже в stderr — только одна строка в лог
    assert "Traceback" not in captured.out + captured.err


def test_main_unexpected_error_hints_configured_log_file(monkeypatch, capsys, tmp_path) -> None:
    log_path = tmp_path / "logs" / "bot.log"
    fake_config = SimpleNamespace(mode="sandbox", log_file=log_path)

    def boom(config):
        raise KeyError("missing-key")

    monkeypatch.setattr(cli, "Config", lambda: fake_config)
    monkeypatch.setattr(cli, "_attach_file_log", lambda config: None)
    monkeypatch.setattr(sys, "argv", ["cli.py", "status"])
    monkeypatch.setattr(cli, "cmd_status", boom)

    assert cli.main() == 1

    err = capsys.readouterr().err
    assert "KeyError" in err and "missing-key" in err
    assert f"Подробности в логе: {log_path}" in err
    assert "лог не настроен" not in err


# ---------- U3: баннер режима ----------

def test_mode_banner_text_for_sandbox_and_real() -> None:
    ui.set_colors(True)
    assert "SANDBOX" in ui.mode_banner("sandbox")
    assert "REAL" in ui.mode_banner("real")
    assert ui.mode_banner("real").startswith("\033[")
    assert ui.mode_banner("sandbox").startswith("\033[")


def test_mode_banner_respects_no_color(monkeypatch) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    ui._enabled.clear()

    assert ui.mode_banner("real") == " РЕЖИМ: REAL "
    assert ui.mode_banner("sandbox") == " РЕЖИМ: SANDBOX "


def test_cmd_run_prints_mode_banner_as_first_line(tmp_path, monkeypatch, capsys) -> None:
    config = make_config(tmp_path, mode="real")
    config.confirm_real_trading = False

    assert cli.cmd_run(config, max_iterations=None) == 2

    first_line = capsys.readouterr().out.splitlines()[0]
    assert "РЕЖИМ: REAL" in first_line


# ---------- U4: группы команд в --help ----------

def test_help_epilog_contains_command_groups(capsys) -> None:
    with pytest.raises(SystemExit):
        cli._build_parser().parse_args(["--help"])

    out = capsys.readouterr().out
    assert "Первый запуск" in out
    assert "Торговля" in out
    assert "Мониторинг" in out
    assert "Обслуживание" in out


# ---------- U5: --json у report и check-config ----------

def test_parser_accepts_json_for_report_and_check_config() -> None:
    parser = cli._build_parser()

    assert parser.parse_args(["report", "--json"]).json is True
    assert parser.parse_args(["check-config", "--json"]).json is True
    # старые вызовы без флага не должны ломаться
    assert parser.parse_args(["report"]).json is False
    assert parser.parse_args(["check-config"]).json is False


def test_check_config_json_is_offline_and_masks_token(tmp_path, monkeypatch, capsys) -> None:
    secret = "t.SUPER-SECRET-TOKEN-VALUE-1234567890"
    config = make_config(tmp_path, token=secret)
    monkeypatch.setattr(Config, "validate", lambda self: None)

    def forbidden_api(config):
        raise AssertionError("check-config --json не должен обращаться к API")

    monkeypatch.setattr(cli, "make_api", forbidden_api)

    assert cli.cmd_check_config(config, json_output=True) == 0

    out = capsys.readouterr().out
    payload = json.loads(out)  # ровно один JSON-объект
    assert payload["ok"] is True
    assert payload["mode"] == "sandbox"
    assert payload["token"].endswith("...")
    assert secret not in out
    assert "NaN" not in out


def test_check_config_json_reports_validation_error(tmp_path, monkeypatch, capsys) -> None:
    config = make_config(tmp_path)

    def boom(self):
        raise ValueError("MODE должен быть sandbox|real")

    monkeypatch.setattr(Config, "validate", boom)

    assert cli.cmd_check_config(config, json_output=True) == 2

    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["ok"] is False
    assert payload["errors"] == ["MODE должен быть sandbox|real"]


def test_main_passes_json_flag_to_check_config(monkeypatch) -> None:
    seen: dict[str, bool] = {}

    def fake_check(config, json_output=False):
        seen["json"] = json_output
        return 0

    monkeypatch.setattr(sys, "argv", ["cli.py", "check-config", "--json"])
    monkeypatch.setattr(Config, "validate", lambda self: None)
    monkeypatch.setattr(cli, "cmd_check_config", fake_check)

    assert cli.main() == 0
    assert seen["json"] is True


class FakeReportAPI:
    def get_accounts(self):
        return [{"id": "acc-1"}]

    def get_portfolio(self, account_id):
        return {
            "total_amount_rub": 1_050_000.0,
            "cash_rub": float("nan"),
            "positions": {
                "FIGI-SBER": {
                    "quantity": 10,
                    "average_position_price": 100.0,
                    "current_price": 110.0,
                }
            },
        }

    def resolve_instruments(self, tickers):
        return {t: {"figi": f"FIGI-{t}", "lot": 2} for t in tickers}


def _report_config(tmp_path):
    return SimpleNamespace(
        mode="sandbox",
        reports_dir=tmp_path,
        tickers=["SBER"],
        account_id="",
        sandbox_initial_rub=1_000_000.0,
        account_budget_rub=None,
    )


def test_report_json_is_one_object_and_nan_safe(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "make_api", lambda config: FakeReportAPI())

    assert cli.cmd_report(_report_config(tmp_path), json_output=True) == 0

    out = capsys.readouterr().out
    payload = json.loads(out)  # ровно один JSON-объект, без таблиц
    assert payload["ok"] is True
    assert payload["capital_rub"] == 1_050_000.0
    assert payload["cash_rub"] is None  # NaN -> null
    assert payload["positions"][0]["ticker"] == "SBER"
    assert payload["positions"][0]["lots"] == 5
    assert "СЧЁТ" not in out
    assert "NaN" not in out


def test_report_json_without_accounts_returns_1(tmp_path, monkeypatch, capsys) -> None:
    class NoAccountsAPI(FakeReportAPI):
        def get_accounts(self):
            return []

    monkeypatch.setattr(cli, "make_api", lambda config: NoAccountsAPI())

    assert cli.cmd_report(_report_config(tmp_path), json_output=True) == 1

    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert "Счетов нет" in payload["error"]


def test_main_passes_json_flag_to_report(monkeypatch) -> None:
    seen: dict[str, bool] = {}

    def fake_report(config, json_output=False):
        seen["json"] = json_output
        return 0

    monkeypatch.setattr(sys, "argv", ["cli.py", "report", "--json"])
    monkeypatch.setattr(Config, "validate", lambda self: None)
    monkeypatch.setattr(cli, "cmd_report", fake_report)

    assert cli.main() == 0
    assert seen["json"] is True


# ---------- Fix round: invalid config still yields JSON ----------

def test_main_check_config_json_with_invalid_env_emits_json(monkeypatch, capsys) -> None:
    """MODE=bogus: pre-validation не должна съедать JSON — ошибки едут в payload."""
    monkeypatch.setenv("MODE", "bogus")
    monkeypatch.setattr(sys, "argv", ["cli.py", "check-config", "--json"])

    assert cli.main() == 2

    out = capsys.readouterr().out
    payload = json.loads(out)  # ровно один JSON-объект даже для неверного конфига
    assert payload["ok"] is False
    assert payload["errors"]
    assert any("MODE" in error for error in payload["errors"])


def test_check_config_json_masks_short_token(tmp_path, monkeypatch, capsys) -> None:
    """Секрет короче 8 символов не должен утекать даже первыми 4 символами."""
    config = make_config(tmp_path, token="t.abc")
    monkeypatch.setattr(Config, "validate", lambda self: None)

    assert cli.cmd_check_config(config, json_output=True) == 0

    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["token"] == "***"
    assert "t.abc" not in out

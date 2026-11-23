"""Риск-валидация конфига и подтверждение реальной торговли."""
from __future__ import annotations

import pytest

from config import Config

_RISK_ENV = (
    "STOP_LOSS_PCT",
    "TAKE_PROFIT_PCT",
    "MAX_POSITION_PCT",
    "CAPITAL_PROFILE",
    "ACCOUNT_BUDGET_RUB",
    "COMMISSION_MIN_RUB",
)


@pytest.fixture(autouse=True)
def _clean_risk_env(monkeypatch):
    """Дефолты Config не должны зависеть от локального .env разработчика."""
    for name in _RISK_ENV:
        monkeypatch.delenv(name, raising=False)


def test_validate_risk_passes_for_defaults():
    Config().validate_risk()  # не бросает


def test_validate_risk_rejects_stop_loss_out_of_range():
    cfg = Config()
    cfg.stop_loss_pct = 5
    with pytest.raises(ValueError, match=r"stop_loss_pct должен быть в \(0; 1\), получено: 5"):
        cfg.validate_risk()


def test_validate_risk_rejects_negative_commission_min():
    cfg = Config()
    cfg.commission_min_rub = -1.0
    with pytest.raises(ValueError, match="COMMISSION_MIN_RUB не может быть отрицательным"):
        cfg.validate_risk()


def test_validate_still_runs_risk_checks():
    cfg = Config(token="token")
    cfg.stop_loss_pct = 5
    with pytest.raises(ValueError, match="stop_loss_pct"):
        cfg.validate()


# ---------- параметры, которые раньше проходили без проверки ----------

@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        # 15 вместо 0.15: порог kill-switch уходил в минус и запрет никогда не срабатывал
        ("max_drawdown_pct", 15, "max_drawdown_pct"),
        ("max_drawdown_pct", float("nan"), "max_drawdown_pct"),
        ("min_abs_return", 0, "MIN_ABS_RETURN"),
        ("reversal_exit_mult", -1, "REVERSAL_EXIT_MULT"),
        ("commission_pct", 5, "commission_pct"),
        ("slippage_pct", -0.1, "slippage_pct"),
        ("max_total_exposure_pct", 15, "MAX_TOTAL_EXPOSURE_PCT"),
        ("max_total_exposure_pct", 0, "MAX_TOTAL_EXPOSURE_PCT"),
        ("forecast_horizon", 0, "FORECAST_HORIZON"),
        # интервал 0: busy-loop, в котором очередь команд (stop) не опрашивается
        ("loop_interval_sec", 0, "LOOP_INTERVAL_SEC"),
        ("sandbox_initial_rub", 0, "SANDBOX_INITIAL_RUB"),
    ],
)
def test_validate_risk_rejects_out_of_range(field, value, message):
    cfg = Config()
    setattr(cfg, field, value)
    with pytest.raises(ValueError, match=message):
        cfg.validate_risk()


def test_validate_risk_accepts_boundary_values():
    cfg = Config()
    cfg.max_drawdown_pct = 0
    cfg.max_total_exposure_pct = 1.0
    cfg.commission_pct = 0
    cfg.slippage_pct = 0
    cfg.loop_interval_sec = 0.01  # доли секунды допустимы: онлайн-тесты и e2e на них
    cfg.forecast_horizon = 1
    cfg.validate_risk()  # не бросает


def test_zero_sandbox_budget_allowed_in_real_mode():
    cfg = Config()
    cfg.mode = "real"
    cfg.sandbox_initial_rub = 0
    cfg.validate_risk()  # в real стартовый бюджет песочницы не используется


@pytest.mark.parametrize("raw", ["nan", "inf", "-inf"])
def test_non_finite_env_value_falls_back_to_default(monkeypatch, capsys, raw):
    monkeypatch.setenv("MAX_DRAWDOWN_PCT", raw)

    assert Config().max_drawdown_pct == 0.15

    assert "MAX_DRAWDOWN_PCT" in capsys.readouterr().err  # с предупреждением, а не молча


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("YES", True),
        ("yes", False),
        ("Yes", False),
        ("YES ", False),
        ("1", False),
        ("true", False),
        ("", False),
    ],
)
def test_confirm_real_trading_requires_exact_yes(monkeypatch, raw, expected):
    monkeypatch.setenv("CONFIRM_REAL_TRADING", raw)
    assert Config().confirm_real_trading is expected


def test_confirm_real_trading_defaults_false(monkeypatch):
    monkeypatch.delenv("CONFIRM_REAL_TRADING", raising=False)
    assert Config().confirm_real_trading is False

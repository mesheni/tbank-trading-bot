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

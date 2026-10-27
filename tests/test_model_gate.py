"""Гейт качества модели как чистая функция (аудит F-C2.4)."""
from __future__ import annotations

from bot import model_passes_live_gate

NAN = float("nan")


def test_dir_acc_above_threshold_passes():
    assert model_passes_live_gate({"directional_acc": 0.547}, 0.5) is True


def test_dir_acc_exactly_at_threshold_passes():
    assert model_passes_live_gate({"directional_acc": 0.5}, 0.5) is True


def test_nan_dir_acc_with_positive_live_sharpe_passes():
    metrics = {"directional_acc": NAN, "live_sharpe_net": 0.42}
    assert model_passes_live_gate(metrics, 0.5) is True


def test_positive_strategy_sharpe_also_passes():
    metrics = {"directional_acc": 0.48, "strategy_sharpe_net": 1.74}
    assert model_passes_live_gate(metrics, 0.5) is True


def test_all_nan_fails():
    metrics = {
        "directional_acc": NAN,
        "live_sharpe_net": NAN,
        "strategy_sharpe_net": NAN,
    }
    assert model_passes_live_gate(metrics, 0.5) is False


def test_missing_metrics_fail():
    assert model_passes_live_gate({}, 0.5) is False


def test_negative_sharpe_with_low_dir_acc_fails():
    metrics = {"directional_acc": 0.480, "strategy_sharpe_net": -7.02}
    assert model_passes_live_gate(metrics, 0.5) is False


def test_nan_sharpe_with_subthreshold_dir_acc_fails():
    metrics = {"directional_acc": 0.480, "live_sharpe_net": NAN}
    assert model_passes_live_gate(metrics, 0.5) is False


def test_zero_sharpe_does_not_pass():
    # edge должен быть строго положительным
    assert model_passes_live_gate({"directional_acc": 0.48, "live_sharpe_net": 0.0}, 0.5) is False

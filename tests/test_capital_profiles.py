"""Профили капитала: режимы micro/small/standard, бюджет счёта, фильтр по лоту."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pandas as pd
import pytest

import cli
from backtest import run_backtest
from config import (
    CAPITAL_PROFILES,
    MIN_TRADABLE_EQUITY_RUB,
    Config,
    resolve_capital_profile,
)
from strategy import PortfolioState, RiskConfig, is_affordable


# ---------- resolve_capital_profile ----------

@pytest.mark.parametrize(
    "equity,expected",
    [
        (9_999.0, "micro"),  # ниже минимума — micro, бот предупредит о неполноценности
        (10_000.0, "micro"),
        (49_999.0, "micro"),
        (50_000.0, "small"),
        (199_999.0, "small"),
        (200_000.0, "standard"),
        (1_975_000.0, "standard"),
    ],
)
def test_resolve_capital_profile_boundaries(equity, expected):
    assert resolve_capital_profile(equity) == expected


# ---------- apply_profile ----------

def make_config(**kwargs) -> Config:
    defaults = dict(token="t", mode="sandbox")
    return Config(**{**defaults, **kwargs})


def test_apply_profile_auto_picks_params_by_equity(monkeypatch):
    for var in ("MAX_POSITION_PCT", "MAX_TOTAL_EXPOSURE_PCT"):
        monkeypatch.delenv(var, raising=False)

    config = make_config(capital_profile="auto")
    assert config.apply_profile(30_000.0) == "micro"
    assert config.max_position_pct == CAPITAL_PROFILES["micro"]["max_position_pct"]
    assert config.max_total_exposure_pct == CAPITAL_PROFILES["micro"]["max_total_exposure_pct"]

    config = make_config(capital_profile="auto")
    assert config.apply_profile(120_000.0) == "small"
    assert config.max_position_pct == pytest.approx(0.35)

    config = make_config(capital_profile="auto")
    assert config.apply_profile(1_000_000.0) == "standard"
    assert config.max_position_pct == pytest.approx(0.20)
    assert config.max_total_exposure_pct == pytest.approx(0.8)


def test_apply_profile_fixed_and_env_override(monkeypatch):
    for var in ("MAX_POSITION_PCT", "MAX_TOTAL_EXPOSURE_PCT"):
        monkeypatch.delenv(var, raising=False)

    # фиксированный профиль — капитал не важен
    config = make_config(capital_profile="micro")
    assert config.apply_profile(5_000_000.0) == "micro"
    assert config.max_position_pct == pytest.approx(1.0)

    # явный MAX_POSITION_PCT из .env сильнее профиля
    monkeypatch.setenv("MAX_POSITION_PCT", "0.10")
    config = make_config(capital_profile="micro")
    config.apply_profile(30_000.0)
    assert config.max_position_pct == pytest.approx(0.10)  # не перезаписан профилем
    assert config.max_total_exposure_pct == pytest.approx(1.0)  # не задан явно — из профиля

    # явный "off" для экспозиции тоже сильнее профиля
    monkeypatch.delenv("MAX_POSITION_PCT")
    monkeypatch.setenv("MAX_TOTAL_EXPOSURE_PCT", "off")
    config = make_config(capital_profile="micro")
    config.apply_profile(30_000.0)
    assert config.max_position_pct == pytest.approx(1.0)
    assert config.max_total_exposure_pct is None


# ---------- budget_rub ----------

def test_budget_rub_prefers_account_budget():
    config = make_config(account_budget_rub=30_000.0, sandbox_initial_rub=1_000_000.0)
    assert config.budget_rub == 30_000.0
    config = make_config(sandbox_initial_rub=500_000.0)
    assert config.budget_rub == 500_000.0


# ---------- validate ----------

def test_validate_rejects_unknown_profile():
    config = make_config(capital_profile="huge")
    with pytest.raises(ValueError, match="CAPITAL_PROFILE"):
        config.validate()


def test_validate_rejects_non_positive_budget():
    config = make_config(account_budget_rub=0.0)
    with pytest.raises(ValueError, match="ACCOUNT_BUDGET_RUB"):
        config.validate()


def test_validate_allows_full_position_share():
    # доля позиции 1.0 допустима: это режим micro (весь капитал в одной позиции)
    make_config(max_position_pct=1.0).validate()


# ---------- is_affordable ----------

def test_is_affordable_boundaries():
    # лот ровно в бюджет — доступен; дороже — нет
    assert is_affordable(price=100.0, lot_size=10, equity=1_000.0, max_position_pct=1.0)
    assert not is_affordable(price=100.01, lot_size=10, equity=1_000.0, max_position_pct=1.0)
    # бюджет = доля капитала
    assert is_affordable(100.0, 10, 50_000.0, 0.2)  # 1000 <= 10000
    assert not is_affordable(100.0, 10, 4_000.0, 0.2)  # 1000 > 800
    assert not is_affordable(0.0, 10, 1_000_000.0, 1.0)
    assert not is_affordable(100.0, 0, 1_000_000.0, 1.0)


# ---------- бэктест на малом капитале ----------

def flat_candles(closes: list[float]) -> pd.DataFrame:
    n = len(closes)
    index = pd.date_range("2026-09-08", periods=n, freq="h", tz="UTC")
    return pd.DataFrame(
        {
            "open": closes,
            "high": [c * 1.001 for c in closes],
            "low": [c * 0.999 for c in closes],
            "close": closes,
            "volume": [1000.0] * n,
        },
        index=index,
    )


def test_backtest_micro_capital_trades_cheap_lot():
    # цена 100, лот 1: лот 100 руб влезает в 30 тыс. (доля 0.9 — с запасом на издержки)
    risk = RiskConfig(max_position_pct=0.9, min_abs_return=0.004)
    df = flat_candles([100.0] * 5)
    result = run_backtest(
        df, pd.Series(0.01, index=df.index, dtype=float), risk, initial_cash=30_000.0, lot_size=1
    )
    assert result.metrics["n_trades"] > 0
    assert result.equity_curve.iloc[-1] > 0


def test_backtest_micro_capital_never_buys_expensive_lot():
    # лот 200 * 250 = 50 тыс. руб дороже всего счёта 30 тыс. — сделок нет, деньги целы
    risk = RiskConfig(max_position_pct=1.0, min_abs_return=0.004)
    df = flat_candles([250.0] * 5)
    result = run_backtest(
        df, pd.Series(0.01, index=df.index, dtype=float), risk, initial_cash=30_000.0, lot_size=200
    )
    assert result.metrics["n_trades"] == 0
    assert result.equity_curve.iloc[-1] == pytest.approx(30_000.0)


def test_backtest_min_commission_per_order():
    # комиссия 0% + минимум 5 руб за заявку: каждая сторона платит ровно минимум
    risk = RiskConfig(max_position_pct=0.9, commission_pct=0.0, commission_min_rub=5.0)
    df = flat_candles([100.0, 103.0])
    preds = pd.Series([0.01, -0.02], index=df.index, dtype=float)
    result = run_backtest(df, preds, risk, initial_cash=30_000.0, lot_size=1)

    assert len(result.trades) == 2
    assert result.trades["commission"].tolist() == pytest.approx([5.0, 5.0])
    assert result.metrics["total_commission"] == pytest.approx(10.0)
    # кэш сходится: комиссии по 5 руб с каждой стороны плюс/минус ход цены
    lots = result.trades[result.trades["action"] == "BUY"]["lots"].iloc[0]
    buy_exec = 100.0 * (1 + risk.slippage_pct)
    sell_exec = 103.0 * (1 - risk.slippage_pct)
    expected = 30_000.0 - 10.0 + (sell_exec - buy_exec) * lots
    assert result.equity_curve.iloc[-1] == pytest.approx(expected)


def test_backtest_without_min_commission_unchanged():
    risk = RiskConfig(max_position_pct=0.9, commission_pct=0.001, commission_min_rub=0.0)
    df = flat_candles([100.0, 103.0])
    preds = pd.Series([0.01, -0.02], index=df.index, dtype=float)
    result = run_backtest(df, preds, risk, initial_cash=30_000.0, lot_size=1)
    lots = result.trades[result.trades["action"] == "BUY"]["lots"].iloc[0]
    buy_exec = 100.0 * (1 + risk.slippage_pct)
    sell_exec = 103.0 * (1 - risk.slippage_pct)
    expected = (buy_exec + sell_exec) * lots * risk.commission_pct
    assert result.metrics["total_commission"] == pytest.approx(expected, rel=1e-4)


# ---------- kill-switch от бюджета счёта ----------

def make_kill_switch_bot(budget: float | None):
    from notify import Notifier, SmtpConfig

    from bot import TradingBot

    bot = TradingBot.__new__(TradingBot)
    bot.config = SimpleNamespace(sandbox_initial_rub=1_000_000.0, max_drawdown_pct=0.15)
    bot._budget_rub = budget
    bot.notifier = Notifier(SmtpConfig())  # выключен: send() тихо вернёт False
    bot._entries_allowed = True
    return bot


def test_kill_switch_small_budget_trips_at_15pct_of_budget():
    bot = make_kill_switch_bot(budget=30_000.0)
    bot._check_kill_switch(27_000.0)  # -10% от бюджета — ещё работает
    assert bot._entries_allowed is True

    bot._check_kill_switch(25_000.0)  # -16.7% > 15% — сработал
    assert bot._entries_allowed is False


def test_kill_switch_falls_back_to_sandbox_budget():
    # бюджет ещё не установлен (запуск вне run_forever) — прежнее поведение
    bot = make_kill_switch_bot(budget=None)
    bot._check_kill_switch(900_000.0)
    assert bot._entries_allowed is True
    bot._check_kill_switch(840_000.0)
    assert bot._entries_allowed is False


# ---------- cli: фильтр тикеров по капиталу ----------

def test_cmd_train_filters_tickers_by_lot_affordability(tmp_path, monkeypatch, candles):
    calls: list[str] = []

    def fake_load_candles(_conn, ticker, _interval):
        df = candles.copy()
        if ticker == "EXPENSIVE":
            df[["open", "high", "low", "close"]] *= 10  # лот 100 * ~2500 = 250 тыс. руб
        return df

    def fake_evaluate_all(_df, _horizon, round_trip_cost=0.0):
        metrics = pd.DataFrame(
            {
                "rmse": [0.01],
                "mae": [0.01],
                "directional_acc": [0.6],
                "strategy_sharpe": [1.0],
                "strategy_sharpe_net": [0.5],
                "n_points": [100],
            },
            index=["persistence"],
        )
        return metrics, None

    def fake_train_and_save(*args, **kwargs):
        calls.append("train")
        return SimpleNamespace(kind="persistence", metrics={"strategy_sharpe_net": 0.5}, threshold=0.004)

    monkeypatch.setattr(cli, "connect", lambda _path: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(cli, "load_candles", fake_load_candles)
    monkeypatch.setattr(
        cli, "load_instrument",
        lambda _conn, ticker: {"CHEAP": {"lot": 1}, "EXPENSIVE": {"lot": 100}}[ticker],
    )
    monkeypatch.setattr(cli, "evaluate_all", fake_evaluate_all)
    monkeypatch.setattr(cli, "train_and_save", fake_train_and_save)

    config = SimpleNamespace(
        db_path=tmp_path / "db.sqlite",
        tickers=["CHEAP", "EXPENSIVE"],
        candle_interval="hour",
        forecast_horizon=1,
        commission_pct=0.0004,
        slippage_pct=0.0002,
        models_dir=tmp_path / "models",
        reports_dir=tmp_path / "reports",
    )

    rc = cli.cmd_train(config, capital=30_000.0)

    assert rc == 0
    assert calls == ["train"]  # EXPENSIVE отфильтрован ещё до обучения

    payload = json.loads((tmp_path / "reports" / "train_summary.json").read_text(encoding="utf-8"))
    assert payload["capital_rub"] == 30_000.0
    assert payload["profile"] == "micro"
    assert list(payload["tickers"]) == ["CHEAP"]


# ---------- bot: старт профиля капитала ----------

def make_startup_bot(equity: float, **config_overrides):
    """Бот без __init__: настоящий Config (apply_profile — его метод), заглушки окружения."""
    from bot import TradingBot
    from notify import Notifier, SmtpConfig

    cfg = dict(
        token="t",
        mode="sandbox",
        capital_profile="auto",
        account_budget_rub=None,
        sandbox_initial_rub=1_000_000.0,
    )
    cfg.update(config_overrides)

    bot = TradingBot.__new__(TradingBot)
    bot.config = Config(**cfg)
    bot.notifier = Notifier(SmtpConfig())
    bot.conn = None  # load_candles в тестах подменяется, БД не нужна
    bot._load_portfolio = lambda: PortfolioState(cash=equity, equity=equity, positions={})
    bot.instruments = {
        "CHEAP": {"ticker": "CHEAP", "figi": "F1", "lot": 1},
        "EXPENSIVE": {"ticker": "EXPENSIVE", "figi": "F2", "lot": 100},
    }
    candles_by_ticker = {
        "CHEAP": flat_candles([250.0] * 3),
        "EXPENSIVE": flat_candles([2500.0] * 3),
    }
    import bot as bot_mod

    return bot, bot_mod, candles_by_ticker


def setup_env(monkeypatch):
    for var in ("MAX_POSITION_PCT", "MAX_TOTAL_EXPOSURE_PCT"):
        monkeypatch.delenv(var, raising=False)


def test_bot_startup_applies_profile_and_filters_instruments(monkeypatch):
    setup_env(monkeypatch)
    bot, bot_mod, candles = make_startup_bot(equity=30_000.0)
    monkeypatch.setattr(bot_mod, "load_candles", lambda _conn, t, _i: candles[t])

    bot._setup_capital_profile()

    # sandbox без явного ACCOUNT_BUDGET_RUB: бюджет = SANDBOX_INITIAL_RUB (обратная совместимость)
    assert bot._budget_rub == 1_000_000.0

    # профиль micro применён к конфигу и пересобран в risk
    assert bot.config.max_position_pct == pytest.approx(1.0)
    assert bot.config.max_total_exposure_pct == pytest.approx(1.0)
    assert bot.risk.max_position_pct == pytest.approx(1.0)
    assert bot.risk.commission_min_rub == 0.0

    # дорогой лот исключён из вселенной
    assert list(bot.instruments) == ["CHEAP"]


def test_bot_startup_real_mode_budget_from_equity(monkeypatch):
    setup_env(monkeypatch)
    bot, bot_mod, candles = make_startup_bot(equity=30_000.0, mode="real")
    monkeypatch.setattr(bot_mod, "load_candles", lambda _conn, t, _i: candles[t])

    bot._setup_capital_profile()

    # real без ACCOUNT_BUDGET_RUB: бюджет = фактический капитал при старте
    assert bot._budget_rub == 30_000.0


def test_bot_startup_explicit_account_budget_wins(monkeypatch):
    setup_env(monkeypatch)
    bot, bot_mod, candles = make_startup_bot(equity=30_000.0, account_budget_rub=25_000.0)
    monkeypatch.setattr(bot_mod, "load_candles", lambda _conn, t, _i: candles[t])

    bot._setup_capital_profile()

    assert bot._budget_rub == 25_000.0


def test_min_tradable_equity_constant():
    assert MIN_TRADABLE_EQUITY_RUB == 10_000.0

"""ML и риск-менеджмент: purging, нетто-Sharpe, лимит экспозиции, kill-switch."""
from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pandas as pd

from bot import TradingBot
from features import build_features, ensure_news_columns
from models.registry import _metrics, walk_forward_lgbm
from strategy import PortfolioState, Position, RiskConfig, decide


# ---------- purging ----------

def test_walk_forward_lgbm_purges_horizon_overlap(candles, monkeypatch):
    import models.registry as reg

    fits: list[pd.DataFrame] = []

    class StubModel:
        model = object()  # предсказывает всегда

        def __init__(self, horizon, use_news_features=False):
            pass

        def fit(self, train):
            fits.append(train.copy())

        def predict_all(self, features):
            return [0.0] * len(features)

    monkeypatch.setattr(reg, "LGBMReturnModel", StubModel)
    horizon = 2
    features = ensure_news_columns(build_features(candles, horizon=horizon))
    test_points = features.index[-60:]
    walk_forward_lgbm(features, test_points, horizon=horizon, refit_every=30, min_train=10)

    assert fits, "обучение должно было случиться"
    first_block_start = test_points[0]
    test_pos = features.index.get_loc(first_block_start)
    train = fits[0]
    last_train_pos = features.index.get_loc(train.index[-1])
    # метка последней обучающей строки использует close[t+horizon]:
    # t должен быть минимум на horizon+1 позиций раньше начала теста
    assert last_train_pos <= test_pos - (horizon + 1)


# ---------- нетто-Sharpe ----------

def test_net_sharpe_penalizes_churn():
    fact = np.array([0.01, 0.02, 0.01, 0.02, 0.01])
    steady = _metrics(np.full(5, 0.01), fact, bars_per_year=1, round_trip_cost=0.01)
    flipping = _metrics(np.array([0.01, -0.01, 0.01, -0.01, 0.01]), fact, bars_per_year=1, round_trip_cost=0.01)

    assert steady["strategy_sharpe_net"] < steady["strategy_sharpe"]  # издержки снижают оценку
    assert flipping["strategy_sharpe_net"] < steady["strategy_sharpe_net"]  # перевороты штрафуются
    assert flipping["strategy_sharpe_net"] < flipping["strategy_sharpe"]


def test_metrics_keys_include_net():
    m = _metrics(np.array([0.01, -0.02]), np.array([0.01, -0.01]))
    assert "strategy_sharpe_net" in m and "strategy_sharpe" in m


# ---------- лимит совокупной экспозиции ----------

def make_portfolio(cash: float, equity: float) -> PortfolioState:
    other = Position("OTHER", "OTHER", 1, 1, 100.0)
    return PortfolioState(cash=cash, equity=equity, positions={"OTHER": other})


def test_decide_blocks_entry_when_exposure_cap_reached():
    risk = RiskConfig(max_position_pct=0.2, max_total_exposure_pct=0.3)
    # открыто 30% портфеля (equity 1000, позиция стоит 300) — места нет
    portfolio = make_portfolio(cash=700.0, equity=1000.0)
    portfolio.positions["OTHER"] = Position("OTHER", "OTHER", 3, 1, 100.0)

    d = decide("T", "T", 100.0, 0.01, 0.0, portfolio, 1, risk)
    assert d.action == "HOLD"
    assert "экспозиции" in d.reason


def test_decide_sizes_down_to_exposure_room():
    risk = RiskConfig(max_position_pct=0.2, max_total_exposure_pct=0.3)
    # открыто 20% — остаётся 10% (100 руб из 1000), хотя доля позиции разрешает 20%
    portfolio = make_portfolio(cash=800.0, equity=1000.0)
    portfolio.positions["OTHER"] = Position("OTHER", "OTHER", 2, 1, 100.0)

    d = decide("T", "T", 100.0, 0.01, 0.0, portfolio, 1, risk)
    assert d.action == "BUY"
    assert d.lots == 1  # не 2 (как разрешила бы доля позиции), а сколько влезает в лимит


def test_decide_without_cap_behaves_as_before():
    risk = RiskConfig(max_position_pct=0.2, max_total_exposure_pct=None)
    portfolio = make_portfolio(cash=800.0, equity=1000.0)
    portfolio.positions["OTHER"] = Position("OTHER", "OTHER", 2, 1, 100.0)

    d = decide("T", "T", 100.0, 0.01, 0.0, portfolio, 1, risk)
    assert d.lots == 2


# ---------- kill-switch ----------

def make_kill_switch_bot() -> TradingBot:
    from notify import Notifier, SmtpConfig

    bot = TradingBot.__new__(TradingBot)
    bot.config = SimpleNamespace(sandbox_initial_rub=1_000_000.0, max_drawdown_pct=0.15)
    bot._budget_rub = None  # не установлен: kill-switch откатывается к sandbox_initial_rub
    bot.notifier = Notifier(SmtpConfig())  # выключен: send() тихо вернёт False
    bot._entries_allowed = True
    return bot


def test_kill_switch_trips_once_below_threshold():
    bot = make_kill_switch_bot()

    bot._check_kill_switch(900_000.0)  # просадка 10% — ещё работает
    assert bot._entries_allowed is True

    bot._check_kill_switch(840_000.0)  # 16% > 15% — сработал
    assert bot._entries_allowed is False


def test_kill_switch_is_one_way():
    bot = make_kill_switch_bot()
    bot._check_kill_switch(500_000.0)
    assert bot._entries_allowed is False
    bot._check_kill_switch(999_999.0)  # восстановление не разблокирует сам
    assert bot._entries_allowed is False


def test_step_ticker_kill_switch_skips_entries():
    from nlp.agenda import AgendaScore
    from notify import Notifier, SmtpConfig
    from models.registry import ModelArtifact

    calls: list[str] = []

    class Recorder:
        def buy(self, *a, **k):
            calls.append("buy")
            return {}

        def sell(self, *a, **k):
            calls.append("sell")
            return {}

        def log_trade(self, row):
            calls.append("journal")

    bot = TradingBot.__new__(TradingBot)
    bot.config = SimpleNamespace(min_model_dir_acc=0.5)
    bot.risk = RiskConfig()
    bot.notifier = Notifier(SmtpConfig())
    bot.instruments = {"AAA": {"ticker": "AAA", "figi": "F1", "lot": 10}}
    bot.artifacts = {"AAA": ModelArtifact(kind="naive_zero", horizon=1, metrics={"directional_acc": 0.9})}
    bot.ticker_agenda = lambda instrument, ticker: AgendaScore(sentiment=0.0)
    bot.predict = lambda ticker, candles, agenda: (0.01, 0.0)
    bot.trader = Recorder()

    candles = pd.DataFrame(
        {"close": [100.0, 100.0], "volume": [1.0, 1.0]},
        index=pd.date_range("2026-09-08", periods=2, freq="h", tz="UTC"),
    )
    bot.refresh_candles = lambda figi, ticker: candles

    portfolio = PortfolioState(cash=1_000_000, equity=1_000_000)
    bot.step_ticker("AAA", bot.instruments["AAA"], portfolio, entries_allowed=False)

    assert calls == []  # ни запроса, ни сделки — вход просто пропущен

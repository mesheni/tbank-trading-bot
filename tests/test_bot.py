"""_load_portfolio: разбор позиций счёта без сети и БД (нужны только trader и instruments)."""
from __future__ import annotations

from types import SimpleNamespace

from bot import TradingBot
from models.registry import ModelArtifact
from strategy import PortfolioState

OZON_FIGI = "BBG00Y5RXXXX"


def make_bot(instruments: dict, raw_portfolio: dict) -> TradingBot:
    bot = TradingBot.__new__(TradingBot)
    bot.instruments = instruments
    bot.trader = SimpleNamespace(portfolio=lambda: raw_portfolio)
    return bot


def make_gate_bot(min_dir_acc: float = 0.5) -> TradingBot:
    bot = TradingBot.__new__(TradingBot)
    bot.config = SimpleNamespace(min_model_dir_acc=min_dir_acc)
    return bot


def test_model_gate_blocks_weak_directional_accuracy():
    # регрессия первого live-прогона: OZON торговался моделью persistence с dir_acc 0.488
    bot = make_gate_bot()
    artifact = ModelArtifact(kind="persistence", horizon=1, metrics={"directional_acc": 0.488})
    assert bot._model_allowed("OZON", artifact) is False


def test_model_gate_blocks_missing_or_nan_metric():
    bot = make_gate_bot()
    assert bot._model_allowed("T", ModelArtifact(kind="arima", horizon=1, metrics={})) is False
    nan = ModelArtifact(kind="ets", horizon=1, metrics={"directional_acc": float("nan")})
    assert bot._model_allowed("T", nan) is False


def test_model_gate_allows_confirmed_edge():
    bot = make_gate_bot()
    artifact = ModelArtifact(kind="lgbm", horizon=1, metrics={"directional_acc": 0.547})
    assert bot._model_allowed("SBER", artifact) is True


def raw_portfolio(positions: dict) -> dict:
    return {
        "positions": positions,
        "cash_rub": 752_350.24,
        "total_amount_rub": 999_968.24,
    }


def test_tracked_position_gets_ticker():
    # регрессия: KeyError 'ticker' на первой же позиции с отслеживаемым figi
    instruments = {"OZON": {"ticker": "OZON", "figi": OZON_FIGI, "lot": 1}}
    raw = raw_portfolio(
        {OZON_FIGI: {"quantity": 92.0, "current_price": 2690.5, "average_position_price": 2690.5}}
    )

    portfolio = make_bot(instruments, raw)._load_portfolio()

    assert isinstance(portfolio, PortfolioState)
    pos = portfolio.positions[OZON_FIGI]
    assert pos.ticker == "OZON"
    assert pos.lots == 92
    assert pos.lot_size == 1
    assert pos.avg_price == 2690.5


def test_instrument_without_ticker_field_falls_back_to_figi():
    # словарь инструмента в старом формате (без ключа ticker) не должен ломать итерацию
    instruments = {"OZON": {"figi": OZON_FIGI, "lot": 1}}
    raw = raw_portfolio(
        {OZON_FIGI: {"quantity": 92.0, "current_price": 2690.5, "average_position_price": 2690.5}}
    )

    portfolio = make_bot(instruments, raw)._load_portfolio()

    assert portfolio.positions[OZON_FIGI].ticker == OZON_FIGI


def test_unknown_figi_position_kept_with_figi_as_ticker():
    # валютная позиция (ключ = instrumentUid) не совпадает ни с одним инструментом
    instruments = {"LKOH": {"ticker": "LKOH", "figi": "BBG004730NMR", "lot": 1}}
    raw = raw_portfolio(
        {"a1b2c3d4-uid": {"quantity": 1000.0, "current_price": 1.0, "average_position_price": 1.0}}
    )

    portfolio = make_bot(instruments, raw)._load_portfolio()

    pos = portfolio.positions["a1b2c3d4-uid"]
    assert pos.ticker == "a1b2c3d4-uid"
    assert pos.lot_size == 1

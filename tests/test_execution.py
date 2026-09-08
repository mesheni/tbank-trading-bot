"""Исполнение ордеров: разбор статусов, частичные заполнения, знаки Quotation."""
from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest

from bot import TradingBot
from models.registry import ModelArtifact
from nlp.agenda import AgendaScore
from strategy import PortfolioState, Position, RiskConfig
from tbank.api import float_to_quotation, parse_order_state, quotation_to_float
from tbank.trader import Trader


# ---------- parse_order_state ----------

POST_ORDER_FILL = {
    "orderId": "o-1",
    "executionReportStatus": "EXECUTION_REPORT_STATUS_FILL",
    "lotsRequested": "92",
    "lotsExecuted": "92",
    "executedOrderPrice": {"units": "2690", "nano": 500000000},
    "totalOrderAmount": {"units": "247", "nano": 566000000},
    "executedCommission": {"units": "0", "nano": 99036600},
}

ORDER_STATE_PARTIAL = {
    "orderId": "o-2",
    "executionReportStatus": "EXECUTION_REPORT_STATUS_PARTIALLYFILL",
    "lotsRequested": "10",
    "lotsExecuted": "4",
    # OrderState: executedOrderPrice — произведение цены на лоты, средняя — в averagePositionPrice
    "executedOrderPrice": {"units": "1076", "nano": 200000000},
    "averagePositionPrice": {"units": "269", "nano": 50000000},
}


def test_parse_post_order_full_fill():
    state = parse_order_state(POST_ORDER_FILL)
    assert state["status"] == "fill"
    assert state["lots_requested"] == 92
    assert state["lots_executed"] == 92
    assert state["avg_exec_price"] == pytest.approx(2690.5)
    assert state["commission"] == pytest.approx(0.0990366)
    assert state["order_id"] == "o-1"


def test_parse_order_state_uses_average_position_price():
    state = parse_order_state(ORDER_STATE_PARTIAL, executed_price_is_total=True)
    assert state["status"] == "partiallyfill"
    assert state["lots_executed"] == 4
    # средняя цена за бумагу из averagePositionPrice, не сумма executedOrderPrice
    assert state["avg_exec_price"] == pytest.approx(269.05)


def test_parse_order_state_falls_back_to_requested_price():
    state = parse_order_state({"orderId": "o-3"}, fallback_price=100.0)
    assert state["status"] == ""
    assert state["lots_executed"] == 0
    assert state["avg_exec_price"] == 100.0


# ---------- Quotation: отрицательные значения ----------

@pytest.mark.parametrize("value", [260.5, -260.5, -0.0000005, 0.0, 12])
def test_quotation_roundtrip(value):
    assert quotation_to_float(float_to_quotation(value)) == pytest.approx(value, abs=1e-8)


def test_quotation_to_float_negative_units_and_nano():
    # proto: знак несут оба поля: -260.5 = units -260, nano -500000000
    assert quotation_to_float({"units": "-260", "nano": -500000000}) == pytest.approx(-260.5)


# ---------- Trader._execute: дозапрос статуса на new/partiallyfill ----------

class PollingAPI:
    def __init__(self, states: list[dict]):
        self.states = list(states)
        self.state_calls = 0

    def post_order(self, account_id, instrument_id, lots, direction, price=None):
        return self.states.pop(0)

    def get_order_state(self, account_id, order_id):
        self.state_calls += 1
        return self.states.pop(0)


def make_trader(api) -> Trader:
    trader = Trader.__new__(Trader)
    trader.api = api
    trader.account_id = "acc-1"
    trader.journal_path = None
    return trader


def test_execute_polls_until_final_status(monkeypatch):
    import tbank.trader as trader_mod

    monkeypatch.setattr(trader_mod.time, "sleep", lambda s: None)
    api = PollingAPI([
        {"order_id": "o1", "status": "new", "lots_requested": 5, "lots_executed": 0,
         "avg_exec_price": 0.0, "commission": 0.0, "total_amount": 0.0, "message": ""},
        {"order_id": "o1", "status": "partiallyfill", "lots_requested": 5, "lots_executed": 2,
         "avg_exec_price": 100.0, "commission": 0.0, "total_amount": 0.0, "message": ""},
        {"order_id": "o1", "status": "fill", "lots_requested": 5, "lots_executed": 5,
         "avg_exec_price": 100.5, "commission": 0.2, "total_amount": 502.5, "message": ""},
    ])
    state = make_trader(api).buy("F1", 5, 100.0)
    assert state["status"] == "fill"
    assert state["lots_executed"] == 5
    assert api.state_calls == 2


def test_execute_returns_immediately_when_filled():
    api = PollingAPI([filled_state(92, 2690.5)])
    state = make_trader(api).sell("F1", 92, 2690.5)
    assert state["status"] == "fill"
    assert api.state_calls == 0


# ---------- step_ticker: журнал по факту исполнения ----------

class FakeTrader:
    def __init__(self, buy_state=None, sell_state=None):
        self.buy_state = buy_state
        self.sell_state = sell_state
        self.journal: list[dict] = []
        self.buys: list[tuple] = []
        self.sells: list[tuple] = []

    def buy(self, figi, lots, price):
        self.buys.append((figi, lots, price))
        return self.buy_state

    def sell(self, figi, lots, price):
        self.sells.append((figi, lots, price))
        return self.sell_state

    def log_trade(self, row):
        self.journal.append(row)


def filled_state(lots: int, price: float, order_id: str = "ord-1") -> dict:
    return {
        "order_id": order_id, "status": "fill", "lots_requested": lots, "lots_executed": lots,
        "avg_exec_price": price, "commission": 0.1, "total_amount": 0.0, "message": "",
    }


def make_step_bot(trader: FakeTrader) -> TradingBot:
    from notify import Notifier, SmtpConfig

    bot = TradingBot.__new__(TradingBot)
    bot.config = SimpleNamespace(min_model_dir_acc=0.5)
    bot.risk = RiskConfig()
    bot.notifier = Notifier(SmtpConfig())  # уведомления выключены
    bot.instruments = {"AAA": {"ticker": "AAA", "figi": "F1", "lot": 10}}
    bot.artifacts = {"AAA": ModelArtifact(kind="naive_zero", horizon=1, metrics={"directional_acc": 0.9})}
    candles = pd.DataFrame(
        {"close": [99.0, 100.0], "volume": [1.0, 1.0]},
        index=pd.date_range("2026-09-08", periods=2, freq="h", tz="UTC"),
    )
    bot.refresh_candles = lambda figi, ticker: candles
    bot.ticker_agenda = lambda instrument, ticker: AgendaScore(sentiment=0.0)
    bot.predict = lambda ticker, candles, agenda: (0.01, 0.0)
    bot.trader = trader
    return bot


def test_step_ticker_partial_buy_journals_executed_lots_only():
    trader = FakeTrader(buy_state=filled_state(120, 99.5))
    bot = make_step_bot(trader)
    portfolio = PortfolioState(cash=1_000_000, equity=1_000_000, positions={})

    bot.step_ticker("AAA", bot.instruments["AAA"], portfolio)

    assert trader.buys == [("F1", 200, 100.0)]  # решение — 200 лотов
    row = trader.journal[0]
    assert row["action"] == "BUY"
    assert row["lots"] == 120  # в журнале — исполненное
    assert row["price"] == 99.5  # и фактическая цена, не запрошенная
    assert portfolio.positions["F1"].lots == 120


def test_step_ticker_rejected_buy_opens_nothing():
    state = dict(filled_state(0, 0.0))
    state["status"] = "rejected"
    trader = FakeTrader(buy_state=state)
    bot = make_step_bot(trader)
    portfolio = PortfolioState(cash=1_000_000, equity=1_000_000, positions={})

    bot.step_ticker("AAA", bot.instruments["AAA"], portfolio)

    assert trader.journal == []
    assert portfolio.positions == {}


def test_step_ticker_partial_sell_keeps_remainder():
    sell_state = dict(filled_state(30, 90.0))
    sell_state.update(lots_requested=50)
    trader = FakeTrader(sell_state=sell_state)
    bot = make_step_bot(trader)
    portfolio = PortfolioState(
        cash=0.0, equity=1_000_000,
        positions={"F1": Position("F1", "AAA", 50, 10, 100.0)},
    )
    # стоп-лосс: текущая цена 100 → нет; сделаем выход через разворот прогноза
    bot.predict = lambda ticker, candles, agenda: (-0.05, 0.0)

    bot.step_ticker("AAA", bot.instruments["AAA"], portfolio)

    row = trader.journal[0]
    assert row["action"] == "SELL" and row["lots"] == 30
    assert portfolio.positions["F1"].lots == 20  # недопроданный остаток держим

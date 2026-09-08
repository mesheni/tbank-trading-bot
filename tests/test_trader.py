"""Trader.initialize_balance и cli.cmd_normalize: бюджет вносится один раз и только на пустой счёт."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import cli
import tbank.trader as trader_mod
from tbank.trader import Trader


class StubAPI:
    def __init__(self, total: float, cash: float, positions: dict | None = None):
        self.data = {"total_amount_rub": total, "cash_rub": cash, "positions": positions or {}}
        self.pay_ins: list[float] = []
        self.paid_out = 0.0

    def get_accounts(self) -> list[dict]:
        return [{"id": "acc-1"}]

    def get_portfolio(self, account_id: str) -> dict:
        return self.data

    def pay_in(self, account_id: str, amount: float) -> None:
        self.pay_ins.append(amount)

    def pay_out(self, account_id: str, amount: float) -> None:
        self.paid_out += amount


def make_trader(api: StubAPI) -> Trader:
    trader = Trader.__new__(Trader)
    trader.api = api
    trader.account_id = "acc-1"
    trader.journal_path = None
    return trader


def test_empty_account_gets_initial_budget():
    api = StubAPI(total=0.0, cash=0.0)
    result = make_trader(api).initialize_balance(1_000_000.0)
    assert result == 1_000_000.0
    assert api.pay_ins == [1_000_000.0]


def test_funded_account_never_touched():
    # регрессия: раньше бот в цикле доливал кэш при просадке ниже 50 тыс
    # («Баланс 4511 руб < минимум 50000: пополняем на 995489») — бюджет раздувался
    api = StubAPI(total=999_968.0, cash=4_511.0, positions={"f1": {}})
    result = make_trader(api).initialize_balance(1_000_000.0)
    assert result == pytest.approx(999_968.0)
    assert api.pay_ins == []


def test_drawdown_not_refilled():
    # просадка ниже бюджета — часть торговли; восполнять её нельзя
    api = StubAPI(total=812_000.0, cash=12_000.0, positions={"f1": {}})
    make_trader(api).initialize_balance(1_000_000.0)
    assert api.pay_ins == []


def test_cmd_normalize_pays_out_excess(tmp_path, monkeypatch):
    stub = StubAPI(total=1_995_000.0, cash=995_000.0, positions={"f1": {}})
    monkeypatch.setattr(cli, "make_api", lambda cfg: stub)
    monkeypatch.setattr(
        trader_mod,
        "Trader",
        lambda api, path: SimpleNamespace(
            account_id="acc-1", portfolio=lambda: stub.get_portfolio("acc-1")
        ),
    )
    config = SimpleNamespace(mode="sandbox", reports_dir=tmp_path, sandbox_initial_rub=1_000_000.0)

    rc = cli.cmd_normalize(config)

    assert rc == 0
    assert stub.paid_out == pytest.approx(995_000.0)


def test_cmd_normalize_noop_within_budget(tmp_path, monkeypatch):
    stub = StubAPI(total=1_000_000.5, cash=500_000.5, positions={"f1": {}})
    monkeypatch.setattr(cli, "make_api", lambda cfg: stub)
    monkeypatch.setattr(
        trader_mod,
        "Trader",
        lambda api, path: SimpleNamespace(
            account_id="acc-1", portfolio=lambda: stub.get_portfolio("acc-1")
        ),
    )
    config = SimpleNamespace(mode="sandbox", reports_dir=tmp_path, sandbox_initial_rub=1_000_000.0)

    rc = cli.cmd_normalize(config)

    assert rc == 0
    assert stub.paid_out == 0.0


def test_cmd_normalize_refuses_real_mode():
    rc = cli.cmd_normalize(SimpleNamespace(mode="real"))
    assert rc == 2

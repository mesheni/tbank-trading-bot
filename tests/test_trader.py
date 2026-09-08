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


def make_trader_stub(stub: StubAPI):
    flows: list[tuple[float, str]] = []

    def log_flow(amount: float, kind: str, reason: str) -> None:
        flows.append((amount, kind))

    ns = SimpleNamespace(
        account_id="acc-1",
        portfolio=lambda: stub.get_portfolio("acc-1"),
        log_flow=log_flow,
    )
    ns.flows = flows
    return ns


def test_cmd_normalize_pays_out_excess(tmp_path, monkeypatch):
    stub = StubAPI(total=1_995_000.0, cash=995_000.0, positions={"f1": {}})
    monkeypatch.setattr(cli, "make_api", lambda cfg: stub)
    trader_stub = make_trader_stub(stub)
    monkeypatch.setattr(trader_mod, "Trader", lambda api, path: trader_stub)
    config = SimpleNamespace(mode="sandbox", reports_dir=tmp_path, sandbox_initial_rub=1_000_000.0)

    rc = cli.cmd_normalize(config)

    assert rc == 0
    assert stub.paid_out == pytest.approx(995_000.0)
    # компенсирующая пара: restore-запись и вывод — итог по бюджету нулевой
    assert trader_stub.flows == [
        (pytest.approx(995_000.0), "adjust"),
        (pytest.approx(-995_000.0), "withdraw"),
    ]


def test_cmd_normalize_noop_within_budget(tmp_path, monkeypatch):
    stub = StubAPI(total=1_000_000.5, cash=500_000.5, positions={"f1": {}})
    monkeypatch.setattr(cli, "make_api", lambda cfg: stub)
    trader_stub = make_trader_stub(stub)
    monkeypatch.setattr(trader_mod, "Trader", lambda api, path: trader_stub)
    config = SimpleNamespace(mode="sandbox", reports_dir=tmp_path, sandbox_initial_rub=1_000_000.0)

    rc = cli.cmd_normalize(config)

    assert rc == 0
    assert stub.paid_out == 0.0
    assert trader_stub.flows == []


def test_cmd_normalize_refuses_real_mode():
    rc = cli.cmd_normalize(SimpleNamespace(mode="real"))
    assert rc == 2


def test_log_flow_appends_to_csv(tmp_path):
    trader = make_trader(StubAPI(total=0.0, cash=0.0))
    trader.journal_path = tmp_path / "journal.csv"

    trader.log_flow(974_735.0, "adjust", "восстановление учёта")
    trader.log_flow(-974_735.0, "withdraw", "вывод излишка")

    import pandas as pd

    flows = pd.read_csv(tmp_path / "flows.csv")
    assert list(flows.columns) == ["time", "amount_rub", "kind", "reason"]
    assert flows["amount_rub"].tolist() == [974735.0, -974735.0]
    assert flows["kind"].tolist() == ["adjust", "withdraw"]


def test_net_invested_sums_budget_and_flows():
    import pandas as pd

    from cli import _net_invested

    empty = pd.DataFrame(columns=["time", "amount_rub", "kind", "reason"])
    assert _net_invested(1_000_000.0, empty) == 1_000_000.0
    # компенсирующая пара normalize (adjust + withdraw) даёт ноль
    flows = pd.DataFrame(
        {
            "time": ["t1", "t2"],
            "amount_rub": [974_735.0, -974_735.0],
            "kind": ["adjust", "withdraw"],
            "reason": ["", ""],
        }
    )
    assert _net_invested(1_000_000.0, flows) == 1_000_000.0


def test_period_pnl_excludes_flows():
    import pandas as pd

    from cli import _period_pnl

    times = pd.to_datetime(
        ["2026-09-08 10:00", "2026-09-08 14:33", "2026-09-08 16:00"], utc=True
    )
    equity = pd.DataFrame(
        {"time": times, "total_rub": [1_000_000.0, 1_995_489.0, 1_974_735.0]}
    )
    flows = pd.DataFrame(
        {
            "time": [times[1]],
            "amount_rub": [995_489.0],
            "kind": ["deposit"],
            "reason": [""],
        }
    )

    res = _period_pnl(equity, flows, times[0])

    assert res is not None
    pnl, base, moved, coverage = res
    # дельта 974 735 минус пополнение 995 489 = −20 754: пополнение не прибыль
    assert pnl == pytest.approx(-20_754.0)
    assert base == pytest.approx(1_000_000.0)
    assert moved == pytest.approx(995_489.0)


def test_period_pnl_window_outside_data_returns_none():
    import pandas as pd

    from cli import _period_pnl

    times = pd.to_datetime(["2026-09-08 10:00", "2026-09-08 16:00"], utc=True)
    equity = pd.DataFrame({"time": times, "total_rub": [1.0, 2.0]})
    flows = pd.DataFrame(columns=["time", "amount_rub", "kind", "reason"])

    assert _period_pnl(equity, flows, pd.Timestamp("2026-09-09", tz="UTC")) is None

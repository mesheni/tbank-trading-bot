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

    def get_accounts(self) -> list[dict]:
        return [{"id": "acc-1"}]

    def get_portfolio(self, account_id: str) -> dict:
        return self.data

    def pay_in(self, account_id: str, amount: float) -> None:
        self.pay_ins.append(amount)


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


def make_trader_stub(total: float):
    flows: list[tuple[float, str]] = []

    def log_flow(amount: float, kind: str, reason: str) -> None:
        flows.append((amount, kind))

    ns = SimpleNamespace(
        account_id="old-acc",
        portfolio=lambda: {"total_amount_rub": total, "cash_rub": 0.0, "positions": {"f1": {}}},
        log_flow=log_flow,
    )
    ns.flows = flows
    return ns


class AccountsAPI:
    def get_accounts(self) -> list[dict]:
        return [{"id": "old-acc"}]


def test_cmd_normalize_reports_excess_and_does_nothing(tmp_path, monkeypatch):
    # в T-Invest API нет вывода из sandbox: normalize лишь объясняет и отправляет в reset-sandbox
    monkeypatch.setattr(cli, "make_api", lambda cfg: AccountsAPI())
    trader_stub = make_trader_stub(1_995_000.0)
    monkeypatch.setattr(trader_mod, "Trader", lambda api, path: trader_stub)
    config = SimpleNamespace(mode="sandbox", reports_dir=tmp_path, sandbox_initial_rub=1_000_000.0)

    rc = cli.cmd_normalize(config)

    assert rc == 1
    assert trader_stub.flows == []


def test_cmd_normalize_noop_within_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "make_api", lambda cfg: AccountsAPI())
    trader_stub = make_trader_stub(1_000_000.5)
    monkeypatch.setattr(trader_mod, "Trader", lambda api, path: trader_stub)
    config = SimpleNamespace(mode="sandbox", reports_dir=tmp_path, sandbox_initial_rub=1_000_000.0)

    rc = cli.cmd_normalize(config)

    assert rc == 0
    assert trader_stub.flows == []


class ResetAPI:
    def __init__(self):
        self.closed: list[str] = []
        self.paid_in: list[tuple[str, float]] = []

    def get_accounts(self) -> list[dict]:
        return [{"id": "old-acc"}]

    def close_sandbox_account(self, account_id: str) -> None:
        self.closed.append(account_id)

    def open_sandbox_account(self) -> str:
        return "new-acc"

    def pay_in(self, account_id: str, amount: float) -> None:
        self.paid_in.append((account_id, amount))


def test_cmd_reset_sandbox_full_flow(tmp_path, monkeypatch):
    api_stub = ResetAPI()
    monkeypatch.setattr(cli, "make_api", lambda cfg: api_stub)
    monkeypatch.setattr(trader_mod, "Trader", lambda api, path: make_trader_stub(1_995_000.0))
    monkeypatch.setattr("builtins.input", lambda prompt="": "YES")
    for name in ("journal.csv", "equity_live.csv"):
        (tmp_path / name).write_text("x,y\n1,2\n", encoding="utf-8")
    config = SimpleNamespace(mode="sandbox", reports_dir=tmp_path, sandbox_initial_rub=1_000_000.0)

    rc = cli.cmd_reset_sandbox(config, assume_yes=False)

    assert rc == 0
    assert api_stub.closed == ["old-acc"]
    assert api_stub.paid_in == [("new-acc", 1_000_000.0)]
    # журналы старого счёта заархивированы, на их месте чисто
    assert not (tmp_path / "journal.csv").exists()
    assert list(tmp_path.glob("journal_*.csv"))
    assert not (tmp_path / "equity_live.csv").exists()
    assert list(tmp_path.glob("equity_live_*.csv"))


def test_cmd_reset_sandbox_cancelled_without_yes(tmp_path, monkeypatch):
    api_stub = ResetAPI()
    monkeypatch.setattr(cli, "make_api", lambda cfg: api_stub)
    monkeypatch.setattr(trader_mod, "Trader", lambda api, path: make_trader_stub(1_995_000.0))
    monkeypatch.setattr("builtins.input", lambda prompt="": "no")
    config = SimpleNamespace(mode="sandbox", reports_dir=tmp_path, sandbox_initial_rub=1_000_000.0)

    rc = cli.cmd_reset_sandbox(config, assume_yes=False)

    assert rc == 1
    assert api_stub.closed == []
    assert api_stub.paid_in == []
    assert (tmp_path / "journal.csv").exists() is False  # файлов и не было — ничего не тронуто


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

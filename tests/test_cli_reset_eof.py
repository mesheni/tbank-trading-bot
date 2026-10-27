"""reset-sandbox: неинтерактивный stdin не должен ронять команду трейсбеком."""
from __future__ import annotations

from types import SimpleNamespace

import cli
import tbank.trader as trader_mod


class _StubAPI:
    def __init__(self) -> None:
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


def _make_trader(api, path, account_id=""):
    return SimpleNamespace(
        account_id="old-acc",
        portfolio=lambda: {"total_amount_rub": 1_000_000.0, "positions": {}},
    )


def _raise_eof(prompt: str = "") -> str:
    raise EOFError


def test_confirm_reset_returns_none_on_eof(monkeypatch, capsys) -> None:
    monkeypatch.setattr("builtins.input", _raise_eof)

    assert cli._confirm_reset(False) is None
    assert "Нет интерактивного ввода — передайте --yes для подтверждения" in capsys.readouterr().err


def test_cmd_reset_sandbox_eof_returns_2(tmp_path, monkeypatch, capsys) -> None:
    api = _StubAPI()
    monkeypatch.setattr(cli, "make_api", lambda config: api)
    monkeypatch.setattr(trader_mod, "Trader", _make_trader)
    monkeypatch.setattr("builtins.input", _raise_eof)
    config = SimpleNamespace(
        mode="sandbox",
        reports_dir=tmp_path,
        sandbox_initial_rub=1_000_000.0,
    )

    rc = cli.cmd_reset_sandbox(config, assume_yes=False)

    assert rc == 2
    assert "Нет интерактивного ввода — передайте --yes для подтверждения" in capsys.readouterr().err
    assert api.closed == []
    assert api.paid_in == []

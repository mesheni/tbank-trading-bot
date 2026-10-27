"""Выбор счёта Trader: в real без ACCOUNT_ID при нескольких счетах — ошибка."""
from __future__ import annotations

import pytest

from tbank.trader import Trader


class FakeAPI:
    def __init__(self, accounts: list[dict], is_sandbox: bool):
        self.accounts = accounts
        self.is_sandbox = is_sandbox
        self.opened_sandbox = 0

    def get_accounts(self) -> list[dict]:
        return list(self.accounts)

    def open_sandbox_account(self) -> str:
        self.opened_sandbox += 1
        return "sandbox-new"


def _trader(api: FakeAPI, tmp_path) -> Trader:
    return Trader(api, tmp_path / "journal.csv")


def test_real_multiple_accounts_without_id_raises(tmp_path):
    api = FakeAPI([{"id": "acc-1"}, {"id": "acc-2"}], is_sandbox=False)
    with pytest.raises(RuntimeError) as exc:
        _trader(api, tmp_path)
    assert str(exc.value) == (
        "ACCOUNT_ID не задан, а счетов в режиме real несколько (2) — задайте ACCOUNT_ID в .env"
    )
    assert api.opened_sandbox == 0


def test_real_single_account_without_id_works(tmp_path):
    api = FakeAPI([{"id": "acc-1"}], is_sandbox=False)
    assert _trader(api, tmp_path).account_id == "acc-1"


def test_sandbox_multiple_accounts_uses_first(tmp_path):
    api = FakeAPI([{"id": "acc-1"}, {"id": "acc-2"}], is_sandbox=True)
    assert _trader(api, tmp_path).account_id == "acc-1"

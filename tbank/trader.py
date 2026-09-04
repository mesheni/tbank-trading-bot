"""Исполнение сделок в sandbox: открытие счета, пополнение, ордера, журнал."""
from __future__ import annotations

import csv
import datetime as dt
import logging
from pathlib import Path

from .api import TBankAPI

log = logging.getLogger(__name__)

BUY = "ORDER_DIRECTION_BUY"
SELL = "ORDER_DIRECTION_SELL"


class Trader:
    """Работа со счётом sandbox: гарантированный аккаунт, балансовый минимум, сделки, журнал."""

    def __init__(self, api: TBankAPI, journal_path: Path):
        self.api = api
        self.journal_path = journal_path
        self.account_id = self._ensure_account()

    def _ensure_account(self) -> str:
        accounts = self.api.get_accounts()
        if accounts:
            return accounts[0].get("id", "")
        log.info("Sandbox-счёт не найден, открываем новый")
        return self.api.open_sandbox_account()

    def ensure_balance(self, min_rub: float, top_up_to: float) -> float:
        portfolio = self.portfolio()
        cash = portfolio["cash_rub"]
        if cash < min_rub:
            add = top_up_to - cash
            if add > 0:
                log.info("Баланс %.0f руб < минимум %.0f: пополняем на %.0f", cash, min_rub, add)
                self.api.pay_in(self.account_id, add)
                cash += add
        return cash

    def portfolio(self) -> dict:
        return self.api.get_portfolio(self.account_id)

    def buy(self, instrument_id: str, lots: int, price: float) -> dict:
        return self.api.post_order(self.account_id, instrument_id, lots, BUY, price=price)

    def sell(self, instrument_id: str, lots: int, price: float | None = None) -> dict:
        return self.api.post_order(self.account_id, instrument_id, lots, SELL, price=price)

    def log_trade(self, row: dict) -> None:
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        is_new = not self.journal_path.exists()
        with open(self.journal_path, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(row))
            if is_new:
                writer.writeheader()
            writer.writerow(row)


def make_journal_row(ticker: str, action: str, lots: int, price: float, reason: str, order_id: str = "") -> dict:
    return {
        "time": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "ticker": ticker,
        "action": action,
        "lots": lots,
        "price": round(price, 4),
        "reason": reason,
        "order_id": order_id,
    }

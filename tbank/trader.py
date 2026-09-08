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
    """Работа со счётом sandbox: гарантированный аккаунт, разовый стартовый бюджет, сделки, журнал."""

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

    def initialize_balance(self, initial_rub: float) -> float:
        """Разовое наполнение счёта бюджетом: только если счёт полностью пуст.

        Живой счёт (есть деньги или позиции) никогда не трогаем — бот торгует
        строго данным бюджетом, убыток и прибыль остаются внутри него.
        """
        portfolio = self.portfolio()
        total = portfolio["total_amount_rub"]
        if total >= 1.0:
            log.info(
                "Счёт не пуст: %.0f руб (кэш %.0f, позиций %d) — бюджет не пополняем",
                total, portfolio["cash_rub"], len(portfolio["positions"]),
            )
            return total
        log.info("Счёт пуст: вносим стартовый бюджет %.0f руб", initial_rub)
        self.api.pay_in(self.account_id, initial_rub)
        return initial_rub

    def log_flow(self, amount_rub: float, kind: str, reason: str) -> None:
        """Дописывает движение денег в reports/flows.csv — основа P&L без пополнений.

        kind: deposit | withdraw | adjust (bookkeeping-компенсация неучтённого
        движения). Стартовый бюджет из SANDBOX_INITIAL_RUB не журналируется:
        он всегда учитывается как база в отчёте.
        """
        path = self.journal_path.parent / "flows.csv"
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        is_new = not path.exists()
        with open(path, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if is_new:
                writer.writerow(["time", "amount_rub", "kind", "reason"])
            writer.writerow(
                [
                    dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                    round(amount_rub, 2),
                    kind,
                    reason,
                ]
            )

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

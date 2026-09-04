"""Тесты наблюдаемости: кривая капитала и разбор портфеля по реальной структуре API."""
from __future__ import annotations

from pathlib import Path

from bot import log_equity
from tbank.api import parse_portfolio


def test_log_equity_creates_and_appends(tmp_path):
    path = tmp_path / "reports" / "equity_live.csv"
    log_equity(path, 1_000_000.0, 750_000.0, 250_000.0)
    log_equity(path, 1_005_000.5, 800_000.5, 205_000.0)

    import csv

    with open(path, encoding="utf-8") as f:
        rows = list(csv.reader(f))
    assert rows[0] == ["time", "total_rub", "cash_rub", "positions_rub"]
    assert len(rows) == 3  # заголовок + 2 точки
    assert rows[1][1:] == ["1000000.0", "750000.0", "250000.0"]
    assert rows[2][1:] == ["1005000.5", "800000.5", "205000.0"]
    assert rows[1][0] and "T" in rows[1][0]  # timestamp заполнен


def test_parse_portfolio_real_structure():
    """Структура из реального ответа OperationsService/GetPortfolio (2026-09)."""
    raw = {
        "totalAmountShares": {"currency": "rub", "units": "150000", "nano": 500000000},
        "totalAmountBonds": {"currency": "rub", "units": "0", "nano": 0},
        "totalAmountEtf": {"currency": "rub", "units": "0", "nano": 0},
        "totalAmountCurrencies": {"currency": "rub", "units": "850000", "nano": 500000000},
        "totalAmountFutures": {"currency": "rub", "units": "0", "nano": 0},
        "totalAmountOptions": {"currency": "rub", "units": "0", "nano": 0},
        "expectedYield": {"units": "250", "nano": 0},
        "positions": [
            {
                "instrumentType": "share",
                "figi": "BBG004730N88",
                "instrumentUid": "e6123145-9665-43e0-8413-cd61b8aa9b13",
                "quantity": {"units": "50", "nano": 0},
                "currentPrice": {"units": "300", "nano": 100000000},
                "averagePositionPrice": {"units": "274", "nano": 280000000},
            }
        ],
        "accountId": "8b925b21-949e-45cf-860a-15b19ccf1848",
    }
    portfolio = parse_portfolio(raw)
    # итог = сумма разбивки (150000.5 + 850000.5), а не несуществующее totalAmountPortfolio
    assert portfolio["total_amount_rub"] == 1_000_001.0
    assert portfolio["cash_rub"] == 850_000.5
    assert len(portfolio["positions"]) == 1
    pos = portfolio["positions"]["BBG004730N88"]
    assert pos["quantity"] == 50
    assert pos["current_price"] == 300.1
    assert pos["average_position_price"] == 274.28


def test_parse_portfolio_empty():
    portfolio = parse_portfolio({"positions": []})
    assert portfolio["total_amount_rub"] == 0.0
    assert portfolio["cash_rub"] == 0.0
    assert portfolio["positions"] == {}

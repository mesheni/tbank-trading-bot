"""Высокоуровневые методы T-Invest API, нужные боту.

Имена методов соответствуют proto-контракту (UsersService, SandboxService,
InstrumentsService, MarketDataService, OrdersService, OperationsService).
"""
from __future__ import annotations

import datetime as dt
import logging
import uuid

from .rest import APIError, TBankRestClient

log = logging.getLogger(__name__)


def quotation_to_float(quotation: dict | None) -> float:
    """{\"units\": \"260\", \"nano\": 500000000} -> 260.5"""
    if not quotation:
        return 0.0
    units = int(quotation.get("units", 0))
    nano = int(quotation.get("nano", 0))
    sign = -1.0 if units < 0 else 1.0
    return units + sign * nano / 1e9


def float_to_quotation(value: float) -> dict:
    units = int(value)
    nano = int(round((value - units) * 1e9))
    if nano >= int(1e9):
        units += 1
        nano -= int(1e9)
    return {"units": str(units), "nano": nano}


def float_to_quotation_rub(value: float) -> dict:
    """MoneyValue с валютой (для SandboxPayIn.amount)."""
    quotation = float_to_quotation(value)
    quotation["currency"] = "rub"
    return quotation


def parse_portfolio(data: dict) -> dict:
    """Разбирает ответ OperationsService/GetPortfolio.

    Итог = сумма разбивки totalAmount{Shares,Bonds,Etf,Currencies,Futures,Options}.
    """
    parts = (
        "totalAmountShares",
        "totalAmountBonds",
        "totalAmountEtf",
        "totalAmountCurrencies",
        "totalAmountFutures",
        "totalAmountOptions",
    )
    total = sum(quotation_to_float(data.get(key)) for key in parts)
    positions: dict[str, dict] = {}
    for pos in data.get("positions", []):
        key = pos.get("figi") or pos.get("instrumentUid") or ""
        if not key:
            continue
        positions[key] = {
            "figi": pos.get("figi", ""),
            "instrument_uid": pos.get("instrumentUid", ""),
            "instrument_type": pos.get("instrumentType", ""),
            "quantity": quotation_to_float(pos.get("quantity")),
            "current_price": quotation_to_float(pos.get("currentPrice")),
            "average_position_price": quotation_to_float(pos.get("averagePositionPrice")),
        }
    return {
        "positions": positions,
        "total_amount_rub": total,
        "cash_rub": quotation_to_float(data.get("totalAmountCurrencies")),
    }


class TBankAPI:
    """Обёртки над методами API, единые для prod и sandbox."""

    USERS = "tinkoff.public.invest.api.contract.v1.UsersService"
    SANDBOX = "tinkoff.public.invest.api.contract.v1.SandboxService"
    INSTRUMENTS = "tinkoff.public.invest.api.contract.v1.InstrumentsService"
    MARKET_DATA = "tinkoff.public.invest.api.contract.v1.MarketDataService"
    ORDERS = "tinkoff.public.invest.api.contract.v1.OrdersService"
    OPERATIONS = "tinkoff.public.invest.api.contract.v1.OperationsService"

    def __init__(self, client: TBankRestClient):
        self.client = client

    # ---------- Счета ----------

    def get_accounts(self) -> list[dict]:
        method = (
            f"{self.SANDBOX}/GetSandboxAccounts"
            if self.client.host.endswith("sandbox-invest-public-api.tbank.ru")
            else f"{self.USERS}/GetAccounts"
        )
        data = self.client.post(method)
        return data.get("accounts", [])

    def open_sandbox_account(self) -> str:
        if not self.client.host.endswith("sandbox-invest-public-api.tbank.ru"):
            raise APIError(0, "open_sandbox_account доступен только в sandbox")
        data = self.client.post(f"{self.SANDBOX}/OpenSandboxAccount")
        account_id = data.get("accountId", "")
        if not account_id:
            raise APIError(0, "не удалось открыть sandbox-счёт", str(data)[:300])
        log.info("Открыт sandbox-счёт %s", account_id)
        return account_id

    def pay_in(self, account_id: str, amount_rub: float) -> None:
        payload = {
            "accountId": account_id,
            "amount": float_to_quotation_rub(amount_rub),
        }
        self.client.post(f"{self.SANDBOX}/SandboxPayIn", payload)

    # ---------- Инструменты ----------

    def resolve_instruments(self, tickers: list[str]) -> dict[str, dict]:
        """Тикер -> {figi, uid, name, lot, class_code}. Берём акции базового списка."""
        data = self.client.post(
            f"{self.INSTRUMENTS}/Shares",
            {"instrumentStatus": "INSTRUMENT_STATUS_BASE"},
        )
        wanted = {t.upper() for t in tickers}
        found: dict[str, dict] = {}
        for share in data.get("instruments", []):
            ticker = str(share.get("ticker", "")).upper()
            if ticker in wanted and ticker not in found:
                if share.get("apiTradeAvailableFlag") is False and share.get("buyAvailableFlag") is False:
                    continue
                found[ticker] = {
                    "figi": share.get("figi", ""),
                    "uid": share.get("uid", ""),
                    "name": share.get("name", ""),
                    "lot": int(share.get("lot", 1)) or 1,
                    "class_code": share.get("classCode", ""),
                    "min_price_increment": quotation_to_float(share.get("minPriceIncrement")),
                }
        missing = wanted - set(found)
        if missing:
            log.warning("Инструменты не найдены в базовом списке акций: %s", ", ".join(sorted(missing)))
        return found

    def trading_schedules(self, figi: str, days: int = 7) -> list[tuple[dt.datetime, dt.datetime]]:
        """Интервалы [начало, конец] торгов по инструменту на ближайшие дни (MSK)."""
        now = dt.datetime.now(dt.timezone.utc)
        data = self.client.post(
            f"{self.INSTRUMENTS}/TradingSchedules",
            {"from": (now).isoformat(), "to": (now + dt.timedelta(days=days)).isoformat()},
        )
        result: list[tuple[dt.datetime, dt.datetime]] = []
        for exchange in data.get("exchanges", []):
            if exchange.get("exchange") not in {"MOEX", "MOEX_EVENINGDAY", ""}:
                continue
            for day in exchange.get("days", []):
                instrument = day.get("instrumentInfo", {}) or {}
                if instrument.get("tradingStatus", "").startswith("CUSTOM"):
                    continue
                start, end = instrument.get("startDate"), instrument.get("endDate")
                if not start or not end:
                    continue
                result.append(
                    (
                        dt.datetime.fromisoformat(start.replace("Z", "+00:00")),
                        dt.datetime.fromisoformat(end.replace("Z", "+00:00")),
                    )
                )
        return result

    # ---------- Рыночные данные ----------

    def get_candles(self, instrument_id: str, interval_enum: str, from_dt: dt.datetime, to_dt: dt.datetime) -> list[dict]:
        """Свечи за [from_dt, to_dt]. Возвращает список нормализованных строк."""
        payload = {
            "instrumentId": instrument_id,
            "interval": interval_enum,
            "from": from_dt.astimezone(dt.timezone.utc).isoformat(),
            "to": to_dt.astimezone(dt.timezone.utc).isoformat(),
        }
        try:
            data = self.client.post(f"{self.MARKET_DATA}/GetCandles", payload)
        except APIError as exc:
            if exc.status == 400:
                # Старый контракт принимает figi вместо instrumentId
                payload.pop("instrumentId", None)
                payload["figi"] = instrument_id
                data = self.client.post(f"{self.MARKET_DATA}/GetCandles", payload)
            else:
                raise

        rows = []
        for candle in data.get("candles", []):
            rows.append(
                {
                    "time": candle.get("time"),
                    "open": quotation_to_float(candle.get("open")),
                    "high": quotation_to_float(candle.get("high")),
                    "low": quotation_to_float(candle.get("low")),
                    "close": quotation_to_float(candle.get("close")),
                    "volume": int(candle.get("volume", 0)),
                    "is_complete": bool(candle.get("isComplete", True)),
                }
            )
        return rows

    def get_last_price(self, instrument_id: str) -> float:
        data = self.client.post(
            f"{self.MARKET_DATA}/GetLastPrices", {"instrumentId": [instrument_id]}
        )
        prices = data.get("lastPrices", [])
        if not prices:
            return 0.0
        return quotation_to_float(prices[0].get("price"))

    # ---------- Новости ----------

    def get_news(self, instrument_ids: list[str] | None = None, max_pages: int = 5) -> list[dict]:
        """Страницы новостей. Серверная фильтрация по инструменту не поддерживается —
        если задан instrument_ids, фильтруем на нашей стороне по instrument_uid.
        Новости без привязки к инструменту (макро) при фильтре отбрасываются
        (они включаются на уровне хранилища — см. load_news)."""
        items: list[dict] = []
        cursor: str | None = None
        for _ in range(max_pages):
            payload = {"nextCursor": cursor} if cursor else {}
            data = self.client.post(f"{self.INSTRUMENTS}/News", payload)
            for raw in data.get("items") or data.get("news") or []:
                items.append(self._normalize_news(raw))
            cursor = data.get("nextCursor")
            if not cursor or not data.get("hasNext", True):
                break
        if instrument_ids:
            wanted = set(instrument_ids)
            items = [i for i in items if i.get("instrument_uid") in wanted]
        return items

    @staticmethod
    def _normalize_news(item: dict) -> dict:
        def first_of(names: tuple[str, ...]):
            for name in names:
                if name in item and item[name] not in (None, ""):
                    return item[name]
            return None

        # привязка к инструментам: [{"instrument": {"instrumentUid": ..., "ticker": ...}}, ...]
        uids: list[str] = []
        refs = item.get("instrumentId") or item.get("instruments") or []
        if isinstance(refs, list):
            for ref in refs:
                if not isinstance(ref, dict):
                    continue
                instrument = ref.get("instrument", ref)
                uid = instrument.get("instrumentUid") or instrument.get("uid")
                if uid:
                    uids.append(str(uid))
        if not uids:
            legacy = first_of(("instrumentUid", "instrument_uid", "figi"))
            if legacy:
                uids = [str(legacy)]

        return {
            "news_id": str(first_of(("id", "newsId", "news_id")) or ""),
            "pub_time": str(first_of(("ts", "pubTime", "pub_time", "publishedAt", "time")) or ""),
            "title": str(first_of(("title", "header")) or ""),
            "text": str(first_of(("content", "text", "body")) or ""),
            "instrument_uid": uids[0] if uids else "",
            "instrument_uids": uids,
        }

    # ---------- Ордера и портфель ----------

    def post_order(
        self,
        account_id: str,
        instrument_id: str,
        quantity_lots: int,
        direction: str,
        price: float | None = None,
        order_type: str = "ORDER_TYPE_LIMIT",
    ) -> dict:
        payload = {
            "instrumentId": instrument_id,
            "quantity": int(quantity_lots),
            "direction": direction,  # ORDER_DIRECTION_BUY | ORDER_DIRECTION_SELL
            "orderType": order_type,  # ORDER_TYPE_LIMIT | ORDER_TYPE_MARKET
            "orderId": str(uuid.uuid4()),
            "accountId": account_id,
        }
        if price is not None and order_type == "ORDER_TYPE_LIMIT":
            payload["price"] = float_to_quotation(price)
        try:
            return self.client.post(f"{self.ORDERS}/PostOrder", payload)
        except APIError as exc:
            if exc.status == 400 and order_type == "ORDER_TYPE_LIMIT":
                payload["orderType"] = "ORDER_TYPE_MARKET"
                payload.pop("price", None)
                return self.client.post(f"{self.ORDERS}/PostOrder", payload)
            raise

    def get_portfolio(self, account_id: str) -> dict:
        data = self.client.post(f"{self.OPERATIONS}/GetPortfolio", {"accountId": account_id})
        return parse_portfolio(data)

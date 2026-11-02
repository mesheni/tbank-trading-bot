"""Исполнение сделок в sandbox: открытие счета, пополнение, ордера, журнал."""
from __future__ import annotations

import csv
import datetime as dt
import json
import logging
import os
import time
from pathlib import Path

from .api import TBankAPI

log = logging.getLogger(__name__)

BUY = "ORDER_DIRECTION_BUY"
SELL = "ORDER_DIRECTION_SELL"

# финальные статусы: заявка доработала; new/partiallyfill ещё могут исполниться дальше
FINAL_STATUSES = {"fill", "cancelled", "rejected"}

# реестр незавершённых заявок переживает рестарт: reports/pending_orders.json
PENDING_ORDERS_FILE = "pending_orders.json"
# повреждённый файл блокирует новые заявки по всем инструментам (fail closed)
PENDING_CORRUPT_KEY = "*"
PENDING_CORRUPT_VALUE = "corrupt-pending-file"


class Trader:
    """Работа со счётом sandbox: гарантированный аккаунт, разовый стартовый бюджет, сделки, журнал."""

    def __init__(self, api: TBankAPI, journal_path: Path, account_id: str = ""):
        self.api = api
        self.journal_path = journal_path
        self._configured_account_id = (account_id or "").strip()
        self.account_id = self._ensure_account()
        # незавершённые заявки: instrument_id -> order_id; новая заявка по тому же
        # инструменту не выставляется, пока прошлая может исполниться.
        # Реестр читается с диска: заявка, живая на бирже, не должна потеряться
        # при рестарте процесса — иначе бот выставит поверх неё вторую.
        self._pending_orders: dict[str, str] = self._load_pending()

    def _ensure_account(self) -> str:
        accounts = self.api.get_accounts()
        if self._configured_account_id:
            # в real счетов обычно несколько — без явного ACCOUNT_ID бот торговал бы
            # на первом попавшемся; заданный ID проверяем, что он вообще существует
            if not any(acc.get("id") == self._configured_account_id for acc in accounts):
                raise RuntimeError(
                    f"ACCOUNT_ID={self._configured_account_id} не найден среди счетов "
                    f"({len(accounts)} шт) — проверьте .env"
                )
            return self._configured_account_id
        if accounts:
            # в real счетов обычно несколько (брокерский, ИИС): без явного
            # ACCOUNT_ID выбор «первого попавшегося» может торговать не тем счётом
            if len(accounts) > 1 and not getattr(self.api, "is_sandbox", True):
                raise RuntimeError(
                    f"ACCOUNT_ID не задан, а счетов в режиме real несколько ({len(accounts)}) — "
                    "задайте ACCOUNT_ID в .env"
                )
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
        if not getattr(self.api, "is_sandbox", True):
            # SandboxPayIn на прод-хосте — гарантированный 404; без гварда бот
            # с пустым real-счётом падал бы на старте и крутился в crash-loop
            raise RuntimeError(
                f"Счёт {self.account_id} пуст ({total:.2f} руб), а пополнение через API "
                "доступно только в sandbox. В режиме real пополните счёт вручную "
                "и запустите бота снова."
            )
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
        blocked = self._reconcile_pending(instrument_id)
        if blocked is not None:
            return self._pending_result(blocked, lots)
        return self._execute(instrument_id, lots, BUY, price)

    def sell(self, instrument_id: str, lots: int, price: float | None = None) -> dict:
        blocked = self._reconcile_pending(instrument_id)
        if blocked is not None:
            return self._pending_result(blocked, lots)
        return self._execute(instrument_id, lots, SELL, price)

    def _pending_registry(self) -> dict[str, str]:
        """Ленивая инициализация: часть тестов создаёт Trader через __new__."""
        pending = getattr(self, "_pending_orders", None)
        if pending is None:
            pending = {}
            self._pending_orders = pending
        return pending

    def _pending_orders_path(self) -> Path | None:
        """Путь к реестру рядом с журналом сделок (reports/pending_orders.json)."""
        journal_path = getattr(self, "journal_path", None)
        if journal_path is None:
            # Trader, собранный в тестах через __new__, журнала не имеет — писать некуда
            return None
        return Path(journal_path).parent / PENDING_ORDERS_FILE

    def _load_pending(self) -> dict[str, str]:
        """Читает реестр с диска; повреждённый файл — fail closed для всех инструментов."""
        path = self._pending_orders_path()
        if path is None or not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning(
                "Файл незавершённых заявок %s не прочитан (%s) — новые заявки "
                "блокируем по всем инструментам до его удаления",
                path, exc,
            )
            return {PENDING_CORRUPT_KEY: PENDING_CORRUPT_VALUE}
        if not isinstance(payload, dict):
            log.warning(
                "Файл незавершённых заявок %s повреждён: ожидался объект, получено %s — "
                "новые заявки блокируем по всем инструментам",
                path, type(payload).__name__,
            )
            return {PENDING_CORRUPT_KEY: PENDING_CORRUPT_VALUE}
        return {str(key): str(value) for key, value in payload.items()}

    def _persist_pending(self) -> None:
        """Атомарно сохраняет реестр: временный файл рядом + os.replace.

        Ошибка записи не срывает торговый цикл (заявка уже выставлена) — только
        предупреждаем: после рестарта этот реестр может оказаться неполным.
        """
        path = self._pending_orders_path()
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
            tmp.write_text(
                json.dumps(self._pending_registry(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(tmp, path)
        except OSError as exc:
            log.warning(
                "Не удалось сохранить незавершённые заявки в %s (%s) — "
                "после рестарта они могут потеряться",
                path, exc,
            )

    def _reconcile_pending(self, instrument_id: str) -> str | None:
        """Проверяет ранее оставленную незавершённую заявку по инструменту.

        Возвращает order_id, если заявка всё ещё может исполниться (новую
        выставлять нельзя), и None, когда можно торговать дальше. Ошибка
        GetOrderState трактуется как «статус неизвестен» — fail closed.
        """
        pending = self._pending_registry()
        sentinel = pending.get(PENDING_CORRUPT_KEY)
        if sentinel:
            # Не знаем, какие заявки живы: блокируем все инструменты, пока файл
            # не удалят или не перепишут валидным содержимым.
            log.warning(
                "Файл незавершённых заявок повреждён (%s) — новые заявки "
                "блокируем по всем инструментам",
                sentinel,
            )
            return sentinel
        order_id = pending.get(instrument_id)
        if not order_id:
            return None
        try:
            state = self.api.get_order_state(self.account_id, order_id)
        except Exception as exc:
            log.warning(
                "Статус прошлой заявки %s по %s не получен (%s) — новые заявки блокируем",
                order_id, instrument_id, exc,
            )
            return order_id
        status = str(state.get("status") or "")
        if status in FINAL_STATUSES:
            pending.pop(instrument_id, None)
            self._persist_pending()
            log.info(
                "Прошлая заявка %s по %s завершилась (%s) — можно выставлять новую",
                order_id, instrument_id, status,
            )
            return None
        log.warning(
            "По %s всё ещё жива заявка %s (статус %s) — новую не выставляем",
            instrument_id, order_id, status or "неизвестен",
        )
        return order_id

    @staticmethod
    def _pending_result(order_id: str, lots: int) -> dict:
        return {
            "order_id": order_id,
            "status": "pending_unresolved",
            "lots_requested": int(lots),
            "lots_executed": 0,
            "avg_exec_price": 0.0,
            "commission": 0.0,
            "total_amount": 0.0,
            "message": f"previous order still open: {order_id}",
        }

    def _execute(self, instrument_id: str, lots: int, direction: str, price: float | None) -> dict:
        """Ставит заявку и возвращает нормализованный результат исполнения.

        Рыночные заявки песочницы обычно исполняются мгновенно; если статус ещё
        не финальный (new/partiallyfill), дозапрашиваем GetOrderState несколько раз,
        чтобы не считать исполнением то, что ещё висит на бирже. Не доработавшую
        заявку отзываем: оставленная на бирже лимитка может исполниться через часы
        по давно устаревшему сигналу, поверх которого цикл поставит новую.
        """
        lots = int(lots)
        if lots <= 0:
            # дробный остаток после корп. действия (0 лотов) либо пустое решение —
            # PostOrder с quantity=0 отклоняется биржей и шумел бы каждую итерацию
            log.warning("Заявка %s %s на %d лот(ов) не выставляется", instrument_id, direction, lots)
            return {
                "order_id": "", "status": "rejected", "lots_requested": 0, "lots_executed": 0,
                "avg_exec_price": 0.0, "commission": 0.0, "total_amount": 0.0,
                "message": "0 лотов — заявка не выставлялась",
            }
        state = self.api.post_order(self.account_id, instrument_id, lots, direction, price=price)
        attempts = 0
        while state["status"] not in FINAL_STATUSES and attempts < 3 and state["order_id"]:
            attempts += 1
            time.sleep(2.0)
            try:
                state = self.api.get_order_state(self.account_id, state["order_id"])
            except Exception as exc:
                log.warning("GetOrderState %s не удался (%s) — работаем с последним статусом", state["order_id"], exc)
                break
        if state["status"] not in FINAL_STATUSES and state["order_id"]:
            try:
                self.api.cancel_order(self.account_id, state["order_id"])
            except Exception as exc:
                log.warning(
                    "Отмена недоисполненной заявки %s не удалась (%s) — она может доработать позже",
                    state["order_id"], exc,
                )
            else:
                try:
                    state = self.api.get_order_state(self.account_id, state["order_id"])
                except Exception as exc:
                    log.warning("GetOrderState %s после отмены не удался (%s)", state["order_id"], exc)
                    state["status"] = "cancelled"  # отмена принята — дальше заявка не исполнится
        if state["status"] not in FINAL_STATUSES:
            if state["order_id"]:
                # отмена не удалась или биржа ещё не подтвердила финальный статус:
                # заявка может исполниться позже, поэтому блокируем новые по инструменту
                self._pending_registry()[instrument_id] = state["order_id"]
                self._persist_pending()
            log.warning(
                "Заявка %s осталась в статусе %s (исполнено %d/%d лотов)",
                state["order_id"], state["status"], state["lots_executed"], state["lots_requested"],
            )
        return state

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

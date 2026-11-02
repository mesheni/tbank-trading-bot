"""Главный цикл бота: sandbox-торговля по сигналам модели и новостям.

Цикл (каждые LOOP_INTERVAL_SEC):
  1) обновить свечи по инструментам (API -> БД -> признаки);
  2) прогноз доходности на H баров вперёд (артефакт лучшей модели);
  3) обновить новости, проставить тональность, собрать повестку;
  4) решение стратегии -> ордер в sandbox -> журнал.
Вне торговой сессии MOEX цикл спит.
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import logging
import os
import time
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

import pandas as pd

import control
from backtest import RiskConfig  # noqa: F401  (реэкспорт для удобства)
from config import MIN_TRADABLE_EQUITY_RUB, MSK, Config
from features import build_live_features, ensure_news_columns
from models.registry import ModelArtifact, load_artifact, model_factories
from nlp.agenda import AgendaScore, batch_score_news, score_agenda
from nlp.embedder import NewsEmbedder
from nlp.sentiment import make_sentiment
from notify import Notifier, throttle_key
from strategy import Decision, PortfolioState, Position, RiskConfig, decide, is_affordable
import ui
from tbank.api import TBankAPI
from tbank.market_data import (
    connect,
    download_candles,
    load_candles,
    load_news,
    load_sentiments,
    store_instruments,
    store_news,
    store_sentiments,
)
from tbank.rest import TBankRestClient
from tbank.trader import Trader, make_journal_row

log = logging.getLogger(__name__)

# Расписание MOEX по умолчанию (если TradingSchedules недоступен): основная сессия
DEFAULT_SESSION = (dt.time(9, 50), dt.time(18, 50))

# Столько подряд неудачных продаж позиции — критический сигнал: стоп не может
# исполниться (регрессия 09-18.09.2026: лотность позиции была завышена в 10 раз,
# биржа отклоняла заявки 9 дней, никто не замечал)
FAILED_SELL_ESCALATION = 3

# Длительность бара в секундах; ключи совпадают с config.CANDLE_INTERVALS
BAR_SECONDS = {"1min": 60, "5min": 300, "15min": 900, "hour": 3600, "day": 86400}

# Запас сверх двух баров: вечерние/выходные перерывы и задержка публикации API
STALE_BAR_GRACE_SEC = 900

# Интервалы, которых нет в BAR_SECONDS: предупреждаем один раз за процесс
_STALE_UNKNOWN_INTERVALS: set[str] = set()


class StaleDataError(RuntimeError):
    """Последняя свеча старше допустимого — по тикеру нельзя принимать решения."""


def kill_switch_path(reports_dir) -> Path:
    """Путь к файлу состояния kill-switch (переживает рестарт до явного сброса)."""
    return Path(reports_dir) / "kill_switch.json"


def read_kill_switch(reports_dir) -> dict | None:
    """Состояние kill-switch с диска; None — файла нет, {"corrupt": True} — fail closed."""
    path = kill_switch_path(reports_dir)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"corrupt": True}
    return payload if isinstance(payload, dict) else {"corrupt": True}


def clear_kill_switch(reports_dir) -> bool:
    """Снимает kill-switch (cli.py kill-switch-reset); True — файл существовал."""
    path = kill_switch_path(reports_dir)
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True


def _write_kill_switch(reports_dir, payload: dict) -> None:
    """Атомарная запись состояния: временный файл рядом + os.replace."""
    path = kill_switch_path(reports_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def model_passes_live_gate(metrics: Mapping, min_dir_acc: float) -> bool:
    """Гейт качества модели: без подтверждённого преимущества — новых входов нет.

    Модель допускается, если у неё либо измеренный dir_acc >= min_dir_acc, либо
    положительный edge после издержек: live_sharpe_net (пороговая стратегия, как
    выбирает train) или strategy_sharpe_net (полный флип). Метрики NaN/отсутствие
    доказательств — не допуск.
    """
    dir_acc = metrics.get("directional_acc")
    if dir_acc is not None and not pd.isna(dir_acc) and float(dir_acc) >= min_dir_acc:
        return True
    for key in ("live_sharpe_net", "strategy_sharpe_net"):
        value = metrics.get(key)
        if value is not None and not pd.isna(value) and float(value) > 0.0:
            return True
    return False


class SessionCalendar:
    """Торговый календарь MOEX: интервалы из TradingSchedules + фиксированный фолбэк.

    Используется и живым циклом, и watchdog-командой; расписание обновляется
    раз в 6 часов, при недоступности API действует сессия пн-пт 09:50-18:50 МСК.
    """

    REFRESH_HOURS = 6

    def __init__(self, api: TBankAPI | None = None):
        self.api = api
        self._intervals: list[tuple[dt.datetime, dt.datetime]] = []
        self._checked_at: dt.datetime | None = None

    def active(self, now: dt.datetime | None = None) -> bool:
        now = now or dt.datetime.now(MSK)
        if (
            self.api is not None
            and (self._checked_at is None or now - self._checked_at > dt.timedelta(hours=self.REFRESH_HOURS))
        ):
            try:
                self._intervals = self.api.trading_schedules()
            except Exception as exc:
                log.warning("Расписание торгов недоступно (%s), используется фиксированное", exc)
                self._intervals = []
            self._checked_at = now

        if not self._intervals:
            start = dt.datetime.combine(now.date(), DEFAULT_SESSION[0], tzinfo=MSK)
            end = dt.datetime.combine(now.date(), DEFAULT_SESSION[1], tzinfo=MSK)
            return start <= now <= end and now.weekday() < 5
        return any(start <= now <= end for start, end in self._intervals)


def log_equity(path: Path, total: float, cash: float, positions_value: float) -> None:
    """Дописывает точку кривой капитала живого прогона (reports/equity_live.csv)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    is_new = not path.exists()
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if is_new:
            writer.writerow(["time", "total_rub", "cash_rub", "positions_rub"])
        writer.writerow(
            [
                dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                round(total, 2),
                round(cash, 2),
                round(positions_value, 2),
            ]
        )


def write_heartbeat(path: Path) -> None:
    """Отметка «живой итерации» — её свежесть проверяет cli.py watchdog."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), encoding="utf-8")


def ensure_sentiments(conn, news_df: pd.DataFrame, scorer, model_key: str) -> dict[str, float]:
    """Скоры тональности с кэшем в БД: каждая новость считается один раз.

    Живой цикл и бэктест используют один кэш — после первого прогона
    тональность не пересчитывается ни при рестартах, ни в офлайн-оценке.
    """
    cached = load_sentiments(conn, model_key)
    missing = [str(n) for n in news_df["news_id"] if str(n) not in cached]
    if missing:
        to_score = news_df[news_df["news_id"].astype(str).isin(missing)]
        fresh = batch_score_news(to_score, scorer)
        if fresh:
            store_sentiments(conn, model_key, fresh)
            cached.update(fresh)
    return cached


# ТОЛЬКО для тестов/e2e (env TBANK_FAKE_API=1): детерминированный TBankAPI без сети.
# Достаточно для прокрутки живого цикла: счета/портфель/ордера/расписание/новости.
class FakeTBankAPI:
    """Минимальная офлайн-заглушка TBankAPI — не использовать в торговле."""

    is_sandbox = True
    client = None
    order_market_fallback = False
    on_market_fallback = None

    def __init__(self, tickers, cash_rub: float = 1_000_000.0):
        self.tickers = list(tickers)
        self.cash_rub = float(cash_rub)
        self.orders: dict[str, dict] = {}

    def get_accounts(self) -> list[dict]:
        return [{"id": "fake-account"}]

    def resolve_instruments(self, tickers) -> dict[str, dict]:
        return {
            str(t).upper(): {
                "ticker": str(t).upper(),
                "figi": f"FAKE-{str(t).upper()}",
                "uid": f"fake-{str(t).upper()}",
                "name": str(t).upper(),
                "lot": 1,
                "class_code": "TQBR",
                "min_price_increment": 0.01,
            }
            for t in tickers
        }

    def trading_schedules(self, figi: str = "", days: int = 7) -> list[tuple]:
        return []

    def get_news(self, instrument_ids=None, max_pages: int = 5) -> list[dict]:
        return []

    def get_candles(self, instrument_id, interval_enum, from_dt, to_dt) -> list[dict]:
        return []

    def get_last_price(self, instrument_id) -> float:
        return 0.0

    def get_portfolio(self, account_id) -> dict:
        return {"positions": {}, "total_amount_rub": self.cash_rub, "cash_rub": self.cash_rub}

    def open_sandbox_account(self) -> str:
        return "fake-account"

    def pay_in(self, account_id, amount_rub: float) -> None:
        self.cash_rub = float(amount_rub)

    def close_sandbox_account(self, account_id) -> None:
        return None

    def post_order(self, account_id, instrument_id, quantity_lots, direction, price=None, order_type="ORDER_TYPE_LIMIT") -> dict:
        order_id = f"fake-{len(self.orders) + 1}"
        state = {
            "order_id": order_id,
            "status": "fill",
            "lots_requested": int(quantity_lots),
            "lots_executed": int(quantity_lots),
            "avg_exec_price": float(price or 0.0),
            "commission": 0.0,
            "total_amount": float(price or 0.0) * int(quantity_lots),
            "message": "",
        }
        self.orders[order_id] = state
        return dict(state)

    def get_order_state(self, account_id, order_id) -> dict:
        return dict(self.orders.get(order_id, {"order_id": order_id, "status": "fill", "lots_requested": 0, "lots_executed": 0, "avg_exec_price": 0.0, "commission": 0.0, "total_amount": 0.0, "message": ""}))

    def cancel_order(self, account_id, order_id) -> dict:
        state = dict(self.orders.get(order_id, {}))
        state.update({"order_id": order_id, "status": "cancelled"})
        return state


class TradingBot:
    def __init__(self, config: Config, risk: RiskConfig | None = None, api: TBankAPI | None = None):
        config.validate()
        self.config = config
        self.risk = risk or RiskConfig(
            max_position_pct=config.max_position_pct,
            stop_loss_pct=config.stop_loss_pct,
            take_profit_pct=config.take_profit_pct,
            min_abs_return=config.min_abs_return,
            news_sentiment_gate=config.news_sentiment_gate,
            commission_pct=config.commission_pct,
            slippage_pct=config.slippage_pct,
            commission_min_rub=config.commission_min_rub,
            reversal_exit_mult=config.reversal_exit_mult,
            max_total_exposure_pct=config.max_total_exposure_pct,
        )
        self.client = api.client if api is not None else TBankRestClient(config.token, config.mode)
        self.api = api if api is not None else TBankAPI(
            self.client, order_market_fallback=config.order_fallback_to_market
        )
        self.trader = Trader(
            self.api, Path(config.reports_dir) / "journal.csv", account_id=getattr(config, "account_id", "")
        )
        self.conn = connect(config.db_path)
        self.calendar = SessionCalendar(self.api)
        self.notifier = Notifier.from_config(config)
        # алерт при автопереводе лимитной заявки в рыночную (см. api.post_order)
        self.api.on_market_fallback = lambda note: self.notifier.send_throttled(
            "market-fallback", "лимитная заявка ушла рыночной", note
        )

        self.instruments = self.api.resolve_instruments(config.tickers)
        if not self.instruments:
            raise RuntimeError("Ни один тикер не разрешён в инструмент")
        store_instruments(self.conn, self.instruments)  # кэш лотов/uid для офлайн-команд

        self.embedder = NewsEmbedder(config.embedding_model, enabled=config.nlp_embedder)
        self.sentiment = make_sentiment(config.sentiment_model, preference=config.nlp_sentiment)

        self.artifacts: dict[str, ModelArtifact] = {}
        self._news_updated_at: dt.datetime | None = None
        self._running = False
        # kill-switch по просадке: False = новых входов нет, выходы работают
        self._entries_allowed = True
        # сработавший kill-switch переживает рестарт до явного `cli.py kill-switch-reset`
        self._load_kill_switch_state()
        # подряд неудачные продажи по figi: эскалация шума после FAILED_SELL_ESCALATION
        self._sell_failures: dict[str, int] = {}
        # бюджет счёта (якорь kill-switch и P&L); уточняется при старте цикла
        self._budget_rub: float | None = getattr(config, "account_budget_rub", None)

    # ---------- Сессия ----------

    def in_trading_session(self, now: dt.datetime | None = None) -> bool:
        return self.calendar.active(now)

    # ---------- Данные и сигналы ----------

    def _check_stale_candles(self, ticker: str, df: pd.DataFrame) -> None:
        """Свеча старше 2 баров + запас — торговать по ней нельзя (аудит F-C1.5)."""
        interval = self.config.candle_interval
        bar_seconds = BAR_SECONDS.get(interval)
        if bar_seconds is None:
            if interval not in _STALE_UNKNOWN_INTERVALS:
                _STALE_UNKNOWN_INTERVALS.add(interval)
                log.warning(
                    "Интервал %r не учтён в BAR_SECONDS — проверка устаревания свечей отключена",
                    interval,
                )
            return
        last = pd.Timestamp(df.index[-1])
        if last.tzinfo is None:
            last = last.tz_localize("UTC")
        else:
            last = last.tz_convert("UTC")
        age_sec = (dt.datetime.now(dt.timezone.utc) - last.to_pydatetime()).total_seconds()
        if age_sec > 2 * bar_seconds + STALE_BAR_GRACE_SEC:
            raise StaleDataError(f"{ticker}: последняя свеча {last.isoformat()} старше допустимого")

    def refresh_candles(self, figi: str, ticker: str) -> pd.DataFrame:
        try:
            download_candles(
                self.api,
                self.conn,
                figi,
                self.config.candle_interval,
                days_back=3,
                now=dt.datetime.now(dt.timezone.utc),
                ticker=ticker,
            )
        except Exception as exc:
            log.warning("%s: не удалось обновить свечи (%s), работаем по кэшу", ticker, exc)
        df = load_candles(self.conn, ticker, self.config.candle_interval)
        if df.empty:
            raise RuntimeError(f"{ticker}: в БД нет свечей — выполните команду download")
        self._check_stale_candles(ticker, df)
        return df

    def update_news(self) -> None:
        """Подкачивает свежие новости в БД (один раз за цикл — лента общая)."""
        now = dt.datetime.now(dt.timezone.utc)
        if self._news_updated_at is not None and now - self._news_updated_at < dt.timedelta(minutes=5):
            return
        try:
            items = self.api.get_news(max_pages=3)
            if items:
                store_news(self.conn, items)
                log.info("Новости: в БД добавлено/обновлено %d записей", len(items))
            self._news_updated_at = now
        except Exception as exc:
            log.warning("Новости недоступны (%s)", exc)

    def ticker_agenda(self, instrument: dict, ticker: str) -> AgendaScore:
        """Собирает повестку тикера из уже сохранённых новостей."""
        uid = instrument.get("uid") or instrument.get("figi")
        now = pd.Timestamp.now(tz="UTC")
        news_df = load_news(
            self.conn,
            uid,
            since=(now - pd.Timedelta(hours=72)).to_pydatetime(),
        )
        if news_df.empty:
            return AgendaScore()
        sentiments = ensure_sentiments(self.conn, news_df, self.sentiment, self._sentiment_model_key())
        return score_agenda(
            news_df,
            sentiments,
            now,
            window_hours=48.0,
            half_life_hours=self.config.news_half_life_hours,
            embedder=self.embedder if self.embedder.available else None,
        )

    def _sentiment_model_key(self) -> str:
        """Ключ кэша скоров: разные модели тональности не смешиваются."""
        name = getattr(self.sentiment, "name", "")
        if name == "lexicon":
            return "lexicon"
        return f"transformers:{self.config.sentiment_model}"

    def _artifact(self, ticker: str) -> ModelArtifact:
        artifact = self.artifacts.get(ticker)
        if artifact is None:
            artifact = load_artifact(
                self.config.models_dir, ticker, self.config.candle_interval, self.config.forecast_horizon
            )
            self.artifacts[ticker] = artifact
        return artifact

    def _model_allowed(self, ticker: str, artifact: ModelArtifact) -> bool:
        """Гейт качества модели (см. model_passes_live_gate); блокирует только входы."""
        return model_passes_live_gate(artifact.metrics, self.config.min_model_dir_acc)

    def predict(self, ticker: str, candles: pd.DataFrame, agenda: AgendaScore) -> tuple[float, float]:
        """Возвращает (прогноз доходности на горизонт, новостной скор)."""
        artifact = self._artifact(ticker)

        features = ensure_news_columns(build_live_features(candles, self.config.forecast_horizon))

        if artifact.kind == "lgbm" and artifact.model is not None:
            last = features.iloc[-1]
            r_hat = artifact.model.predict_row(last)
        else:
            factory = model_factories(self.config.forecast_horizon).get(artifact.kind)
            if factory is None:
                raise ValueError(f"Неизвестный вид модели: {artifact.kind}")
            model = factory()
            model.fit(candles["close"])
            r_hat = model.predict()
        return float(r_hat), float(agenda.sentiment)

    # ---------- Исполнение ----------

    def _log_execution_deviation(self, ticker: str, price: float, exec_price: float) -> None:
        """Проскальзывание факта против цены решения в базисных пунктах (аудит F-C2.2)."""
        if price <= 0:
            return
        bps = (exec_price / price - 1.0) * 1e4
        log.info(
            "%s: отклонение исполнения %+.1f bps (решение %.2f → факт %.2f)",
            ticker, bps, price, exec_price,
        )

    def step_ticker(
        self, ticker: str, instrument: dict, portfolio: PortfolioState, entries_allowed: bool = True
    ) -> None:
        figi = instrument["figi"]
        # оффлайн-тесты собирают бот через __new__ без __init__ — счётчик ленивый
        if "_sell_failures" not in self.__dict__:
            self._sell_failures = {}
        if figi not in portfolio.positions:
            self._sell_failures.pop(figi, None)
        artifact = self._artifact(ticker)
        if not entries_allowed and figi not in portfolio.positions:
            # kill-switch: просадка глубже MAX_DRAWDOWN_PCT — капитал сохраняем,
            # открытые позиции обслуживаются (стопы/тейки/разворот), новых входов нет
            log.info("%-5s kill-switch: новых входов нет (просадка сверх порога)", ticker)
            return
        if figi not in portfolio.positions and not self._model_allowed(ticker, artifact):
            dir_acc = artifact.metrics.get("directional_acc")
            live = artifact.metrics.get("live_sharpe_net")
            net = artifact.metrics.get("strategy_sharpe_net")
            fmt = lambda v: "n/a" if v is None or pd.isna(v) else f"{float(v):+.3f}"
            log.info(
                "%-5s пропуск: модель %s без подтверждённого преимущества "
                "(dir_acc=%s, live=%s, net=%s; порог dir_acc %.2f или sharpe > 0) — новых входов нет",
                ticker,
                artifact.kind,
                fmt(dir_acc),
                fmt(live),
                fmt(net),
                self.config.min_model_dir_acc,
            )
            return
        lot = int(instrument.get("lot", 1))
        candles = self.refresh_candles(figi, ticker)
        agenda = self.ticker_agenda(instrument, ticker)
        r_hat, news_score = self.predict(ticker, candles, agenda)
        price = float(candles["close"].iloc[-1])

        # порог входа: не ниже конфига и не ниже рекомендованного моделью (издержки/масштаб)
        risk = self.risk
        if artifact is not None and artifact.threshold > risk.min_abs_return:
            risk = replace(self.risk, min_abs_return=artifact.threshold)

        decision: Decision = decide(
            ticker, figi, price, r_hat, news_score, portfolio, lot, risk
        )
        action_color = {
            "BUY": ui.BRIGHT_GREEN,
            "SELL": ui.YELLOW,
            "HOLD": ui.DIM,
        }.get(decision.action, "")
        action = ui.paint_log(f"{decision.action:<4}", action_color) if action_color else decision.action
        log.info(
            "%-5s цена %9.2f | прогноз %s | новости %s (%d нов.) | %s · %s",
            ticker,
            price,
            ui.fmt_signed_log(r_hat, "{:+.5f}"),
            ui.fmt_signed_log(news_score, "{:+.2f}"),
            agenda.n_items,
            action,
            decision.reason,
        )

        if decision.action == "BUY":
            state = self.trader.buy(figi, decision.lots, price)
            executed = max(0, int(state.get("lots_executed") or 0))
            exec_price = float(state.get("avg_exec_price") or 0.0) or price
            if executed <= 0:
                log.warning(
                    "%s: заявка не исполнена (статус %s) — позиция не открывается",
                    ticker, state.get("status") or "неизвестен",
                )
                self.notifier.send_throttled(
                    throttle_key("BUY", "order-not-filled", ticker), f"заявка BUY {ticker} не исполнена",
                    f"Статус: {state.get('status') or 'неизвестен'}\n"
                    f"Запрошено {decision.lots} лот(ов) по {price:.2f}\nПричина решения: {decision.reason}",
                )
            else:
                self._log_execution_deviation(ticker, price, exec_price)
                if executed < decision.lots:
                    log.warning("%s: частичное исполнение покупки: %d из %d лотов", ticker, executed, decision.lots)
                    self.notifier.send_throttled(
                        throttle_key("BUY", "partial-fill", ticker), f"частичная покупка {ticker}",
                        f"Исполнено {executed} из {decision.lots} лотов по {exec_price:.2f}\n"
                        f"Причина решения: {decision.reason}",
                    )
                portfolio.positions[figi] = Position(figi, ticker, executed, lot, exec_price)
                # снимок портфеля делается раз на итерацию — следующие тикеры этого
                # цикла должны считать бюджет от денег после сделки, а не до неё
                fee = float(state.get("commission") or 0.0)
                portfolio.cash = max(0.0, portfolio.cash - exec_price * executed * lot - fee)
                portfolio.equity = max(0.0, portfolio.equity - fee)  # деньги стали позицией; ушла комиссия
                self.trader.log_trade(
                    make_journal_row(ticker, "BUY", executed, exec_price, decision.reason, str(state.get("order_id", "")))
                )
                self.notifier.send(
                    f"BUY {ticker}: {executed} лот(ов) × {exec_price:.2f}",
                    f"Прогноз: {r_hat:+.5f} · новости: {news_score:+.2f}\nПричина: {decision.reason}\n"
                    f"Заявка: {state.get('order_id', '')} (статус {state.get('status')})",
                )
        elif decision.action == "SELL" and figi in portfolio.positions:
            position = portfolio.positions[figi]
            state = self.trader.sell(figi, position.lots, price)
            executed = max(0, min(int(state.get("lots_executed") or 0), position.lots))
            exec_price = float(state.get("avg_exec_price") or 0.0) or price
            if executed <= 0:
                fails = self._sell_failures.get(figi, 0) + 1
                self._sell_failures[figi] = fails
                log.warning(
                    "%s: продажа не исполнена (статус %s) — позиция остаётся",
                    ticker, state.get("status") or "неизвестен",
                )
                if fails >= FAILED_SELL_ESCALATION:
                    pnl_pct = price / position.avg_price - 1.0 if position.avg_price > 0 else 0.0
                    log.error(
                        "%s: %d подряд неудачных продаж (статус %s): %d лот(ов), pnl %+.2f%% — "
                        "стоп/выход физически не исполняется, требуется ручная проверка счёта",
                        ticker, fails, state.get("status") or "неизвестен", position.lots, pnl_pct * 100,
                    )
                    self.notifier.send_throttled(
                        f"sell-stuck:{ticker}", f"позиция {ticker} не продаётся",
                        f"{fails} подряд неудачных попыток продажи "
                        f"(статус {state.get('status') or 'неизвестен'}).\n"
                        f"Держим {position.lots} лот(ов) × {position.avg_price:.2f}, "
                        f"pnl {pnl_pct:+.2%}\nПричина решения: {decision.reason}\n"
                        f"Последняя ошибка: {state.get('message') or 'нет деталей'}\n"
                        "Стоп-лосс не может исполниться — проверьте счёт вручную.",
                    )
                else:
                    self.notifier.send_throttled(
                        throttle_key("SELL", "order-not-filled", ticker), f"заявка SELL {ticker} не исполнена",
                        f"Статус: {state.get('status') or 'неизвестен'}\n"
                        f"Пробовали продать {position.lots} лот(ов) по {price:.2f}\nПричина решения: {decision.reason}",
                    )
            else:
                self._log_execution_deviation(ticker, price, exec_price)
                self._sell_failures.pop(figi, None)
                if executed < position.lots:
                    log.warning(
                        "%s: частичная продажа: %d из %d лотов, остаток держим",
                        ticker, executed, position.lots,
                    )
                    self.notifier.send_throttled(
                        throttle_key("SELL", "partial-fill", ticker), f"частичная продажа {ticker}",
                        f"Исполнено {executed} из {position.lots} лотов по {exec_price:.2f}\n"
                        f"Причина решения: {decision.reason}",
                    )
                    portfolio.positions[figi] = Position(
                        figi, ticker, position.lots - executed, lot, position.avg_price
                    )
                else:
                    del portfolio.positions[figi]
                # выручка вернулась в кэш (комиссия ушла) — уточняем снимок портфеля
                fee = float(state.get("commission") or 0.0)
                portfolio.cash += exec_price * executed * lot - fee
                portfolio.equity = max(0.0, portfolio.equity - fee)  # оценочно: без переоценки остатка
                self.trader.log_trade(
                    make_journal_row(ticker, "SELL", executed, exec_price, decision.reason, str(state.get("order_id", "")))
                )
                self.notifier.send(
                    f"SELL {ticker}: {executed} лот(ов) × {exec_price:.2f}",
                    f"Причина: {decision.reason}\nЗаявка: {state.get('order_id', '')} "
                    f"(статус {state.get('status')})",
                )

    def run_forever(self, max_iterations: int | None = None) -> None:
        self._running = True
        iteration = 0
        # бюджет вносится один раз на старте и только на пустой счёт:
        # в цикле никаких пополнений — бот торгует строго данным бюджетом
        self.trader.initialize_balance(self.config.budget_rub)
        # профиль капитала и вселенная фиксируются на запуск (см. _setup_capital_profile)
        self._setup_capital_profile()
        log.info(
            "Бот запущен: mode=%s tickers=%s interval=%s horizon=%d",
            self.config.mode, ",".join(self.instruments), self.config.candle_interval, self.config.forecast_horizon,
        )
        try:
            while self._running and (max_iterations is None or iteration < max_iterations):
                iteration += 1
                self._daemon_event(iteration)
                if not self.in_trading_session():
                    log.info("Вне торговой сессии MOEX — пауза %d c", self.config.loop_interval_sec)
                    if not self._tick_wait(self.config.loop_interval_sec):
                        break
                    continue

                try:
                    self.update_news()
                    portfolio = self._load_portfolio()
                    self._check_kill_switch(portfolio.equity)
                    for ticker, instrument in self.instruments.items():
                        try:
                            self.step_ticker(ticker, instrument, portfolio, self._entries_allowed)
                        except StaleDataError as exc:
                            log.warning("%s: свечи устарели — тикер пропущен: %s", ticker, exc)
                            self.notifier.send_throttled(
                                f"stale-data:{ticker}", f"свечи {ticker} устарели", str(exc)
                            )
                            continue
                        except Exception as exc:
                            log.exception("%s: шаг не выполнен: %s", ticker, exc)
                            self.notifier.send_throttled(
                                f"step-error:{ticker}", f"ошибка шага {ticker}",
                                f"{type(exc).__name__}: {exc}\n(не чаще 1 письма в 30 минут)",
                            )

                    snapshot = self.trader.portfolio()
                    positions_value = snapshot["total_amount_rub"] - snapshot["cash_rub"]
                    log_equity(
                        Path(self.config.reports_dir) / "equity_live.csv",
                        snapshot["total_amount_rub"],
                        snapshot["cash_rub"],
                        positions_value,
                    )
                    write_heartbeat(Path(self.config.reports_dir) / "heartbeat")
                    pnl_pct = snapshot["total_amount_rub"] / self._budget_rub - 1.0
                    self._daemon_event(iteration, snapshot=snapshot)
                    log.info(
                        "%s Итерация %d · капитал %s руб · P&L %s · позиций: %d",
                        ui.paint_log("==", ui.CYAN),
                        iteration,
                        ui.fmt_money(snapshot["total_amount_rub"]),
                        ui.fmt_signed_log(pnl_pct),
                        len(snapshot["positions"]),
                    )
                except Exception as exc:
                    log.exception("Ошибка итерации: %s", exc)
                    self._daemon_event(iteration, error=f"{type(exc).__name__}: {exc}")
                    self.notifier.send_throttled(
                        "iteration-error", "ошибка итерации бота",
                        f"{type(exc).__name__}: {exc}\n(не чаще 1 письма в 30 минут; "
                        "живость бота проверяет cli.py watchdog)",
                    )

                if max_iterations is None or iteration < max_iterations:
                    if not self._tick_wait(self.config.loop_interval_sec):
                        break
        finally:
            self._running = False
            self._daemon_finish()
            self.conn.close()
            log.info("Бот остановлен")

    def _load_kill_switch_state(self) -> None:
        """Восстанавливает сработавший kill-switch с диска (в т.ч. повреждённый — fail closed)."""
        reports_dir = getattr(self.config, "reports_dir", None)
        if not reports_dir:
            return
        state = read_kill_switch(reports_dir)
        if state is None:
            return
        self._entries_allowed = False
        triggered_at = None if state.get("corrupt") else state.get("triggered_at")
        log.error(
            "kill-switch активен с %s: новых входов нет, пока не выполнена `cli.py kill-switch-reset`",
            triggered_at or "неизвестного времени",
        )

    def _check_kill_switch(self, equity: float) -> None:
        """Просадка глубже MAX_DRAWDOWN_PCT — запретить входы до явного сброса (переживает рестарт)."""
        anchor = self._budget_rub if self._budget_rub is not None else self.config.sandbox_initial_rub
        if self._entries_allowed and equity < anchor * (1.0 - self.config.max_drawdown_pct):
            self._entries_allowed = False
            dd = 1.0 - equity / anchor
            triggered_at = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
            reports_dir = getattr(self.config, "reports_dir", None)
            if reports_dir:
                try:
                    _write_kill_switch(
                        reports_dir,
                        {
                            "triggered_at": triggered_at,
                            "equity": equity,
                            "budget": anchor,
                            "threshold": self.config.max_drawdown_pct,
                        },
                    )
                except OSError as exc:
                    log.error("kill-switch: не удалось сохранить состояние (%s)", exc)
            log.error(
                "KILL-SWITCH: капитал %.0f руб — просадка %.1f%% превышает MAX_DRAWDOWN_PCT (%.0f%%). "
                "Новые входы запрещены; стопы/тейки продолжают работать. Вернуть торговлю: "
                "`cli.py kill-switch-reset`.",
                equity, dd * 100, self.config.max_drawdown_pct * 100,
            )
            self.notifier.send(
                "kill-switch: просадка сверх порога",
                f"Капитал {equity:,.0f} руб — просадка {dd:.1%} при пороге "
                f"{self.config.max_drawdown_pct:.0%} (бюджет {anchor:,.0f} руб).\n"
                f"Новые входы запрещены до `cli.py kill-switch-reset`; "
                f"открытые позиции обслуживаются.",
            )

    def reset_kill_switch(self) -> bool:
        """Снять kill-switch: удалить файл состояния и снова разрешить входы.

        _check_kill_switch только выставляет флаг в False и не умеет его снимать —
        сброс обязан сбрасывать и файл, и in-memory флаг (команда демона).
        """
        reports_dir = getattr(self.config, "reports_dir", None)
        cleared = clear_kill_switch(reports_dir) if reports_dir else False
        self._entries_allowed = True
        log.warning(
            "kill-switch сброшен (%s) — новые входы разрешены",
            "файл удалён" if cleared else "файл не был активен",
        )
        return cleared

    # ---------- Демон: tick-цикл и статус ----------

    def _tick_wait(self, seconds: float, on_tick=None) -> bool:
        """Ждёт ``seconds``, просыпаясь каждые DAEMON_TICK_SEC; False — запрошен стоп.

        Сон разбит на срезы, чтобы фоновый демон каждую секунду обрабатывал
        очередь команд и писал статус, а не спал весь LOOP_INTERVAL_SEC.
        """
        tick = float(getattr(self.config, "daemon_tick_sec", 1.0) or 1.0)
        if tick <= 0:
            tick = 1.0
        if on_tick is None:
            runner = getattr(self, "_daemon_runner", None)
            on_tick = runner.on_tick if runner is not None else None
        remaining = max(0.0, float(seconds))
        while remaining > 0:
            slice_sec = min(tick, remaining)
            time.sleep(slice_sec)
            remaining -= slice_sec
            if on_tick is not None and on_tick():
                return False
        return True

    def _daemon_event(self, iteration: int, snapshot: dict | None = None, error: str | None = None) -> None:
        """Уведомить демон-раннер об итерации (no-op для обычного run_forever)."""
        runner = getattr(self, "_daemon_runner", None)
        if runner is not None:
            runner.publish(iteration=iteration, snapshot=snapshot, error=error)

    def _daemon_finish(self) -> None:
        runner = getattr(self, "_daemon_runner", None)
        if runner is not None:
            runner.finish()

    def _setup_capital_profile(self) -> None:
        """Режим работы под размер счёта: профиль капитала + доступная вселенная.

        Вызывается один раз на запуск (смена режима — правка .env и рестарт).
        Профиль подбирает долю позиции и лимит экспозиции под капитал; из вселенной
        исключаются бумаги, чей 1 лот не влезает в бюджет позиции. Kill-switch и
        P&L дальше считаются от бюджета счёта, а не от значения по умолчанию.
        """
        equity = self._load_portfolio().equity
        budget = self.config.account_budget_rub
        if budget is None:
            budget = self.config.sandbox_initial_rub if self.config.is_sandbox else equity
        self._budget_rub = float(budget)

        profile = self.config.apply_profile(equity)
        self.risk = RiskConfig(
            max_position_pct=self.config.max_position_pct,
            stop_loss_pct=self.config.stop_loss_pct,
            take_profit_pct=self.config.take_profit_pct,
            min_abs_return=self.config.min_abs_return,
            news_sentiment_gate=self.config.news_sentiment_gate,
            commission_pct=self.config.commission_pct,
            slippage_pct=self.config.slippage_pct,
            commission_min_rub=self.config.commission_min_rub,
            reversal_exit_mult=self.config.reversal_exit_mult,
            max_total_exposure_pct=self.config.max_total_exposure_pct,
        )
        log.info(
            "Профиль капитала %s: позиция до %.0f%% капитала, экспозиция до %.0f%%, бюджет %s руб",
            profile,
            self.config.max_position_pct * 100,
            (self.config.max_total_exposure_pct or 0) * 100,
            ui.fmt_money(self._budget_rub),
        )
        if equity < MIN_TRADABLE_EQUITY_RUB:
            log.warning(
                "Капитал %.0f руб ниже разумного минимума (~%s руб): целые лоты MOEX "
                "не помещаются в бюджет позиции — сделок почти не будет",
                equity,
                ui.fmt_money(MIN_TRADABLE_EQUITY_RUB),
            )

        # вселенная: инструменты, чей 1 лот влезает в бюджет позиции
        # (цена — последняя закрытая свеча из кэша БД)
        dropped: list[str] = []
        kept: dict[str, dict] = {}
        for ticker, instrument in self.instruments.items():
            df = load_candles(self.conn, ticker, self.config.candle_interval)
            price = float(df["close"].iloc[-1]) if not df.empty else 0.0
            lot = int(instrument.get("lot", 1))
            if price > 0 and not is_affordable(price, lot, equity, self.config.max_position_pct):
                dropped.append(
                    f"{ticker} (лот {ui.fmt_money(price * lot)} руб > "
                    f"бюджет {ui.fmt_money(equity * self.config.max_position_pct)} руб)"
                )
            else:
                kept[ticker] = instrument
        total = len(self.instruments)
        if dropped and kept:
            self.instruments = kept
            log.warning(
                "Профиль %s: исключены инструменты, чей лот дороже бюджета позиции: %s",
                profile,
                "; ".join(dropped),
            )
        elif dropped and not kept:
            log.error(
                "Профиль %s: все %d инструментов дороже бюджета позиции (%s руб) — "
                "ни одного лота не купить. Увеличьте капитал или задайте CAPITAL_PROFILE явно.",
                profile, total, ui.fmt_money(equity * self.config.max_position_pct),
            )
        self.notifier.send_throttled(
            "capital-profile",
            f"режим {profile}: капитал {equity:,.0f} руб",
            f"Профиль капитала: {profile}\n"
            f"Позиция до {self.config.max_position_pct:.0%} капитала, "
            f"экспозиция до {(self.config.max_total_exposure_pct or 0):.0%}.\n"
            f"Торгуем {len(self.instruments)} из {total} тикеров"
            + (
                f"\nИсключены (лот дороже бюджета): {', '.join(d.split(' ')[0] for d in dropped)}"
                if dropped
                else ""
            )
            + f"\nБюджет (якорь kill-switch): {self._budget_rub:,.0f} руб",
        )

    def _load_portfolio(self) -> PortfolioState:
        raw = self.trader.portfolio()
        positions = {}
        for figi, pos in raw["positions"].items():
            instrument = next((i for i in self.instruments.values() if i["figi"] == figi), None)
            ticker = (instrument.get("ticker") or figi) if instrument else figi
            lot = int(instrument.get("lot", 1)) if instrument else 1
            if pos["quantity"] > 0:
                # API отдаёт количество в ШТУКАХ, а стратегиям/ордерам нужны ЛОТЫ
                lots = int(pos["quantity"] // lot)
                if lots == 0:
                    # некратное лоту число штук (корп. действие) неисполнимо:
                    # заявка на 0 лотов отклоняется биржей и шумела бы вечно
                    log.warning(
                        "%s: позиция %.2f шт меньше лота (%d) — не обслуживается ботом",
                        ticker, pos["quantity"], lot,
                    )
                    continue
                positions[figi] = Position(
                    figi, ticker, lots, lot,
                    pos["average_position_price"] or pos["current_price"],
                )
        return PortfolioState(cash=raw["cash_rub"], equity=raw["total_amount_rub"], positions=positions)

# =====================================================================
# D1: headless-демон (status snapshot + command queue + PID-lock)
# =====================================================================

class _DaemonRunner:
    """Публикует статус демона и обрабатывает команды на каждом тике."""

    def __init__(self, bot, reports_dir):
        self.bot = bot
        self.reports_dir = Path(reports_dir)
        self.started_at = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        self.state = "running"
        self.iteration = 0
        self.stop_requested = False
        self.last_error: str | None = None
        self.equity: float | None = None
        self.cash: float | None = None
        self.positions: int = 0
        self.position_rows: list[dict] = []

    def _kill_switch(self) -> tuple[bool, str | None]:
        if getattr(self.bot, "_entries_allowed", True):
            return False, None
        state = read_kill_switch(self.reports_dir)
        if isinstance(state, dict):
            return True, "corrupt" if state.get("corrupt") else state.get("triggered_at")
        return True, None

    def _positions_payload(self, portfolio_positions: dict) -> list[dict]:
        """Позиции портфеля для TUI (ticker/lots/price/pnl_rub); только чтение.

        Ключи API — figi; тикер и лот берём из уже разрешённых self.bot.instruments.
        Пустой список вне сессии и при отсутствии снимка портфеля.
        """
        instruments = getattr(self.bot, "instruments", {}) or {}
        by_figi: dict[str, tuple[str, dict]] = {}
        for ticker, instrument in instruments.items():
            instrument = instrument or {}
            figi = instrument.get("figi")
            if figi:
                by_figi[figi] = (ticker, instrument)
        rows: list[dict] = []
        for figi, pos in (portfolio_positions or {}).items():
            if not isinstance(pos, dict):
                continue
            ticker, instrument = by_figi.get(figi, (figi, {}))
            try:
                quantity = float(pos.get("quantity") or 0)
                avg = float(pos.get("average_position_price") or 0)
                current = float(pos.get("current_price") or 0)
                lot = int(instrument.get("lot") or 1)
            except (TypeError, ValueError):
                continue
            lots = int(quantity // lot) if lot else int(quantity)
            pnl_rub = round((current - avg) * quantity, 2) if avg > 0 and current > 0 else 0.0
            rows.append(
                {
                    "ticker": ticker,
                    "lots": lots,
                    "price": round(current, 4),
                    "pnl_rub": pnl_rub,
                }
            )
        return rows

    def publish(self, iteration: int | None = None, snapshot: dict | None = None,
                error: str | None = None, state: str | None = None) -> None:
        if iteration is not None:
            self.iteration = int(iteration)
        if state is not None:
            self.state = state
        if snapshot is not None:
            self.equity = snapshot.get("total_amount_rub")
            self.cash = snapshot.get("cash_rub")
            portfolio_positions = snapshot.get("positions") or {}
            self.positions = len(portfolio_positions)
            self.position_rows = self._positions_payload(portfolio_positions)
        if error is not None:
            self.last_error = error
        active, reason = self._kill_switch()
        payload = {
            "pid": os.getpid(),
            "state": self.state,
            "started_at": self.started_at,
            "mode": getattr(self.bot.config, "mode", None),
            "iteration": self.iteration,
            "equity_rub": self.equity,
            "cash_rub": self.cash,
            "positions_count": self.positions,
            "positions": self.position_rows,
            "kill_switch_active": active,
            "kill_switch_reason": reason,
            "last_error": self.last_error,
        }
        try:
            control.write_status(self.reports_dir, payload)
        except Exception as exc:  # статус не должен ронять торговый цикл
            log.exception("Демон: не удалось записать daemon_status.json (%s)", exc)

    def on_tick(self) -> bool:
        """Дренаж очереди команд; True — запрошен стоп (завершить цикл)."""
        try:
            commands = control.drain_commands(self.reports_dir)
        except Exception as exc:
            log.exception("Демон: очередь команд недоступна (%s)", exc)
            return self.stop_requested
        for command in commands:
            name = command.get("name")
            if name == "stop":
                self.stop_requested = True
                log.info("Демон: получена команда stop — завершаем текущую итерацию")
            elif name == "kill_switch_reset":
                try:
                    self.bot.reset_kill_switch()
                except Exception as exc:
                    log.exception("Демон: kill_switch_reset не выполнен (%s)", exc)
        if self.stop_requested:
            self.publish(state="stopping")
        return self.stop_requested

    def finish(self) -> None:
        self.publish(state="stopped")


def run_headless(bot, max_iterations: int | None = None, reports_dir=None, *, acquire: bool = True) -> int:
    """Foreground-демон: lock, tick-цикл, статус после итерации, команды.

    Возвращает 0 — штатное завершение, 3 — lock занят живым процессом.
    """
    reports_dir = Path(reports_dir or bot.config.reports_dir)
    if acquire and not control.acquire_lock(reports_dir):
        log.error("Демон уже запущен (lock %s занят живым pid) — второй экземпляр не стартует", reports_dir)
        return 3
    runner = _DaemonRunner(bot, reports_dir)
    bot._daemon_runner = runner
    try:
        runner.publish(iteration=0, state="running")
        bot.run_forever(max_iterations=max_iterations)
        return 0
    except Exception as exc:
        runner.last_error = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        runner.finish()
        if acquire:
            control.release_lock(reports_dir)

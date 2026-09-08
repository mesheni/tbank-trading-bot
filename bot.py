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
import logging
import time
from dataclasses import replace
from pathlib import Path

import pandas as pd

from backtest import RiskConfig  # noqa: F401  (реэкспорт для удобства)
from config import MSK, Config
from features import build_features, ensure_news_columns
from models.registry import ModelArtifact, load_artifact, model_factories
from nlp.agenda import AgendaScore, batch_score_news, score_agenda
from nlp.embedder import NewsEmbedder
from nlp.sentiment import make_sentiment
from notify import Notifier
from strategy import Decision, PortfolioState, Position, RiskConfig, decide
import ui
from tbank.api import TBankAPI
from tbank.market_data import connect, download_candles, load_candles, load_news, store_news
from tbank.rest import TBankRestClient
from tbank.trader import Trader, make_journal_row

log = logging.getLogger(__name__)

# Расписание MOEX по умолчанию (если TradingSchedules недоступен): основная сессия
DEFAULT_SESSION = (dt.time(9, 50), dt.time(18, 50))


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


class TradingBot:
    def __init__(self, config: Config, risk: RiskConfig | None = None):
        config.validate()
        self.config = config
        self.risk = risk or RiskConfig(
            max_position_pct=config.max_position_pct,
            stop_loss_pct=config.stop_loss_pct,
            take_profit_pct=config.take_profit_pct,
            min_abs_return=config.min_abs_return,
            news_sentiment_gate=config.news_sentiment_gate,
            commission_pct=config.commission_pct,
            reversal_exit_mult=config.reversal_exit_mult,
        )
        self.client = TBankRestClient(config.token, config.mode)
        self.api = TBankAPI(self.client, order_market_fallback=config.order_fallback_to_market)
        self.trader = Trader(self.api, Path(config.reports_dir) / "journal.csv")
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

        self.embedder = NewsEmbedder(config.embedding_model, enabled=config.nlp_embedder)
        self.sentiment = make_sentiment(config.sentiment_model, preference=config.nlp_sentiment)

        self.artifacts: dict[str, ModelArtifact] = {}
        self.news_sentiments: dict[str, dict[str, float]] = {}
        self._news_updated_at: dt.datetime | None = None
        self._running = False

    # ---------- Сессия ----------

    def in_trading_session(self, now: dt.datetime | None = None) -> bool:
        return self.calendar.active(now)

    # ---------- Данные и сигналы ----------

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
        sentiments = self.news_sentiments.setdefault(ticker, {})
        for news_id, score in batch_score_news(news_df, self.sentiment).items():
            sentiments.setdefault(news_id, score)
        return score_agenda(
            news_df,
            sentiments,
            now,
            window_hours=48.0,
            half_life_hours=self.config.news_half_life_hours,
            embedder=self.embedder if self.embedder.available else None,
        )

    def _artifact(self, ticker: str) -> ModelArtifact:
        artifact = self.artifacts.get(ticker)
        if artifact is None:
            artifact = load_artifact(
                self.config.models_dir, ticker, self.config.candle_interval, self.config.forecast_horizon
            )
            self.artifacts[ticker] = artifact
        return artifact

    def _model_allowed(self, ticker: str, artifact: ModelArtifact) -> bool:
        """Гейт качества модели: без подтверждённого направления — новых входов нет.

        Открытые позиции обслуживаются как обычно (стопы/тейки/выход по развороту);
        гейт блокирует только открытие новых позиций. Модели без замеренной
        directional_acc (NaN) тоже не допускаются: нет доказательств преимущества.
        """
        dir_acc = artifact.metrics.get("directional_acc")
        if dir_acc is None or pd.isna(dir_acc):
            return False
        return float(dir_acc) >= self.config.min_model_dir_acc

    def predict(self, ticker: str, candles: pd.DataFrame, agenda: AgendaScore) -> tuple[float, float]:
        """Возвращает (прогноз доходности на горизонт, новостной скор)."""
        artifact = self._artifact(ticker)

        features = ensure_news_columns(build_features(candles, self.config.forecast_horizon))

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

    def step_ticker(self, ticker: str, instrument: dict, portfolio: PortfolioState) -> None:
        figi = instrument["figi"]
        artifact = self._artifact(ticker)
        if figi not in portfolio.positions and not self._model_allowed(ticker, artifact):
            dir_acc = artifact.metrics.get("directional_acc")
            log.info(
                "%-5s пропуск: модель %s, dir_acc=%s < %.2f (MIN_MODEL_DIR_ACC) — новых входов нет",
                ticker,
                artifact.kind,
                "n/a" if dir_acc is None or pd.isna(dir_acc) else f"{dir_acc:.3f}",
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
                    f"order-not-filled:{ticker}", f"заявка BUY {ticker} не исполнена",
                    f"Статус: {state.get('status') or 'неизвестен'}\n"
                    f"Запрошено {decision.lots} лот(ов) по {price:.2f}\nПричина решения: {decision.reason}",
                )
            else:
                if executed < decision.lots:
                    log.warning("%s: частичное исполнение покупки: %d из %d лотов", ticker, executed, decision.lots)
                    self.notifier.send_throttled(
                        f"partial-fill:{ticker}", f"частичная покупка {ticker}",
                        f"Исполнено {executed} из {decision.lots} лотов по {exec_price:.2f}\n"
                        f"Причина решения: {decision.reason}",
                    )
                portfolio.positions[figi] = Position(figi, ticker, executed, lot, exec_price)
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
                log.warning(
                    "%s: продажа не исполнена (статус %s) — позиция остаётся",
                    ticker, state.get("status") or "неизвестен",
                )
                self.notifier.send_throttled(
                    f"order-not-filled:{ticker}", f"заявка SELL {ticker} не исполнена",
                    f"Статус: {state.get('status') or 'неизвестен'}\n"
                    f"Пробовали продать {position.lots} лот(ов) по {price:.2f}\nПричина решения: {decision.reason}",
                )
            else:
                if executed < position.lots:
                    log.warning(
                        "%s: частичная продажа: %d из %d лотов, остаток держим",
                        ticker, executed, position.lots,
                    )
                    self.notifier.send_throttled(
                        f"partial-fill:{ticker}", f"частичная продажа {ticker}",
                        f"Исполнено {executed} из {position.lots} лотов по {exec_price:.2f}\n"
                        f"Причина решения: {decision.reason}",
                    )
                    portfolio.positions[figi] = Position(
                        figi, ticker, position.lots - executed, lot, position.avg_price
                    )
                else:
                    del portfolio.positions[figi]
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
        self.trader.initialize_balance(self.config.sandbox_initial_rub)
        log.info(
            "Бот запущен: mode=%s tickers=%s interval=%s horizon=%d",
            self.config.mode, ",".join(self.instruments), self.config.candle_interval, self.config.forecast_horizon,
        )
        try:
            while self._running and (max_iterations is None or iteration < max_iterations):
                iteration += 1
                if not self.in_trading_session():
                    log.info("Вне торговой сессии MOEX — пауза %d c", self.config.loop_interval_sec)
                    time.sleep(self.config.loop_interval_sec)
                    continue

                try:
                    self.update_news()
                    portfolio = self._load_portfolio()
                    for ticker, instrument in self.instruments.items():
                        try:
                            self.step_ticker(ticker, instrument, portfolio)
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
                    pnl_pct = snapshot["total_amount_rub"] / self.config.sandbox_initial_rub - 1.0
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
                    self.notifier.send_throttled(
                        "iteration-error", "ошибка итерации бота",
                        f"{type(exc).__name__}: {exc}\n(не чаще 1 письма в 30 минут; "
                        "живость бота проверяет cli.py watchdog)",
                    )

                if max_iterations is None or iteration < max_iterations:
                    time.sleep(self.config.loop_interval_sec)
        finally:
            self._running = False
            self.conn.close()
            log.info("Бот остановлен")

    def _load_portfolio(self) -> PortfolioState:
        raw = self.trader.portfolio()
        positions = {}
        for figi, pos in raw["positions"].items():
            instrument = next((i for i in self.instruments.values() if i["figi"] == figi), None)
            ticker = (instrument.get("ticker") or figi) if instrument else figi
            if pos["quantity"] > 0:
                positions[figi] = Position(
                    # дробные количества (корп. действия) округляются до целых лотов/штук
                    figi, ticker, int(round(pos["quantity"])), int(instrument.get("lot", 1)) if instrument else 1,
                    pos["average_position_price"] or pos["current_price"],
                )
        return PortfolioState(cash=raw["cash_rub"], equity=raw["total_amount_rub"], positions=positions)

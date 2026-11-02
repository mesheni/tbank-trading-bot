"""CLI торгового бота: setup / smoke / download / news / train / backtest / run / report / status.

Примеры:
    python cli.py setup                  # первый запуск: smoke → download → news → train → backtest
    python cli.py status                 # офлайн-сводка: конфиг, данные, модели, живость
    python cli.py smoke                  # проверка токена, счёта и API
    python cli.py download --days 720    # выгрузка истории свечей
    python cli.py news                   # выгрузка новостей
    python cli.py train                  # сравнение моделей, выбор лучшей
    python cli.py train --capital 30000  # то же, но с фильтром тикеров по лоту под капитал
    python cli.py backtest               # бэктест стратегии с лучшей моделью
    python cli.py backtest --capital 30000  # бэктест на счёте 30 тыс. руб
    python cli.py run                    # торговый цикл в sandbox
    python cli.py run --iterations 5     # пробный запуск, 5 итераций
    python cli.py report                 # состояние счёта и журнал сделок
    python cli.py status --json          # машинночитаемая сводка (для мониторинга)
    python cli.py check-config           # проверка конфигурации без обращения к API
    python cli.py kill-switch-reset      # снять kill-switch после срабатывания
    python cli.py watchdog --restart     # проверка живости бота (для cron)
    python cli.py digest                 # письмо-дайджест состояния (для cron)
    python cli.py send-test-email        # проверка SMTP-настроек
    python cli.py normalize              # проверить счёт на соответствие бюджету
    python cli.py reset-sandbox          # пересоздать sandbox-счёт с бюджетом

Флаги команды: python cli.py <command> --help.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import logging
import logging.handlers
import math
import shutil
import sys
import time
from dataclasses import replace
from pathlib import Path

import pandas as pd
import requests

import control
import ui
from backtest import run_backtest, save_report
from config import BASE_DIR, CAPITAL_PROFILES, Config, resolve_capital_profile
from features import build_features, ensure_news_columns
from models.registry import (
    artifact_path,
    baseline_refit_every,
    evaluate_all,
    load_artifact,
    model_factories,
    select_best,
    train_and_save,
    walk_forward_baselines,
    walk_forward_lgbm,
)
from stats_utils import n_test_points
from strategy import RiskConfig, is_affordable
from tbank.api import TBankAPI
from tbank.market_data import (
    connect,
    download_candles,
    load_candles,
    load_instrument,
    load_news,
    store_instruments,
    store_news,
)
from tbank.rest import APIError, TBankRestClient

__VERSION__ = "1.2.0"


def _sandbox_budget(config) -> float:
    """Бюджет песочницы: ACCOUNT_BUDGET_RUB, если задан, иначе SANDBOX_INITIAL_RUB."""
    return getattr(config, "account_budget_rub", None) or config.sandbox_initial_rub


def _json_safe(obj):
    """Рекурсивно заменяет NaN/inf на None: RFC 8259 не допускает такие числа."""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {key: _json_safe(value) for key, value in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(value) for value in obj]
    return obj


def _mask_secret(value: str) -> str:
    """Маска секрета: короткий (<8) скрывается целиком, длинный — первые 4 символа + «...»."""
    if len(value) < 8:
        return "***"
    return f"{value[:4]}..."


class ColorFormatter(logging.Formatter):
    """Красит уровень важности; текст сообщений окрашивается там, где создаётся."""

    LEVEL_COLORS = {
        "DEBUG": ui.DIM,
        "WARNING": ui.YELLOW,
        "ERROR": ui.BRIGHT_RED,
        "CRITICAL": ui.BRIGHT_RED,
    }

    def format(self, record: logging.LogRecord) -> str:
        color = self.LEVEL_COLORS.get(record.levelname)
        if color and ui.color_enabled(sys.stderr):
            record.levelname = ui.paint(f"{record.levelname:<7}", color, stream=sys.stderr)
        else:
            record.levelname = f"{record.levelname:<7}"
        return super().format(record)


logging.basicConfig(level=logging.INFO)
_handler = logging.root.handlers[0]
_handler.setFormatter(
    ColorFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S")
)
log = logging.getLogger("cli")


def ok(message: str) -> None:
    print(f"{ui.paint('[OK]', ui.BRIGHT_GREEN)} {message}")


def warn(message: str) -> None:
    print(f"{ui.paint('[!!]', ui.BRIGHT_RED)} {ui.paint(message, ui.YELLOW)}", file=sys.stderr)


def make_api(config: Config) -> TBankAPI:
    return TBankAPI(
        TBankRestClient(config.token, config.mode),
        order_market_fallback=config.order_fallback_to_market,
    )


def _pick_account(config, accounts: list[dict]) -> str:
    """ID счёта для отчётной команды: ACCOUNT_ID из .env, иначе первый из списка."""
    configured = getattr(config, "account_id", "")
    if configured:
        if not any(acc.get("id") == configured for acc in accounts):
            raise RuntimeError(f"ACCOUNT_ID={configured} не найден среди счетов API")
        return configured
    if len(accounts) > 1:
        print(
            f"[!!] ACCOUNT_ID не задан, а счетов несколько ({len(accounts)}) — "
            f"для отчёта используется первый: {accounts[0]['id']}. "
            "Укажите ACCOUNT_ID в .env, чтобы выбрать нужный счёт.",
            file=sys.stderr,
        )
    return accounts[0]["id"]


def cmd_smoke(config: Config) -> int:
    print(ui.header(f"SMOKE · режим {config.mode}"))
    api = make_api(config)
    accounts = api.get_accounts()
    ok(f"Счетов: {len(accounts)}")
    for acc in accounts[:3]:
        print(f"     - {acc.get('id')} ({acc.get('type')})")
    if config.mode == "sandbox":
        if not accounts:
            budget = _sandbox_budget(config)
            account_id = api.open_sandbox_account()
            ok(f"Открыт sandbox-счёт: {account_id}")
            api.pay_in(account_id, budget)
            ok(f"Пополнен на {ui.fmt_money(budget)} руб (бюджет из .env)")
        else:
            account_id = accounts[0]["id"]

    instruments = api.resolve_instruments(config.tickers)
    ok("Инструменты: " + ", ".join(f"{t}({i['figi']})" for t, i in instruments.items()))

    conn = connect(config.db_path)
    first_ticker, first = next(iter(instruments.items()))
    n = download_candles(api, conn, first["figi"], "day", days_back=30, ticker=first_ticker)
    df = load_candles(conn, first["figi"], "day")
    last = df.iloc[-1] if not df.empty else None
    ok(
        f"Свечи day {first_ticker}: {len(df)} строк (добавлено {n})"
        + (f", последняя close={last['close']:.2f}" if last is not None else "")
    )

    problems: list[str] = []
    try:
        news = api.get_news(max_pages=1)
        sber_uid = first.get("uid") or first["figi"]
        linked = [
            item for item in news
            if sber_uid in (item.get("instrument_uids") or [item.get("instrument_uid", "")])
        ]
        ok(
            f"Новости: {len(news)} шт. "
            + ui.paint(f"(с упоминанием {first_ticker}: {len(linked)})", ui.CYAN)
        )
        for item in (linked or news)[:2]:
            print(f"     {item['pub_time'][:19]} | {item['title'][:70]}")
    except Exception as exc:
        warn(f"Новости недоступны: {exc}")
        problems.append("новости недоступны")

    conn.close()
    print()
    if problems:
        warn(f"Smoke-тест пройден частично: {', '.join(problems)}")
        return 1
    ok("Smoke-тест пройден")
    return 0


def cmd_download(config: Config, days: int) -> int:
    api = make_api(config)
    instruments = api.resolve_instruments(config.tickers)
    conn = connect(config.db_path)
    store_instruments(conn, instruments)  # лоты/uid для офлайн-команд (backtest)
    total = len(config.tickers)
    for i, (ticker, instrument) in enumerate(instruments.items(), 1):
        print(f"[{i}/{total}] {ticker}")
        print(f"Выгрузка {ticker} ({config.candle_interval}, {days} дней)...")
        n = download_candles(api, conn, instrument["figi"], config.candle_interval, days_back=days, ticker=ticker)
        print(f"  {ticker}: {n} свечей")
    conn.close()
    return 0


def cmd_news(config: Config) -> int:
    api = make_api(config)
    instruments = api.resolve_instruments(config.tickers)
    conn = connect(config.db_path)
    items = api.get_news(max_pages=5)
    stored = store_news(conn, items)
    print(
        ui.header(f"НОВОСТИ · загружено {len(items)}, записей в БД: {stored}")
    )

    for ticker, instrument in instruments.items():
        uid = instrument.get("uid") or instrument["figi"]
        df = load_news(conn, uid)
        linked = load_news(conn, uid, include_market=False)
        print(
            f"  {ui.paint(ticker, ui.BOLD)}: {len(df)} новостей (с макро) / "
            + ui.paint(f"{len(linked)} привязанных напрямую", ui.CYAN)
        )
        for _, row in df.head(3).iterrows():
            print(f"    {str(row['pub_time'])[:19]} | {row['title'][:70]}")
    conn.close()
    return 0


def _render_metrics_table(metrics: pd.DataFrame, best: str | None) -> str:
    """Таблица сравнения моделей: лучшая строка выделена, sharpe/net/live окрашены по знаку."""

    def fmt_num(v: float, fmt: str, na: str = "--") -> str:
        return na if pd.isna(v) else fmt.format(v)

    rows = []
    for name, m in metrics.iterrows():
        rows.append(
            [
                ">" if name == best else "",
                name,
                fmt_num(m["rmse"], "{:.5f}"),
                fmt_num(m["mae"], "{:.5f}"),
                fmt_num(m["directional_acc"], "{:.1%}"),
                fmt_num(m["strategy_sharpe"], "{:+.2f}"),
                fmt_num(m["strategy_sharpe_net"], "{:+.2f}"),
                fmt_num(m["live_sharpe_net"], "{:+.2f}"),
                fmt_num(m["live_trades"], "{:.0f}"),
                f"{int(m['n_points']):d}",
            ]
        )

    def cell_paint(r: int, c: int, text: str) -> str:
        name = metrics.index[r]
        if name == best:
            return ui.paint(text, ui.BRIGHT_GREEN, ui.BOLD)
        if c in (5, 6, 7):  # колонки sharpe / net / live
            v = metrics.iloc[r][["strategy_sharpe", "strategy_sharpe_net", "live_sharpe_net"][c - 5]]
            if pd.isna(v):
                return text
            if v > 0:
                return ui.paint(text, ui.GREEN)
            if v < 0:
                return ui.paint(text, ui.RED)
            return ui.paint(text, ui.DIM)
        return text

    return ui.render_table(
        ["", "модель", "rmse", "mae", "dir_acc", "sharpe", "net", "live", "сд", "точек"],
        rows,
        aligns=["c", "l", "r", "r", "r", "r", "r", "r", "r", "r"],
        paint_cell=cell_paint,
    )


def cmd_train(config: Config, capital: float | None = None) -> int:
    conn = connect(config.db_path)
    summary = {}
    trained = []
    # учёт капитала: тикеры, чей 1 лот не влезает в бюджет позиции при данном
    # капитале, не обучаются — стратегия всё равно не сможет купить ни одного лота
    capital_profile = None
    pos_pct = None
    if capital:
        capital_profile = resolve_capital_profile(capital)
        pos_pct = CAPITAL_PROFILES[capital_profile]["max_position_pct"]
        print(
            ui.header(
                f"Капитал {ui.fmt_money(capital)} руб · профиль {capital_profile} "
                f"· позиция до {pos_pct:.0%} капитала"
            )
        )
    for i, ticker in enumerate(config.tickers, 1):
        print(f"[{i}/{len(config.tickers)}] {ticker}")
        df = load_candles(conn, ticker, config.candle_interval)
        if df.empty:
            warn(f"{ticker}: нет свечей в {config.db_path} — выполните `python cli.py download`")
            continue
        if len(df) < 500:
            warn(f"{ticker}: всего {len(df)} свечей — мало для обучения (нужно >= 500), пропускаем. "
                 f"Возможно, бумага торгуется недавно.")
            continue
        if capital:
            instrument = load_instrument(conn, ticker)
            if not instrument:
                warn(f"{ticker}: тикера нет в кэше instruments — фильтр по лоту пропущен "
                     f"(заполнит download/smoke/run)")
            else:
                lot_size = int(instrument["lot"])
                price = float(df["close"].iloc[-1])
                if not is_affordable(price, lot_size, capital, pos_pct):
                    warn(
                        f"{ticker}: 1 лот {ui.fmt_money(price * lot_size)} руб дороже бюджета позиции "
                        f"{ui.fmt_money(capital * pos_pct)} руб при капитале {ui.fmt_money(capital)} руб "
                        f"— пропускаем"
                    )
                    continue
        print(ui.header(f"{ticker} · {len(df)} свечей · горизонт {config.forecast_horizon} бар(а)"))
        round_trip = 2 * (config.commission_pct + config.slippage_pct)
        reversal_mult = getattr(config, "reversal_exit_mult", 2.0)
        # один walk-forward на тикер: таблица и артефакт оцениваются по нему же
        metrics, results = evaluate_all(
            df, config.forecast_horizon, round_trip_cost=round_trip, reversal_mult=reversal_mult
        )
        # таблицу печатаем до логов train_and_save; маркер «>» — тот же выбор, что и в артефакт
        print(_render_metrics_table(metrics, select_best(metrics)))
        try:
            artifact = train_and_save(
                df, config.forecast_horizon, config.models_dir, ticker, config.candle_interval,
                cost_floor=2 * (config.commission_pct + config.slippage_pct),
                reversal_mult=reversal_mult,
                evaluation=(metrics, results),
            )
        except ValueError as exc:
            warn(f"{ticker}: {exc} — тикер пропущен, старый артефакт (если был) не тронут")
            continue
        trained.append(ticker)
        summary[ticker] = {"best": artifact.kind, **artifact.metrics}
        best = ui.paint(artifact.kind, ui.BRIGHT_GREEN, ui.BOLD)
        live_sharpe = artifact.metrics.get("live_sharpe_net")
        live_trades = artifact.metrics.get("live_trades")
        if live_sharpe is not None and not pd.isna(live_sharpe):
            score_str = f"live sharpe {ui.fmt_signed(live_sharpe, '{{:+.2f}}')}, сделок {int(live_trades)}"
        else:
            score_str = f"net sharpe {ui.fmt_signed(artifact.metrics.get('strategy_sharpe_net', 0.0), '{{:+.2f}}')}"
        print(
            f"  Лучшая модель: {best} ({score_str}) · "
            f"порог входа {ui.paint(f'{artifact.threshold:.4f}', ui.CYAN)}\n"
        )
    conn.close()
    if summary:
        payload: dict = {"tickers": summary}
        if capital:
            payload |= {"capital_rub": capital, "profile": capital_profile}
        config.reports_dir.mkdir(parents=True, exist_ok=True)
        (config.reports_dir / "train_summary.json").write_text(
            json.dumps(_json_safe(payload), ensure_ascii=False, indent=2, allow_nan=False),
            encoding="utf-8",
        )
        print(
            f"Итого обучено: {len(summary)}/{len(config.tickers)}. "
            f"Артефакты: {ui.paint(str(config.models_dir), ui.CYAN)}, "
            f"сводка: {ui.paint(str(config.reports_dir / 'train_summary.json'), ui.CYAN)}"
        )
    return 1 if not summary else 0


def cmd_backtest(config: Config, capital: float | None = None) -> int:
    conn = connect(config.db_path)
    # капитал бэктеста: --capital или бюджет счёта из .env (ACCOUNT_BUDGET_RUB/SANDBOX_INITIAL_RUB)
    initial_cash = capital if capital else _sandbox_budget(config)
    risk = RiskConfig(
        max_position_pct=config.max_position_pct,
        stop_loss_pct=config.stop_loss_pct,
        take_profit_pct=config.take_profit_pct,
        min_abs_return=config.min_abs_return,
        commission_pct=config.commission_pct,
        slippage_pct=config.slippage_pct,
        commission_min_rub=config.commission_min_rub,
        reversal_exit_mult=config.reversal_exit_mult,
        max_total_exposure_pct=config.max_total_exposure_pct,
    )
    # тональность нужна и офлайн: скоры кэшируются в БД, живой цикл их разделяет
    from bot import ensure_sentiments, model_passes_live_gate
    from nlp.agenda import sentiment_series
    from nlp.sentiment import make_sentiment

    scorer = make_sentiment(config.sentiment_model, preference=config.nlp_sentiment)
    model_key = "lexicon" if getattr(scorer, "name", "") == "lexicon" else f"transformers:{config.sentiment_model}"

    ran = 0
    for ticker in config.tickers:
        df = load_candles(conn, ticker, config.candle_interval)
        if df.empty:
            print(f"[!!] {ticker}: нет свечей в {config.db_path} — выполните `python cli.py download`")
            continue
        artifact = None
        try:
            artifact = load_artifact(config.models_dir, ticker, config.candle_interval, config.forecast_horizon)
        except FileNotFoundError as exc:
            warn(f"{ticker}: {exc}")
            continue
        if not model_passes_live_gate(artifact.metrics, config.min_model_dir_acc):
            warn(
                f"{ticker}: модель {artifact.kind} не прошла бы live-гейт — "
                "в боте новых входов не будет"
            )
        features = ensure_news_columns(build_features(df, config.forecast_horizon))

        instrument = load_instrument(conn, ticker)
        lot_size = int(instrument["lot"]) if instrument else 1
        if not instrument:
            warn(f"{ticker}: тикера нет в кэше instruments — считаю лот=1 (заполнит download/smoke/run)")

        # порог входа как в боте: max(MIN_ABS_RETURN, рекомендация модели)
        eff_risk = replace(
            risk,
            min_abs_return=max(risk.min_abs_return, artifact.threshold or 0.0),
        )
        header = f"{ticker} · модель {artifact.kind} · порог входа {eff_risk.min_abs_return:.4f} · лот {lot_size}"
        if capital:
            header += f" · капитал {ui.fmt_money(capital)} руб"

        # историческая серия сентимента: новостные фильтры валидируются на истории
        sent_series = None
        uid = (instrument or {}).get("uid") or ""
        news_df = load_news(conn, uid) if uid else pd.DataFrame()
        if not news_df.empty:
            sentiments = ensure_sentiments(conn, news_df, scorer, model_key)
            sent_series = sentiment_series(
                df.index, news_df, sentiments,
                window_hours=48.0, half_life_hours=config.news_half_life_hours,
            )
            header += f" · новости: {len(news_df)} шт."
        else:
            header += " · новости: нет ряда (фильтр нейтрален)"
        print(ui.header(header))

        # walk-forward предсказания лучшей моделью: только на прошлом, без утечки
        # (артефактная модель обучена на всей истории — для оценки на ней непригодна)
        # расписание переобучения — то же, что и при отборе модели
        test_points = features.index[-n_test_points(len(features)):]
        if artifact.kind == "lgbm":
            preds = walk_forward_lgbm(features, test_points, config.forecast_horizon)
        else:
            preds = walk_forward_baselines(
                df["close"], features["target"], config.forecast_horizon,
                model_factories(config.forecast_horizon)[artifact.kind], test_points,
                refit_every=baseline_refit_every(artifact.kind),
            )
        preds_test = preds

        result = run_backtest(
            df, preds_test, eff_risk, ticker=ticker, lot_size=lot_size, sentiment=sent_series,
            initial_cash=initial_cash,
        )
        name = f"{ticker}_{config.candle_interval}_h{config.forecast_horizon}"
        if capital:
            name += f"_c{capital:g}"
        path = save_report(result, config.reports_dir, name)
        ran += 1
        m = result.metrics
        print(
            f"  Доходность {ui.fmt_signed(m['total_return'])} | "
            f"sharpe {ui.fmt_signed(m['sharpe'], '{:+.2f}')} | "
            f"просадка {ui.fmt_signed(m['max_drawdown'])} | "
            f"сделок {m['n_trades']} | win-rate {m['win_rate']:.0%} | "
            f"комиссии {m.get('total_commission', 0):,.0f} руб"
        )
        print(
            f"  Buy&hold того же окна: {ui.fmt_signed(m.get('benchmark_buyhold', 0))} · "
            f"стратегия минус бенчмарк: "
            f"{ui.fmt_signed(m.get('total_return', 0) - m.get('benchmark_buyhold', 0))}"
        )
        if m["n_trades"] == 0:
            warn("сделок нет: прогнозы модели не превышали порог входа (консервативно, деньги целы)")
        print(f"  Отчёт: {ui.paint(str(path), ui.CYAN)}\n")
    conn.close()
    print(f"Все отчёты: {ui.paint(str(config.reports_dir), ui.CYAN)}")
    if not ran:
        warn("ни один тикер не прошёл бэктест (нет данных или моделей)")
        return 1
    return 0


def _real_trading_guard(config: Config) -> int | None:
    """REAL-режим без CONFIRM_REAL_TRADING=YES — отказ до любых обращений к API.

    Единая точка для `run`, `daemon run`, systemd и Task Scheduler.
    """
    if config.mode == "real" and not getattr(config, "confirm_real_trading", False):
        print(
            "[!!] MODE=real: для торговли на реальном счёте задайте CONFIRM_REAL_TRADING=YES "
            "в окружении или .env",
            file=sys.stderr,
        )
        return 2
    return None


def _ensure_daemon_log(config: Config) -> None:
    """pythonw без консоли не должен терять лог: по умолчанию var/daemon.log."""
    if config.log_file is not None:
        return
    config.log_file = BASE_DIR / "var" / "daemon.log"
    _attach_file_log(config)


def _run_headless(config: Config, max_iterations: int | None) -> int:
    """Общий headless-вход: REAL-гейт, лог, lock, сборка бота, tick-цикл."""
    guard = _real_trading_guard(config)
    if guard is not None:
        return guard
    _ensure_daemon_log(config)

    from bot import FakeTBankAPI, TradingBot, run_headless

    reports_dir = Path(config.reports_dir)
    # lock берём ДО сборки бота: второй экземпляр не должен даже открывать счёт
    if not control.acquire_lock(reports_dir):
        print(
            "[!!] Демон уже запущен (daemon.lock занят живым процессом) — "
            "второй экземпляр не запускается",
            file=sys.stderr,
        )
        return 3
    try:
        if getattr(config, "tbank_fake_api", False):
            bot = TradingBot(config, api=FakeTBankAPI(config.tickers))
        else:
            # артефакты обязательны: без модели бот не может прогнозировать
            missing = [
                t for t in config.tickers
                if not artifact_path(
                    config.models_dir, t, config.candle_interval, config.forecast_horizon
                ).exists()
            ]
            if missing:
                warn(f"Нет артефактов моделей для: {', '.join(missing)} — выполните `python cli.py train`")
            bot = TradingBot(config)
            for ticker in missing:
                bot.instruments.pop(ticker, None)
            if not bot.instruments:
                warn("Ни одного тикера с моделью — запуск невозможен. Сначала: python cli.py train")
                return 2
        return run_headless(bot, max_iterations, reports_dir, acquire=False)
    finally:
        control.release_lock(reports_dir)


def cmd_run(config: Config, max_iterations: int | None) -> int:
    """Обратная совместимость: `cli.py run` — тот же headless-путь, что daemon run."""
    return cmd_daemon_run(config, max_iterations)


def cmd_daemon_run(config: Config, max_iterations: int | None = None) -> int:
    print(ui.mode_banner(config.mode))  # первая строка вывода: режим виден сразу
    return _run_headless(config, max_iterations)


def cmd_daemon_status(config: Config) -> int:
    """Сводка daemon_status.json; отсутствие/порча файла — не ошибка, а «не запущен»."""
    print(ui.mode_banner(config.mode))
    payload = control.read_status(config.reports_dir)
    if payload is None:
        print("  демон не запущен (файла daemon_status.json нет)")
        return 0
    if payload.get("corrupt"):
        print("  демон не запущен (daemon_status.json повреждён)")
        return 0
    print(ui.header("ДЕМОН"))
    print(
        f"  Состояние: {payload.get('state')} · pid {payload.get('pid')} · "
        f"итерация {payload.get('iteration')}"
    )
    print(f"  Режим: {payload.get('mode')} · обновлено: {payload.get('updated_at')}")
    equity = payload.get("equity_rub")
    cash = payload.get("cash_rub")
    if equity is not None or cash is not None:
        print(
            f"  Капитал: {ui.fmt_money(equity) if equity is not None else '--'} руб · "
            f"кэш: {ui.fmt_money(cash) if cash is not None else '--'} руб · "
            f"позиций: {payload.get('positions_count')}"
        )
    if payload.get("kill_switch_active"):
        warn(f"kill-switch активен: {payload.get('kill_switch_reason') or 'причина неизвестна'}")
    if payload.get("last_error"):
        warn(f"последняя ошибка: {payload['last_error']}")
    return 0


def _wait_for_daemon_stop(reports_dir, command_id: str, timeout: float, poll_interval: float) -> bool:
    """Ждёт ack команды И состояния stopped; False — таймаут (демон завис/не отвечает)."""
    ack_path = control.paths(reports_dir)["acks"] / f"{command_id}.json"
    deadline = time.monotonic() + max(0.0, float(timeout))
    while True:
        payload = control.read_status(reports_dir)
        stopped = isinstance(payload, dict) and not payload.get("corrupt") and payload.get("state") == "stopped"
        if ack_path.exists() and stopped:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(min(poll_interval, max(0.0, deadline - time.monotonic())))


def cmd_daemon_stop(config: Config, timeout: float = 60.0, poll_interval: float = 0.5) -> int:
    """Ставит команду stop и ждёт ack ≤ timeout; таймаут — вежливый отказ, не зависание."""
    reports_dir = Path(config.reports_dir)
    current = control.read_status(reports_dir)
    if current is None or (not current.get("corrupt") and current.get("state") == "stopped"):
        print("  демон не запущен — останавливать нечего")
        return 0
    command_id = control.enqueue_command(reports_dir, "stop")
    if _wait_for_daemon_stop(reports_dir, command_id, timeout, poll_interval):
        ok(f"Демон остановлен (команда {command_id})")
        return 0
    warn(f"Демон не подтвердил остановку за {timeout:g} с (команда {command_id})")
    return 1


def cmd_setup(config: Config, days: int | None = None, capital: float | None = None) -> int:
    """Первый запуск одной командой: smoke → download → news → train → backtest.

    Каждый шаг — существующая команда; при сбое установка останавливается
    с подсказкой, как продолжить вручную с того же места.
    """
    steps = [
        ("smoke", "проверка токена и API", lambda: cmd_smoke(config)),
        ("download", "выгрузка истории свечей", lambda: cmd_download(config, days or config.history_days)),
        ("news", "выгрузка новостей", lambda: cmd_news(config)),
        ("train", "обучение и выбор моделей", lambda: cmd_train(config, capital)),
        ("backtest", "бэктест стратегии", lambda: cmd_backtest(config, capital)),
    ]
    print(ui.header(f"SETUP · режим {config.mode} · {len(steps)} шагов"))
    for i, (name, title, run) in enumerate(steps, 1):
        print(f"\n{ui.header(f'ШАГ {i}/{len(steps)} · {title.upper()}')}")
        rc = run()
        if rc != 0:
            warn(f"шаг «{title}» не прошёл (код {rc}) — установка остановлена")
            print(
                f"  Исправьте проблему и продолжите вручную: python cli.py {name}, "
                f"затем оставшиеся шаги: {', '.join(n for n, _, _ in steps[i:])}."
            )
            return rc
    print()
    ok("Установка завершена: данные загружены, модели обучены, бэктест прошёл")
    print(
        "  Пробный запуск: python cli.py run --iterations 5 · постоянный: python cli.py run\n"
        "  Мониторинг: python cli.py status / report · письмо-дайджест: python cli.py digest"
    )
    return 0


def _load_flows(reports_dir: Path) -> pd.DataFrame:
    """Журнал движений денег flows.csv: time, amount_rub (±), kind, reason."""
    path = Path(reports_dir) / "flows.csv"
    if not path.exists():
        return pd.DataFrame(columns=["time", "amount_rub", "kind", "reason"])
    flows = pd.read_csv(path)
    flows["time"] = pd.to_datetime(flows["time"], utc=True)
    return flows


def _net_invested(initial_rub: float, flows: pd.DataFrame) -> float:
    """Вложено в торговлю: бюджет ± все движения денег (пополнения и выводы).

    Компенсирующие пары от normalize (adjust + withdraw) в сумме дают ноль,
    поэтому не искажают базу — только фиксируют момент во времени.
    """
    return initial_rub + (float(flows["amount_rub"].sum()) if len(flows) else 0.0)


def _budget_base_warning(config) -> str | None:
    """В real без ACCOUNT_BUDGET_RUB база P&L — дефолтный SANDBOX_INITIAL_RUB, не капитал."""
    if config.mode == "real" and getattr(config, "account_budget_rub", None) is None:
        return (
            f"MODE=real без ACCOUNT_BUDGET_RUB: P&L считается от SANDBOX_INITIAL_RUB "
            f"({config.sandbox_initial_rub:,.0f} руб) и почти наверняка неверен — "
            "задайте ACCOUNT_BUDGET_RUB в .env"
        )
    return None


def _period_pnl(
    equity: pd.DataFrame, flows: pd.DataFrame, cutoff: pd.Timestamp
) -> tuple[float, float, float, float] | None:
    """P&L за окно от cutoff: дельта капитала минус движения денег в окне.

    Возвращает (pnl, базовый капитал окна, сумма движений в окне, дней данных).
    """
    if equity.empty:
        return None
    window = equity[equity["time"] >= cutoff]
    if window.empty:
        return None
    now = equity["time"].iloc[-1]
    base = float(window["total_rub"].iloc[0])
    last = float(equity["total_rub"].iloc[-1])
    moved = 0.0
    if len(flows):
        in_window = flows[(flows["time"] >= cutoff) & (flows["time"] <= now)]
        moved = float(in_window["amount_rub"].sum())
    pnl = (last - base) - moved
    coverage_days = (now - window["time"].iloc[0]).total_seconds() / 86400
    return pnl, base, moved, coverage_days


def cmd_report(config: Config, json_output: bool = False) -> int:
    """Состояние счёта: таблицы для человека или один JSON-объект для скриптов."""

    def show(*args, **kwargs) -> None:
        if not json_output:
            print(*args, **kwargs)

    api = make_api(config)
    accounts = api.get_accounts()
    if not accounts:
        message = "Счетов нет (в sandbox выполните: python cli.py smoke)"
        if json_output:
            print(json.dumps(_json_safe({"ok": False, "error": message}), ensure_ascii=False, allow_nan=False))
        else:
            warn(message)
        return 1
    account_id = _pick_account(config, accounts)
    portfolio = api.get_portfolio(account_id)
    total = portfolio["total_amount_rub"]
    initial = _sandbox_budget(config)
    flows = _load_flows(config.reports_dir)
    invested = _net_invested(initial, flows)
    pnl = total - invested
    pnl_pct = pnl / invested if invested > 1.0 else 0.0
    base_warning = _budget_base_warning(config)
    if base_warning:
        warn(base_warning)
    payload: dict = {
        "ok": True,
        "account_id": account_id,
        "mode": config.mode,
        "capital_rub": total,
        "cash_rub": portfolio["cash_rub"],
        "invested_rub": invested,
        "pnl_rub": pnl,
        "pnl_pct": pnl_pct,
        "warning": base_warning,
        "periods": {},
        "positions": [],
        "journal": None,
        "equity": None,
        "benchmark_pct": None,
    }

    figi_ticker: dict[str, str] = {}
    lot_by_ticker: dict[str, int] = {}
    try:
        instruments = api.resolve_instruments(config.tickers)
        for t, inst in instruments.items():
            figi_ticker[inst["figi"]] = t
            lot_by_ticker[t] = int(inst.get("lot", 1))
    except Exception as exc:
        warn(f"Не удалось разрешить тикеры ({exc}) — позиции покажем по FIGI, лот=1")

    show(ui.header(f"СЧЁТ {account_id} · {config.mode}"))
    show(
        f"  Капитал:  {ui.paint(ui.fmt_money(total) + ' руб', ui.BOLD)} | "
        f"свободно: {ui.fmt_money(portfolio['cash_rub'])} руб"
    )
    show(
        f"  Вложено:  {ui.fmt_money(invested)} руб "
        f"(бюджет {ui.fmt_money(initial)} ± пополнения/выводы из flows.csv)"
    )
    show(
        f"  P&L:      {ui.fmt_signed(pnl, '{:+,.0f} руб')} "
        f"({ui.fmt_signed(pnl_pct)} от вложенного) — пополнения в прибыль не считают"
    )
    if total - invested > max(1000.0, invested * 0.02):
        warn(
            f"капитал выше вложенного на {ui.fmt_money(total - invested)} руб, "
            "но в flows.csv нет записей об этом — похоже, было неучтённое пополнение. "
            "Выполните `python cli.py normalize`: команда сверит счёт с бюджетом; "
            "излишек не выводится — вывода из песочницы через API нет, вернуть бюджет "
            "можно только пересозданием счёта: `python cli.py reset-sandbox`."
        )

    show(ui.header("P&L ПО ПЕРИОДАМ (с поправкой на пополнения/выводы)"))
    equity_file = config.reports_dir / "equity_live.csv"
    equity = None
    if equity_file.exists():
        equity = pd.read_csv(equity_file)
        equity["time"] = pd.to_datetime(equity["time"], utc=True)
        now = equity["time"].iloc[-1]
        for label, days, key in (("день", 1, "day"), ("неделя", 7, "week"), ("месяц", 30, "month")):
            res = _period_pnl(equity, flows, now - pd.Timedelta(days=days))
            if res is None:
                payload["periods"][key] = None
                show(f"  {label:<7} нет данных за окно")
                continue
            pnl_p, base, moved, coverage = res
            payload["periods"][key] = {
                "pnl_rub": pnl_p,
                "base_rub": base,
                "moved_rub": moved,
                "coverage_days": coverage,
            }
            pct_p = pnl_p / base if base > 1.0 else 0.0
            note = ""
            if coverage < days * 0.98:
                note += f" (данных {coverage:.1f} дн. из {days})"
            if abs(moved) > 1.0:
                note += f" [движение денег в окне: {moved:+,.0f} руб]"
            show(
                f"  {label:<7} {ui.fmt_signed(pnl_p, '{:+,.0f} руб')} "
                f"({ui.fmt_signed(pct_p)} от {ui.fmt_money(base)}){note}"
            )
    else:
        show("  Появится после запуска бота (reports/equity_live.csv).")

    show(ui.header("ПОЗИЦИИ"))
    if not portfolio["positions"]:
        show("  (нет открытых позиций)")
    else:
        rows = []
        for figi, pos in portfolio["positions"].items():
            avg = pos["average_position_price"]
            cur = pos["current_price"]
            pnl_pos = (cur / avg - 1) if avg > 0 and cur > 0 else 0.0
            # API отдаёт количество в штуках; лоты показываем рядом, чтобы
            # расхождение «штуки vs лоты» было видно сразу
            name = figi_ticker.get(figi, figi)
            lot = lot_by_ticker.get(name)
            lots_str = f"{int(pos['quantity'] // lot)}" if lot else "--"
            rows.append(
                [
                    name,
                    f"{pos['quantity']:.0f} шт",
                    lots_str,
                    f"{avg:.2f}",
                    f"{cur:.2f}",
                    ui.fmt_signed(pnl_pos),
                ]
            )
            payload["positions"].append(
                {
                    "ticker": name,
                    "figi": figi,
                    "quantity": pos["quantity"],
                    "lots": int(pos["quantity"] // lot) if lot else None,
                    "average_price": avg,
                    "current_price": cur,
                    "pnl_pct": pnl_pos,
                }
            )

        show(ui.render_table(
            ["инструмент", "кол-во", "лотов", "ср. цена", "текущая", "PnL"],
            rows,
            aligns=["l", "r", "r", "r", "r", "r"],
        ))

    show(ui.header("СДЕЛКИ"))
    journal = config.reports_dir / "journal.csv"
    if journal.exists():
        with open(journal, encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        buys = sum(1 for r in rows if r["action"] == "BUY")
        sells = len(rows) - buys

        def commission_of(row: dict) -> float:
            lot = lot_by_ticker.get(row.get("ticker", ""), 1)
            return float(row["lots"]) * float(row["price"]) * lot * config.commission_pct

        total_commission = sum(commission_of(r) for r in rows)
        payload["journal"] = {
            "total": len(rows),
            "buys": buys,
            "sells": sells,
            "estimated_commission_rub": total_commission,
            "last_10": [{**row, "estimated_commission_rub": commission_of(row)} for row in rows[-10:]],
        }
        show(f"  Всего {len(rows)} операций: {ui.paint(f'покупок {buys}', ui.GREEN)}, "
             f"{ui.paint(f'продаж {sells}', ui.RED)} · "
             f"комиссия (оценка, тариф {config.commission_pct:.2%}): ~{total_commission:,.0f} руб")
        show("  Последние 10:")
        for row in rows[-10:]:
            action_color = ui.GREEN if row["action"] == "BUY" else ui.RED
            action = ui.paint(f"{row['action']:<4}", action_color)
            show(f"    {row['time']}  {action} {row['ticker']:<5} {row['lots']} лот(ов) "
                 f"по {row['price']} · комиссия ~{commission_of(row):,.0f} руб · {row['reason']}")
    else:
        show("  Журнала сделок ещё нет — появится после первого запуска бота.")

    show(ui.header("КРИВАЯ КАПИТАЛА (живой прогон)"))
    if equity is not None:
        last_equity = float(equity.iloc[-1]["total_rub"])
        vs_invested = last_equity / invested - 1.0 if invested > 1.0 else 0.0
        payload["equity"] = {
            "points": len(equity),
            "file": equity_file.name,
            "start_time": str(equity.iloc[0]["time"]),
            "start_rub": float(equity.iloc[0]["total_rub"]),
            "last_time": str(equity.iloc[-1]["time"]),
            "last_rub": last_equity,
            "vs_invested_pct": vs_invested,
            "peak_rub": float(equity["total_rub"].max()),
            "min_rub": float(equity["total_rub"].min()),
        }
        show(f"  Точек: {len(equity)} (файл {equity_file.name})")
        show(
            f"  старт:  {equity.iloc[0]['time']} · {ui.fmt_money(equity.iloc[0]['total_rub'])} руб"
        )
        show(
            f"  сейчас: {equity.iloc[-1]['time']} · "
            f"{ui.fmt_signed(vs_invested)} к вложенному · {ui.fmt_money(last_equity)} руб"
        )
        if len(equity) > 2:
            show(
                f"  максимум: {ui.fmt_money(payload['equity']['peak_rub'])} руб | "
                f"минимум: {ui.fmt_money(payload['equity']['min_rub'])} руб"
            )
        payload["benchmark_pct"] = _print_benchmark(config, equity, vs_invested, emit=show)
    else:
        show("  Пока нет данных — появится после запуска бота (reports/equity_live.csv).")
    if json_output:
        print(json.dumps(_json_safe(payload), ensure_ascii=False, allow_nan=False))
    return 0


def _print_benchmark(
    config: Config, equity: pd.DataFrame, bot_return: float, emit=print
) -> float | None:
    """Бенчмарк: buy&hold равных весов тех же тикеров за период живой кривой (локальная БД свечей).

    Возвращает доходность buy&hold (или None, если посчитать не удалось);
    emit — приёмник вывода, чтобы `--json` не печатал текст в stdout.
    """
    from tbank.market_data import connect as db_connect

    try:
        conn = db_connect(config.db_path)
    except Exception as exc:
        emit(f"  Бенчмарк недоступен (БД: {exc})")
        return None
    start, end = equity["time"].iloc[0], equity["time"].iloc[-1]
    returns: list[float] = []
    try:
        for ticker in config.tickers:
            df = load_candles(conn, ticker, config.candle_interval)
            if df.empty:
                continue
            window = df.loc[(df.index >= start) & (df.index <= end), "close"]
            if len(window) >= 2 and float(window.iloc[0]) > 0:
                returns.append(float(window.iloc[-1]) / float(window.iloc[0]) - 1.0)
    finally:
        conn.close()
    if not returns:
        emit("  Бенчмарк: нет свечей в локальной БД за период (выполните download)")
        return None
    buyhold = sum(returns) / len(returns)
    emit(
        f"  vs buy&hold ({len(returns)} тикеров, равные веса, без комиссий): "
        f"{ui.fmt_signed(buyhold)} · бот {ui.fmt_signed(bot_return)} · "
        f"разница {ui.fmt_signed(bot_return - buyhold)}"
    )
    return buyhold


def _model_info(config: Config, ticker: str) -> dict | None:
    """Данные артефакта модели: вид, порог, dir_acc, возраст; None — модели нет."""
    path = artifact_path(config.models_dir, ticker, config.candle_interval, config.forecast_horizon)
    if not path.exists():
        return None
    kind, threshold, dir_acc = "--", None, None
    metrics_path = path.with_name(f"metrics_{ticker}_{config.candle_interval}_h{config.forecast_horizon}.json")
    try:
        data = json.loads(metrics_path.read_text(encoding="utf-8"))
        kind = str(data.get("best", "--"))
        threshold = float(data.get("threshold", 0.0))
        acc = (data.get("metrics", {}).get("directional_acc", {}) or {}).get(kind)
        if acc is not None:
            dir_acc = float(acc)
    except (OSError, ValueError, TypeError):
        pass  # артефакт есть, метрики не читаются — покажем хотя бы возраст
    age_days = (dt.datetime.now() - dt.datetime.fromtimestamp(path.stat().st_mtime)).days
    return {
        "ticker": ticker,
        "kind": kind,
        "threshold": threshold,
        "dir_acc": dir_acc,
        "age_days": age_days,
    }


def _model_row(config: Config, ticker: str) -> list[str] | None:
    """Строка таблицы моделей: тип, порог, dir_acc, возраст артефакта; None — модели нет."""
    info = _model_info(config, ticker)
    if info is None:
        return None
    threshold = "--" if info["threshold"] is None else f"{info['threshold']:.4f}"
    dir_acc = "--" if info["dir_acc"] is None else f"{info['dir_acc']:.1%}"
    age_days = info["age_days"]
    age = "сегодня" if age_days == 0 else f"{age_days} дн."
    return [ticker, info["kind"], threshold, dir_acc, age]


def cmd_status(config: Config, json_output: bool = False) -> int:
    """Офлайн-сводка без обращения к API: конфиг, покрытие данных, модели, живость."""
    if json_output:
        print(json.dumps(_json_safe(_status_payload(config)), ensure_ascii=False, indent=2, allow_nan=False))
        return 0

    print(ui.header(f"СТАТУС · режим {config.mode} · v{__VERSION__}"))

    print(ui.header("КОНФИГУРАЦИЯ"))
    exposure = f"{config.max_total_exposure_pct:.0%}" if config.max_total_exposure_pct is not None else "без лимита"
    print(
        f"  Профиль капитала: {ui.paint(config.capital_profile, ui.BOLD)} · "
        f"бюджет {ui.fmt_money(config.budget_rub)} руб\n"
        f"  Позиция ≤ {config.max_position_pct:.0%} · экспозиция ≤ {exposure} · "
        f"stop {config.stop_loss_pct:.0%} / take {config.take_profit_pct:.0%}\n"
        f"  Порог входа ≥ {config.min_abs_return:.2%} · таймфрейм {config.candle_interval} · "
        f"горизонт {config.forecast_horizon} бар.\n"
        f"  Тикеров: {len(config.tickers)}"
    )

    print(ui.header(f"ДАННЫЕ · {config.db_path}"))
    conn = connect(config.db_path)
    coverage = conn.execute(
        "SELECT ticker, COUNT(*), MIN(time), MAX(time) FROM candles "
        "WHERE interval = ? AND ticker != '' GROUP BY ticker ORDER BY ticker",
        (config.candle_interval,),
    ).fetchall()
    orphan = conn.execute(
        "SELECT COUNT(*) FROM candles WHERE interval = ? AND ticker = ''",
        (config.candle_interval,),
    ).fetchone()[0]
    news_count = conn.execute("SELECT COUNT(*) FROM news").fetchone()[0]
    conn.close()
    if coverage:
        rows = [[t, f"{n}", str(lo)[:16].replace("T", " "), str(hi)[:16].replace("T", " ")] for t, n, lo, hi in coverage]
        print(ui.render_table(["тикер", "свечей", "первая", "последняя"], rows, aligns=["l", "r", "l", "l"]))
    else:
        print(f"  Свечей нет — выполните `python cli.py download`")
    if orphan:
        print(f"  (+{orphan} свечей без тикера — старая БД, не мешают)")
    print(f"  Новостей в БД: {news_count}" + ("" if news_count else " — выполните `python cli.py news`"))
    missing_data = [t for t in config.tickers if t not in {row[0] for row in coverage}]
    if missing_data:
        warn(f"нет свечей для: {', '.join(missing_data)} — выполните `python cli.py download`")

    print(ui.header(f"МОДЕЛИ · {config.models_dir}"))
    model_rows, missing_models = [], []
    for ticker in config.tickers:
        row = _model_row(config, ticker)
        if row is None:
            missing_models.append(ticker)
        else:
            model_rows.append(row)
    if model_rows:
        print(ui.render_table(
            ["тикер", "модель", "порог", "dir_acc", "возраст"], model_rows, aligns=["l", "l", "r", "r", "r"],
        ))
    if missing_models:
        warn(f"нет моделей для: {', '.join(missing_models)} — выполните `python cli.py train`")
    if not model_rows and not missing_models:
        print("  Тикеры не заданы (TICKERS в .env)")

    print(ui.header("ЖИВОСТЬ"))
    last, source = _last_activity(config.reports_dir)
    if last is not None:
        age_min = (dt.datetime.now(dt.timezone.utc) - last).total_seconds() / 60
        print(f"  {source}: {age_min:.0f} мин назад ({last.isoformat(timespec='seconds')})")
    else:
        print("  Отметок живости нет — бот ещё не запускался (reports/heartbeat, equity_live.csv)")
    journal = Path(config.reports_dir) / "journal.csv"
    if journal.exists():
        with open(journal, encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        if rows:
            print(f"  Сделок в журнале: {len(rows)}, последняя: {rows[-1]['time']} {rows[-1]['action']} {rows[-1]['ticker']}")
    else:
        print("  Журнала сделок нет — появится после первого запуска бота")
    print(f"  Запуск бота: python cli.py run · состояние счёта: python cli.py report")
    return 0


def _last_activity(reports_dir: Path) -> tuple[dt.datetime | None, str]:
    """Самая свежая отметка живости: heartbeat или последняя точка equity_live.csv."""
    stamps: list[tuple[dt.datetime, str]] = []
    heartbeat = Path(reports_dir) / "heartbeat"
    if heartbeat.exists():
        try:
            stamps.append(
                (dt.datetime.fromisoformat(heartbeat.read_text(encoding="utf-8").strip()), "heartbeat")
            )
        except ValueError:
            pass
    equity = Path(reports_dir) / "equity_live.csv"
    if equity.exists():
        try:
            with open(equity, encoding="utf-8") as f:
                rows = list(csv.reader(f))
            for row in reversed(rows[1:]):  # последняя строка с данными
                if row and row[0]:
                    stamps.append(
                        (pd.to_datetime(row[0], utc=True).to_pydatetime(), "equity_live.csv")
                    )
                    break
        except (ValueError, OSError):
            pass
    if not stamps:
        return None, ""
    return max(stamps, key=lambda item: item[0])


def _status_payload(config: Config) -> dict:
    """Машиночитаемая сводка `status --json` (F-C4.10): те же данные, что и таблица."""
    models = []
    for ticker in config.tickers:
        info = _model_info(config, ticker)
        if info is not None:
            models.append({"ticker": ticker, "kind": info["kind"], "dir_acc": info["dir_acc"]})
    last, _source = _last_activity(config.reports_dir)
    heartbeat_age_min = None
    if last is not None:
        heartbeat_age_min = (dt.datetime.now(dt.timezone.utc) - last).total_seconds() / 60
    last_trade = None
    journal = Path(config.reports_dir) / "journal.csv"
    if journal.exists():
        with open(journal, encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        if rows:
            last_trade = dict(rows[-1])
    return {
        "mode": config.mode,
        "capital_profile": config.capital_profile,
        "budget_rub": config.budget_rub,
        "tickers": list(config.tickers),
        "models": models,
        "heartbeat_age_min": heartbeat_age_min,
        "last_trade": last_trade,
    }


def cmd_watchdog(config: Config, max_stale_min: int, restart: bool) -> int:
    """Живость бота: в открытую сессию heartbeat/equity должны обновляться каждый цикл.

    Регрессия 07.09: ошибка внутри итерации не убивала процесс — бот «тихо умирал»,
    equity замолкал, systemd ничего не перезапускал. Watchdog запускается cron'ом:
    при устаревших отметках шлёт письмо (не чаще раза в час) и по --restart
    перезапускает юнит tbank-bot.
    """
    from bot import SessionCalendar
    from notify import Notifier

    reports = Path(config.reports_dir)
    # F-C4.6: диск — единственный ресурс, который watchdog не видел; порог 500 МБ
    disk_warning = None
    try:
        free_mb = shutil.disk_usage(reports).free / (1024 * 1024)
    except OSError:
        free_mb = None
    if free_mb is not None and free_mb < 500:
        disk_warning = f"мало свободного места: {free_mb:.0f} МБ в {reports} (порог 500 МБ)"
        warn(disk_warning)

    in_session = SessionCalendar(make_api(config)).active()
    if not in_session:
        print("Вне торговой сессии MOEX — живость не проверяется (и это нормально)")
        return 0

    now = dt.datetime.now(dt.timezone.utc)
    last, source = _last_activity(reports)
    if last is not None:
        stale_min = (now - last).total_seconds() / 60
    else:
        stale_min = float("inf")

    if stale_min <= max_stale_min:
        print(f"OK: {source} обновлён {stale_min:.1f} мин назад (порог {max_stale_min} мин)")
        return 0

    note = (
        f"нет ни heartbeat, ни equity_live.csv в {reports}"
        if last is None
        else f"{source} молчит {stale_min:.0f} мин (порог {max_stale_min})"
    )
    body = (
        f"Бот не подаёт признаков жизни во время открытой сессии MOEX.\n{note}.\n"
        f"Время проверки: {now.isoformat(timespec='seconds')}.\n"
    )
    if disk_warning:
        body += f"{disk_warning}\n"
    if restart:
        import subprocess

        try:
            proc = subprocess.run(
                ["systemctl", "restart", config.systemd_unit], capture_output=True, text=True
            )
        except FileNotFoundError:
            print(
                "[!!] systemctl не найден — перезапуск невозможен; "
                "запустите юнит вручную или проверьте окружение",
                file=sys.stderr,
            )
            return 1
        if proc.returncode != 0:
            detail = (proc.stderr or "").strip()[:200] or f"код {proc.returncode}"
            print(
                f"[!!] systemctl restart {config.systemd_unit} не удался: {detail}",
                file=sys.stderr,
            )
            return 1
        outcome = "ок"
        body += f"systemctl restart {config.systemd_unit}: {outcome}\n"
        print(f"Перезапуск {config.systemd_unit}: {outcome}")

    notifier = Notifier.from_config(config)
    marker = reports / "watchdog_last_alert"
    alerted_recently = False
    if marker.exists():
        try:
            alerted_recently = dt.datetime.fromisoformat(
                marker.read_text(encoding="utf-8").strip()
            ) > now - dt.timedelta(hours=1)
        except ValueError:
            alerted_recently = False
    if not alerted_recently and notifier.send("бот завис (watchdog)", body):
        marker.write_text(now.isoformat(timespec="seconds"), encoding="utf-8")
    warn(f"живость: {note}")
    return 1


def compose_digest(config: Config) -> str:
    """Текст письма-дайджеста: капитал, P&L по периодам, позиции, последние сделки."""
    api = make_api(config)
    accounts = api.get_accounts()
    if not accounts:
        return "Счетов нет (в sandbox выполните: python cli.py smoke)"
    account_id = _pick_account(config, accounts)
    portfolio = api.get_portfolio(account_id)
    total = portfolio["total_amount_rub"]
    flows = _load_flows(config.reports_dir)
    invested = _net_invested(_sandbox_budget(config), flows)
    pnl = total - invested
    pnl_pct = pnl / invested if invested > 1.0 else 0.0
    lines = [
        f"Счёт {account_id} · режим {config.mode}",
        f"Капитал: {total:,.0f} руб · свободно: {portfolio['cash_rub']:,.0f} руб",
        f"Вложено: {invested:,.0f} руб · P&L: {pnl:+,.0f} руб ({pnl_pct:+.1%})",
    ]
    base_warning = _budget_base_warning(config)
    if base_warning:
        lines.append(f"ВНИМАНИЕ: {base_warning}")

    equity_file = Path(config.reports_dir) / "equity_live.csv"
    equity = pd.DataFrame()
    if equity_file.exists():
        equity = pd.read_csv(equity_file)
        equity["time"] = pd.to_datetime(equity["time"], utc=True)
    if not equity.empty:
        first = equity["time"].iloc[0]
        now = equity["time"].iloc[-1]
        coverage_days = max(1, round((now - first).total_seconds() / 86400))
        lines.append(f"Данные за {coverage_days} дн. (с {first:%d.%m.%Y})")
        for label, days in (("день", 1), ("неделя", 7)):
            res = _period_pnl(equity, flows, now - pd.Timedelta(days=days))
            if res is None:
                continue
            pnl_p, base, moved, coverage = res
            lines.append(f"P&L {label}: {pnl_p:+,.0f} руб от базы {base:,.0f}")

    if portfolio["positions"]:
        figi_ticker = {}
        try:
            figi_ticker = {
                inst["figi"]: t for t, inst in api.resolve_instruments(config.tickers).items()
            }
        except Exception:
            pass
        lines.append("")
        lines.append("Позиции:")
        for figi, pos in portfolio["positions"].items():
            avg, cur = pos["average_position_price"], pos["current_price"]
            pnl_pos = (cur / avg - 1) if avg > 0 and cur > 0 else 0.0
            lines.append(
                f"  {figi_ticker.get(figi, figi)}: {pos['quantity']:.0f} шт × {avg:.2f} → {cur:.2f} ({pnl_pos:+.1%})"
            )
    else:
        lines.append("Открытых позиций нет")

    journal = Path(config.reports_dir) / "journal.csv"
    if journal.exists():
        with open(journal, encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        lines.append("")
        lines.append(f"Последние сделки (всего {len(rows)}):")
        for row in rows[-5:]:
            lines.append(
                f"  {row['time']} {row['action']} {row['ticker']} {row['lots']} лот. × {row['price']} — {row['reason']}"
            )
    return "\n".join(lines)


def cmd_digest(config: Config) -> int:
    """Письмо-дайджест состояния счёта (для cron после закрытия сессии)."""
    from notify import Notifier

    body = compose_digest(config)
    notifier = Notifier.from_config(config)
    if not notifier.enabled:
        print(body)
        warn("SMTP не настроен (NOTIFY_EMAIL_TO, SMTP_HOST/USER/PASSWORD) — письмо не отправлено, текст выше")
        return 1
    today = dt.datetime.now(dt.timezone.utc).date()
    if notifier.send(f"дайджест {today:%Y-%m-%d}", body):
        ok("Дайджест отправлен")
        return 0
    warn("Не удалось отправить дайджест")
    return 1


def cmd_send_test_email(config: Config) -> int:
    """Тестовое письмо: проверка SMTP-настроек без запуска бота."""
    from notify import Notifier

    notifier = Notifier.from_config(config)
    if not notifier.enabled:
        warn("SMTP не настроен — заполните NOTIFY_EMAIL_TO и SMTP_HOST/USER/PASSWORD в .env")
        return 2
    body = (
        f"Тестовое письмо tbank-bot v{__VERSION__}.\n"
        f"Режим: {config.mode}; тикеров: {len(config.tickers)}; таймфрейм: {config.candle_interval}.\n"
        f"Если письмо пришло — уведомления о сделках, watchdog и дайджесты будут доставляться."
    )
    if notifier.send("тест уведомлений", body):
        ok(f"Тестовое письмо отправлено на {notifier.cfg.to_addr}")
        return 0
    warn("Письмо не ушло — проверьте SMTP_HOST/PORT (465=SSL, 587=STARTTLS), SMTP_USER/пароль приложения")
    return 1


def cmd_normalize(config: Config) -> int:
    """Проверяет соответствие счёта бюджету SANDBOX_INITIAL_RUB.

    Дефицит (просадку ниже бюджета) сознательно не восполняет. Излишек вывести
    нельзя: в T-Invest API нет метода вывода из песочницы (SandboxPayOut не
    существует — сервер отвечает 404). Вернуть бюджет можно только полным
    сбросом счёта: python cli.py reset-sandbox.
    """
    if config.mode != "sandbox":
        print("[!!] Команда normalize доступна только в режиме sandbox", file=sys.stderr)
        return 2
    api = make_api(config)
    accounts = api.get_accounts()
    if not accounts:
        print("[!!] Sandbox-счёт не найден — нечего нормализовать")
        return 2
    from tbank.trader import Trader

    trader = Trader(api, Path(config.reports_dir) / "journal.csv", account_id=getattr(config, "account_id", ""))
    portfolio = trader.portfolio()
    total = portfolio["total_amount_rub"]
    budget = _sandbox_budget(config)
    excess = total - budget
    print(
        f"  Счёт {trader.account_id}: {ui.fmt_money(total)} руб "
        f"(кэш {ui.fmt_money(portfolio['cash_rub'])}, бюджет {ui.fmt_money(budget)})"
    )
    if excess > 1.0:
        warn(
            f"излишек {ui.fmt_money(excess)} руб. Вывести его через API нельзя — "
            "в T-Invest API нет метода вывода из sandbox. Вернуть бюджет можно "
            "только пересозданием счёта: python cli.py reset-sandbox"
        )
        return 1
    if excess < -1.0:
        print("  На счёте ниже бюджета (просадка) — дефицит сознательно не восполняем")
    else:
        print("  Счёт в пределах бюджета ±1 руб — ничего не делаем")
    return 0


def _rotate_report_file(path: Path) -> str | None:
    """Убирает журнал старого счёта в архив с меткой времени, возвращает имя архива."""
    if not path.exists():
        return None
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archived = path.with_name(f"{path.stem}_{stamp}{path.suffix}")
    path.replace(archived)
    return archived.name


def _confirm_reset(assume_yes: bool) -> bool | None:
    """Подтверждение сброса: True — да, False — отказ, None — нет интерактивного ввода."""
    if assume_yes:
        return True
    try:
        answer = input("  Подтвердите (YES): ").strip()
    except EOFError:
        print("  Нет интерактивного ввода — передайте --yes для подтверждения", file=sys.stderr)
        return None
    return answer == "YES"


def cmd_reset_sandbox(config: Config, assume_yes: bool = False) -> int:
    """Полный сброс sandbox: закрыть счёт, открыть новый, внести бюджет.

    Единственный способ вернуть счёт к бюджету после ошибочного пополнения:
    вывода из песочницы в API нет. Позиции и кэш закрываются вместе со счётом;
    journal.csv / equity_live.csv / flows.csv ротируются с меткой времени —
    история старого счёта остаётся в reports/ с суффиксом-датой.
    После сброса бота нужно перезапустить, чтобы он подхватил новый счёт.
    """
    if config.mode != "sandbox":
        print("[!!] Команда reset-sandbox доступна только в режиме sandbox", file=sys.stderr)
        return 2
    api = make_api(config)
    accounts = api.get_accounts()
    if not accounts:
        print("[!!] Sandbox-счёт не найден — запустите бота, он откроет и наполнит счёт сам")
        return 2
    from tbank.trader import Trader

    trader = Trader(api, Path(config.reports_dir) / "journal.csv", account_id=getattr(config, "account_id", ""))
    portfolio = trader.portfolio()
    print(
        f"  Счёт {trader.account_id}: {ui.fmt_money(portfolio['total_amount_rub'])} руб, "
        f"позиций: {len(portfolio['positions'])}"
    )
    print(
        f"  Счёт будет ЗАКРЫТ вместе с позициями; откроется новый с бюджетом "
        f"{ui.fmt_money(_sandbox_budget(config))} руб."
    )
    confirmed = _confirm_reset(assume_yes)
    if confirmed is None:
        return 2
    if not confirmed:
        print("  Отменено")
        return 1

    api.close_sandbox_account(trader.account_id)
    new_account_id = api.open_sandbox_account()
    api.pay_in(new_account_id, _sandbox_budget(config))

    rotated = [
        name
        for name in ("journal.csv", "equity_live.csv", "flows.csv")
        if (archived := _rotate_report_file(Path(config.reports_dir) / name))
    ]
    print(f"  Новый счёт: {new_account_id}, бюджет {ui.fmt_money(_sandbox_budget(config))} руб внесён")
    if rotated:
        print(f"  Журналы старого счёта заархивированы: {', '.join(rotated)}")
    print("  Перезапустите бота, чтобы он подхватил новый счёт (например: systemctl restart tbank-bot)")
    return 0


def _check_config_payload(config: Config) -> dict:
    """Машиночитаемая проверка конфигурации; токен маскируется и в вывод не попадает."""
    errors: list[str] = []
    try:
        config.validate()
    except ValueError as exc:
        errors.append(str(exc))
    return {
        "ok": not errors,
        "mode": config.mode,
        "capital_profile": config.capital_profile,
        "budget_rub": config.budget_rub,
        "tickers": list(config.tickers),
        "token": _mask_secret(config.token),
        "risk": {
            "max_position_pct": config.max_position_pct,
            "max_total_exposure_pct": config.max_total_exposure_pct,
            "stop_loss_pct": config.stop_loss_pct,
            "take_profit_pct": config.take_profit_pct,
            "min_abs_return": config.min_abs_return,
            "max_drawdown_pct": config.max_drawdown_pct,
        },
        "errors": errors,
    }


def cmd_check_config(config: Config, json_output: bool = False) -> int:
    """Полная проверка конфигурации: режим, токен, риск-параметры (без API)."""
    if json_output:
        payload = _check_config_payload(config)
        print(json.dumps(_json_safe(payload), ensure_ascii=False, allow_nan=False))
        return 0 if payload["ok"] else 2
    try:
        config.validate()
    except ValueError as exc:
        print(f"[!!] Конфигурация: {exc}", file=sys.stderr)
        return 2
    ok("Конфигурация корректна")
    return 0


def cmd_kill_switch_reset(config: Config) -> int:
    """Снять активный kill-switch: только файл в reports/, API не нужен."""
    from bot import clear_kill_switch

    if clear_kill_switch(Path(config.reports_dir)):
        ok("Kill-switch сброшен — новые входы разрешены после перезапуска бота")
    else:
        warn("Kill-switch не был активен")
    return 0


# команды, которым не нужен токен T-Invest (офлайн или только SMTP)
MODE_ONLY_COMMANDS = {"train", "backtest", "status", "send-test-email", "kill-switch-reset"}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Торговый бот T-Invest API (sandbox) с ML-прогнозом и новостным анализом",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Группы команд:\n"
            "  Первый запуск: setup, smoke, download, news, train, backtest\n"
            "  Торговля:      run, report, kill-switch-reset, normalize, reset-sandbox\n"
            "  Мониторинг:    status, watchdog, digest, send-test-email\n"
            "  Обслуживание:  check-config, --version"
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__VERSION__}")
    parser.add_argument("--no-color", action="store_true", help="отключить цвета в выводе (как NO_COLOR=1)")
    sub = parser.add_subparsers(dest="command", required=True, metavar="command")

    sub.add_parser("smoke", help="проверка токена, счёта и API (открывает и пополняет sandbox-счёт)")

    p = sub.add_parser("download", help="выгрузка истории свечей в БД")
    p.add_argument("--days", type=int, default=None, help="глубина истории в днях (по умолчанию HISTORY_DAYS из .env)")

    sub.add_parser("news", help="выгрузка новостей в БД")

    p = sub.add_parser("train", help="сравнение моделей, обучение и выбор лучшей")
    p.add_argument("--capital", type=float, default=None, help="размер счёта, руб: фильтр тикеров по лоту")

    p = sub.add_parser("backtest", help="бэктест стратегии с лучшей моделью")
    p.add_argument("--capital", type=float, default=None, help="размер счёта, руб (по умолчанию бюджет из .env)")

    p = sub.add_parser("run", help="торговый цикл (sandbox или real по MODE)")
    p.add_argument("--iterations", type=int, default=None, help="число итераций цикла (пробный запуск)")

    p = sub.add_parser("daemon", help="фоновый режим: run | status | stop")
    daemon_sub = p.add_subparsers(dest="daemon_action", required=True, metavar="action")
    p_run = daemon_sub.add_parser("run", help="торговый цикл без UI (lock, статус, очередь команд)")
    p_run.add_argument("--iterations", type=int, default=None, help="число итераций цикла (пробный запуск)")
    daemon_sub.add_parser("status", help="состояние демона из daemon_status.json (офлайн)")
    daemon_sub.add_parser("stop", help="остановить демон через очередь команд (≤60 с)")

    p = sub.add_parser("report", help="состояние счёта: P&L, позиции, сделки, кривая капитала")
    p.add_argument("--json", action="store_true", help="машиночитаемый отчёт одним JSON-объектом")
    p = sub.add_parser("status", help="офлайн-сводка: конфиг, данные, модели, живость (без API)")
    p.add_argument("--json", action="store_true", help="машиночитаемая сводка одним JSON-объектом")

    p = sub.add_parser("check-config", help="проверить конфигурацию: режим, токен, риск-параметры")
    p.add_argument("--json", action="store_true", help="машиночитаемая проверка одним JSON-объектом")

    p = sub.add_parser("watchdog", help="проверка живости бота (для cron)")
    p.add_argument("--max-stale-min", type=int, default=20, help="порог устаревания отметок живости, минут")
    p.add_argument("--restart", action="store_true", help=f"перезапустить systemd-юнит при зависании (имя: SYSTEMD_UNIT)")

    sub.add_parser("digest", help="письмо-дайджест состояния счёта (для cron)")
    sub.add_parser("send-test-email", help="тестовое письмо: проверка SMTP-настроек")
    sub.add_parser("normalize", help="проверить счёт на соответствие бюджету (sandbox)")
    sub.add_parser("kill-switch-reset", help="сбросить kill-switch — разрешить новые входы (локально)")

    p = sub.add_parser("reset-sandbox", help="пересоздать sandbox-счёт с бюджетом")
    p.add_argument("--yes", action="store_true", help="не спрашивать подтверждение")

    p = sub.add_parser("setup", help="первый запуск: smoke → download → news → train → backtest")
    p.add_argument("--days", type=int, default=None, help="глубина истории свечей, дней (по умолчанию HISTORY_DAYS)")
    p.add_argument("--capital", type=float, default=None, help="размер счёта, руб: фильтр тикеров по лоту")
    return parser


def _attach_file_log(config: Config) -> None:
    """Файловый лог с ротацией (LOG_FILE в .env); без него — только stderr/journald."""
    if config.log_file is None:
        return
    try:
        config.log_file.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            config.log_file, maxBytes=config.log_max_bytes,
            backupCount=config.log_backups, encoding="utf-8",
        )
        # в файл — без ANSI-цветов и с полной датой (файл живёт дольше суток)
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
        )
        logging.root.addHandler(handler)
    except OSError as exc:
        print(f"[!!] LOG_FILE {config.log_file}: {exc} — пишу только в консоль", file=sys.stderr)


def main() -> int:
    args = _build_parser().parse_args()
    if args.no_color:
        ui.set_colors(False)
        ui.set_colors(False, sys.stderr)

    config = Config()
    # check-config --json собирает ошибки конфигурации в payload сам; иначе вместо
    # обещанного JSON пользователь получил бы пустой stdout.
    skip_prevalidation = args.command == "check-config" and getattr(args, "json", False)
    try:
        if not skip_prevalidation:
            mode_only = args.command in MODE_ONLY_COMMANDS or (
                args.command == "daemon" and getattr(args, "daemon_action", None) != "run"
            )
            if mode_only:
                if config.mode not in {"sandbox", "real"}:
                    raise ValueError(f"MODE должен быть sandbox|real, получено: {config.mode}")
            else:
                config.validate()
    except ValueError as exc:
        print(f"[!!] Конфигурация: {exc}", file=sys.stderr)
        return 2
    _attach_file_log(config)

    try:
        if args.command == "setup":
            return cmd_setup(config, args.days, args.capital)
        if args.command == "smoke":
            return cmd_smoke(config)
        if args.command == "download":
            return cmd_download(config, args.days or config.history_days)
        if args.command == "news":
            return cmd_news(config)
        if args.command == "train":
            return cmd_train(config, args.capital)
        if args.command == "backtest":
            return cmd_backtest(config, args.capital)
        if args.command == "run":
            return cmd_run(config, args.iterations)
        if args.command == "daemon":
            if args.daemon_action == "run":
                return cmd_daemon_run(config, args.iterations)
            if args.daemon_action == "status":
                return cmd_daemon_status(config)
            if args.daemon_action == "stop":
                return cmd_daemon_stop(config)
            return 1
        if args.command == "report":
            if getattr(args, "json", False):
                return cmd_report(config, json_output=True)
            return cmd_report(config)
        if args.command == "status":
            if getattr(args, "json", False):
                return cmd_status(config, json_output=True)
            return cmd_status(config)
        if args.command == "check-config":
            if getattr(args, "json", False):
                return cmd_check_config(config, json_output=True)
            return cmd_check_config(config)
        if args.command == "kill-switch-reset":
            return cmd_kill_switch_reset(config)
        if args.command == "watchdog":
            return cmd_watchdog(config, args.max_stale_min, args.restart)
        if args.command == "digest":
            return cmd_digest(config)
        if args.command == "send-test-email":
            return cmd_send_test_email(config)
        if args.command == "normalize":
            return cmd_normalize(config)
        if args.command == "reset-sandbox":
            return cmd_reset_sandbox(config, assume_yes=args.yes)
        return 1
    except APIError as exc:
        print(f"[!!] Ошибка API: {exc}", file=sys.stderr)
        return 2
    except requests.RequestException as exc:
        print(f"[!!] Нет соединения с API: {exc}", file=sys.stderr)
        return 2
    except RuntimeError as exc:
        print(f"[!!] {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        # последний рубеж: пользователь видит одну строку; traceback — только в файл лога
        print(f"[!!] Непредвиденная ошибка: {type(exc).__name__}: {exc}", file=sys.stderr)
        log_file = getattr(config, "log_file", None)
        if log_file is not None:
            print(f"Подробности в логе: {log_file}", file=sys.stderr)
            log.exception("Непредвиденная ошибка")
        else:
            print("лог не настроен (LOG_FILE)", file=sys.stderr)
            log.error("Непредвиденная ошибка: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

"""CLI торгового бота: download / news / train / backtest / run / report / normalize / smoke.

Примеры:
    python cli.py smoke                 # проверка токена, счёта и API
    python cli.py download --days 720   # выгрузка истории свечей
    python cli.py news                  # выгрузка новостей
    python cli.py train                 # сравнение моделей, выбор лучшей
    python cli.py train --capital 30000 # то же, но с фильтром тикеров по лоту под капитал
    python cli.py backtest              # бэктест стратегии с лучшей моделью
    python cli.py backtest --capital 30000  # бэктест на счёте 30 тыс. руб
    python cli.py run                   # торговый цикл в sandbox
    python cli.py report                # состояние счёта и журнал сделок
    python cli.py watchdog --restart    # проверка живости бота (для cron)
    python cli.py digest                # письмо-дайджест состояния (для cron)
    python cli.py normalize             # проверить счёт на соответствие бюджету
    python cli.py reset-sandbox         # пересоздать sandbox-счёт с бюджетом
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import logging
import sys
from dataclasses import replace
from pathlib import Path

import pandas as pd

import ui
from backtest import run_backtest, save_report
from config import CAPITAL_PROFILES, Config, resolve_capital_profile
from features import build_features, ensure_news_columns
from models.registry import (
    artifact_path,
    evaluate_all,
    load_artifact,
    model_factories,
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
from tbank.rest import TBankRestClient


def _sandbox_budget(config) -> float:
    """Бюджет песочницы: ACCOUNT_BUDGET_RUB, если задан, иначе SANDBOX_INITIAL_RUB."""
    return getattr(config, "account_budget_rub", None) or config.sandbox_initial_rub


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
    print(f"{ui.paint('[!!]', ui.BRIGHT_RED)} {ui.paint(message, ui.YELLOW)}")


def make_api(config: Config) -> TBankAPI:
    return TBankAPI(
        TBankRestClient(config.token, config.mode),
        order_market_fallback=config.order_fallback_to_market,
    )


def cmd_smoke(config: Config) -> int:
    print(ui.header(f"SMOKE · режим {config.mode}"))
    api = make_api(config)
    accounts = api.get_accounts()
    ok(f"Счетов: {len(accounts)}")
    for acc in accounts[:3]:
        print(f"     - {acc.get('id')} ({acc.get('type')})")
    if config.mode == "sandbox":
        if not accounts:
            account_id = api.open_sandbox_account()
            ok(f"Открыт sandbox-счёт: {account_id}")
            api.pay_in(account_id, 1_000_000)
            ok("Пополнен на 1 000 000 руб")
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

    conn.close()
    print()
    ok("Smoke-тест пройден")
    return 0


def cmd_download(config: Config, days: int) -> int:
    api = make_api(config)
    instruments = api.resolve_instruments(config.tickers)
    conn = connect(config.db_path)
    store_instruments(conn, instruments)  # лоты/uid для офлайн-команд (backtest)
    for ticker, instrument in instruments.items():
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


def _render_metrics_table(metrics: pd.DataFrame, best: str) -> str:
    """Таблица сравнения моделей: лучшая строка выделена, sharpe/net окрашены по знаку."""

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
                f"{int(m['n_points']):d}",
            ]
        )

    def cell_paint(r: int, c: int, text: str) -> str:
        name = metrics.index[r]
        if name == best:
            return ui.paint(text, ui.BRIGHT_GREEN, ui.BOLD)
        if c in (5, 6):  # колонки sharpe / net sharpe
            v = metrics.iloc[r][["strategy_sharpe", "strategy_sharpe_net"][c - 5]]
            if pd.isna(v):
                return text
            if v > 0:
                return ui.paint(text, ui.GREEN)
            if v < 0:
                return ui.paint(text, ui.RED)
            return ui.paint(text, ui.DIM)
        return text

    return ui.render_table(
        ["", "модель", "rmse", "mae", "dir_acc", "sharpe", "net", "точек"],
        rows,
        aligns=["c", "l", "r", "r", "r", "r", "r", "r"],
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
    for ticker in config.tickers:
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
        metrics, _ = evaluate_all(df, config.forecast_horizon, round_trip_cost=round_trip)
        # таблицу печатаем до/после логов train_and_save; выбор там — по net sharpe
        print(_render_metrics_table(metrics, metrics["strategy_sharpe_net"].idxmax()))
        try:
            artifact = train_and_save(
                df, config.forecast_horizon, config.models_dir, ticker, config.candle_interval,
                cost_floor=2 * (config.commission_pct + config.slippage_pct),
            )
        except ValueError as exc:
            warn(f"{ticker}: {exc} — тикер пропущен, старый артефакт (если был) не тронут")
            continue
        trained.append(ticker)
        summary[ticker] = {"best": artifact.kind, **artifact.metrics}
        best = ui.paint(artifact.kind, ui.BRIGHT_GREEN, ui.BOLD)
        sharpe = ui.fmt_signed(artifact.metrics.get("strategy_sharpe_net", 0.0), "{:+.2f}")
        print(
            f"  Лучшая модель: {best} (net sharpe {sharpe}) · "
            f"порог входа {ui.paint(f'{artifact.threshold:.4f}', ui.CYAN)}\n"
        )
    conn.close()
    if summary:
        payload: dict = {"tickers": summary}
        if capital:
            payload |= {"capital_rub": capital, "profile": capital_profile}
        config.reports_dir.mkdir(parents=True, exist_ok=True)
        (config.reports_dir / "train_summary.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(
            f"Итого обучено: {len(summary)}/{len(config.tickers)}. "
            f"Артефакты: {ui.paint(str(config.models_dir), ui.CYAN)}, "
            f"сводка: {ui.paint(str(config.reports_dir / 'train_summary.json'), ui.CYAN)}"
        )
    return 1 if not summary else 0


def cmd_backtest(config: Config, days: int | None = None, capital: float | None = None) -> int:
    conn = connect(config.db_path)
    # капитал бэктеста: --capital или 1 млн по умолчанию (как в run_backtest)
    initial_cash = capital if capital else 1_000_000.0
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
    from bot import ensure_sentiments
    from nlp.agenda import sentiment_series
    from nlp.sentiment import make_sentiment

    scorer = make_sentiment(config.sentiment_model, preference=config.nlp_sentiment)
    model_key = "lexicon" if getattr(scorer, "name", "") == "lexicon" else f"transformers:{config.sentiment_model}"

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
        test_points = features.index[-n_test_points(len(features)):]
        if artifact.kind == "lgbm":
            preds = walk_forward_lgbm(features, test_points, config.forecast_horizon)
        else:
            preds = walk_forward_baselines(
                df["close"], features["target"], config.forecast_horizon,
                model_factories(config.forecast_horizon)[artifact.kind], test_points, refit_every=48,
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
    return 0


def cmd_run(config: Config, max_iterations: int | None) -> int:
    from bot import TradingBot

    # артефакты обязательны: без модели бот не может прогнозировать
    missing = [
        t for t in config.tickers
        if not artifact_path(config.models_dir, t, config.candle_interval, config.forecast_horizon).exists()
    ]
    if missing:
        warn(f"Нет артефактов моделей для: {', '.join(missing)} — выполните `python cli.py train`")
    bot = TradingBot(config)
    for ticker in missing:
        bot.instruments.pop(ticker, None)
    if not bot.instruments:
        warn("Ни одного тикера с моделью — запуск невозможен. Сначала: python cli.py train")
        return 2
    bot.run_forever(max_iterations=max_iterations)
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


def cmd_report(config: Config) -> int:
    api = make_api(config)
    accounts = api.get_accounts()
    if not accounts:
        warn("Счетов нет (в sandbox выполните: python cli.py smoke)")
        return 1
    account_id = accounts[0]["id"]
    portfolio = api.get_portfolio(account_id)
    total = portfolio["total_amount_rub"]
    initial = _sandbox_budget(config)
    flows = _load_flows(config.reports_dir)
    invested = _net_invested(initial, flows)
    pnl = total - invested

    figi_ticker: dict[str, str] = {}
    lot_by_ticker: dict[str, int] = {}
    try:
        instruments = api.resolve_instruments(config.tickers)
        for t, inst in instruments.items():
            figi_ticker[inst["figi"]] = t
            lot_by_ticker[t] = int(inst.get("lot", 1))
    except Exception as exc:
        warn(f"Не удалось разрешить тикеры ({exc}) — позиции покажем по FIGI, лот=1")

    print(ui.header(f"СЧЁТ {account_id} · {config.mode}"))
    print(
        f"  Капитал:  {ui.paint(ui.fmt_money(total) + ' руб', ui.BOLD)} | "
        f"свободно: {ui.fmt_money(portfolio['cash_rub'])} руб"
    )
    print(
        f"  Вложено:  {ui.fmt_money(invested)} руб "
        f"(бюджет {ui.fmt_money(initial)} ± пополнения/выводы из flows.csv)"
    )
    pnl_pct = pnl / invested if invested > 1.0 else 0.0
    print(
        f"  P&L:      {ui.fmt_signed(pnl, '{:+,.0f} руб')} "
        f"({ui.fmt_signed(pnl_pct)} от вложенного) — пополнения в прибыль не считают"
    )
    if total - invested > max(1000.0, invested * 0.02):
        warn(
            f"капитал выше вложенного на {ui.fmt_money(total - invested)} руб, "
            "но в flows.csv нет записей об этом — похоже, было неучтённое пополнение. "
            "Выполните `python cli.py normalize`: излишек будет выведен и внесён в учёт."
        )

    print(ui.header("P&L ПО ПЕРИОДАМ (без учёта пополнений/выводов)"))
    equity_file = config.reports_dir / "equity_live.csv"
    equity = None
    if equity_file.exists():
        equity = pd.read_csv(equity_file)
        equity["time"] = pd.to_datetime(equity["time"], utc=True)
        now = equity["time"].iloc[-1]
        for label, days in (("день", 1), ("неделя", 7), ("месяц", 30)):
            res = _period_pnl(equity, flows, now - pd.Timedelta(days=days))
            if res is None:
                print(f"  {label:<7} нет данных за окно")
                continue
            pnl_p, base, moved, coverage = res
            pct_p = pnl_p / base if base > 1.0 else 0.0
            note = ""
            if coverage < days * 0.98:
                note += f" (данных {coverage:.1f} дн. из {days})"
            if abs(moved) > 1.0:
                note += f" [движение денег в окне: {moved:+,.0f} руб]"
            print(
                f"  {label:<7} {ui.fmt_signed(pnl_p, '{:+,.0f} руб')} "
                f"({ui.fmt_signed(pct_p)} от {ui.fmt_money(base)}){note}"
            )
    else:
        print("  Появится после запуска бота (reports/equity_live.csv).")

    print(ui.header("ПОЗИЦИИ"))
    if not portfolio["positions"]:
        print("  (нет открытых позиций)")
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
            lots_str = f"{pos['quantity'] / lot:.0f}" if lot else "--"
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

        print(ui.render_table(
            ["инструмент", "кол-во", "лотов", "ср. цена", "текущая", "PnL"],
            rows,
            aligns=["l", "r", "r", "r", "r", "r"],
        ))

    print(ui.header("СДЕЛКИ"))
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
        print(f"  Всего {len(rows)} операций: {ui.paint(f'покупок {buys}', ui.GREEN)}, "
              f"{ui.paint(f'продаж {sells}', ui.RED)} · "
              f"комиссия (оценка, тариф {config.commission_pct:.2%}): ~{total_commission:,.0f} руб")
        print("  Последние 10:")
        for row in rows[-10:]:
            action_color = ui.GREEN if row["action"] == "BUY" else ui.RED
            action = ui.paint(f"{row['action']:<4}", action_color)
            print(f"    {row['time']}  {action} {row['ticker']:<5} {row['lots']} лот(ов) "
                  f"по {row['price']} · комиссия ~{commission_of(row):,.0f} руб · {row['reason']}")
    else:
        print("  Журнала сделок ещё нет — появится после первого запуска бота.")

    print(ui.header("КРИВАЯ КАПИТАЛА (живой прогон)"))
    if equity is not None:
        print(f"  Точек: {len(equity)} (файл {equity_file.name})")
        print(
            f"  старт:  {equity.iloc[0]['time']} · {ui.fmt_money(equity.iloc[0]['total_rub'])} руб"
        )
        last_equity = float(equity.iloc[-1]["total_rub"])
        vs_invested = last_equity / invested - 1.0 if invested > 1.0 else 0.0
        print(
            f"  сейчас: {equity.iloc[-1]['time']} · "
            f"{ui.fmt_signed(vs_invested)} к вложенному · {ui.fmt_money(last_equity)} руб"
        )
        if len(equity) > 2:
            peak = equity["total_rub"].max()
            print(
                f"  максимум: {ui.fmt_money(peak)} руб | минимум: {ui.fmt_money(equity['total_rub'].min())} руб"
            )
        _print_benchmark(config, equity, vs_invested)
    else:
        print("  Пока нет данных — появится после запуска бота (reports/equity_live.csv).")
    return 0


def _print_benchmark(config: Config, equity: pd.DataFrame, bot_return: float) -> None:
    """Бенчмарк: buy&hold равных весов тех же тикеров за период живой кривой (локальная БД свечей)."""
    from tbank.market_data import connect as db_connect

    try:
        conn = db_connect(config.db_path)
    except Exception as exc:
        print(f"  Бенчмарк недоступен (БД: {exc})")
        return
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
        print("  Бенчмарк: нет свечей в локальной БД за период (выполните download)")
        return
    buyhold = sum(returns) / len(returns)
    print(
        f"  vs buy&hold ({len(returns)} тикеров, равные веса, без комиссий): "
        f"{ui.fmt_signed(buyhold)} · бот {ui.fmt_signed(bot_return)} · "
        f"разница {ui.fmt_signed(bot_return - buyhold)}"
    )


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
    if restart:
        import subprocess

        proc = subprocess.run(["systemctl", "restart", "tbank-bot"], capture_output=True, text=True)
        outcome = "ок" if proc.returncode == 0 else f"ошибка: {(proc.stderr or '').strip()[:200]}"
        body += f"systemctl restart tbank-bot: {outcome}\n"
        print(f"Перезапуск tbank-bot: {outcome}")

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
    account_id = accounts[0]["id"]
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

    equity_file = Path(config.reports_dir) / "equity_live.csv"
    if equity_file.exists():
        equity = pd.read_csv(equity_file)
        equity["time"] = pd.to_datetime(equity["time"], utc=True)
        now = equity["time"].iloc[-1]
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
    if notifier.send("дайджест дня", body):
        ok("Дайджест отправлен")
        return 0
    warn("Не удалось отправить дайджест")
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

    trader = Trader(api, Path(config.reports_dir) / "journal.csv")
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

    trader = Trader(api, Path(config.reports_dir) / "journal.csv")
    portfolio = trader.portfolio()
    print(
        f"  Счёт {trader.account_id}: {ui.fmt_money(portfolio['total_amount_rub'])} руб, "
        f"позиций: {len(portfolio['positions'])}"
    )
    print(
        f"  Счёт будет ЗАКРЫТ вместе с позициями; откроется новый с бюджетом "
        f"{ui.fmt_money(_sandbox_budget(config))} руб."
    )
    if not assume_yes:
        answer = input("  Подтвердите (YES): ").strip()
        if answer != "YES":
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


OFFLINE_COMMANDS = {"train", "backtest"}


def main() -> int:
    parser = argparse.ArgumentParser(description="Торговый бот T-Invest API (sandbox) с ML-прогнозом и новостным анализом")
    parser.add_argument(
        "command",
        choices=[
            "smoke", "download", "news", "train", "backtest", "run",
            "report", "watchdog", "digest", "normalize", "reset-sandbox",
        ],
    )
    parser.add_argument("--days", type=int, default=None, help="глубина истории в днях (download)")
    parser.add_argument("--iterations", type=int, default=None, help="число итераций цикла (run)")
    parser.add_argument("--yes", action="store_true", help="не спрашивать подтверждение (reset-sandbox)")
    parser.add_argument(
        "--max-stale-min", type=int, default=20,
        help="порог устаревания отметок живости, минут (watchdog)",
    )
    parser.add_argument(
        "--capital", type=float, default=None,
        help="размер счёта, руб: фильтр тикеров по лоту и бэктест на этой сумме (train, backtest)",
    )
    parser.add_argument(
        "--restart", action="store_true",
        help="перезапустить systemd-юнит tbank-bot при зависании (watchdog)",
    )
    args = parser.parse_args()

    config = Config()
    try:
        if args.command in OFFLINE_COMMANDS:
            if config.mode not in {"sandbox", "real"}:
                raise ValueError(f"MODE должен быть sandbox|real, получено: {config.mode}")
        else:
            config.validate()
    except ValueError as exc:
        print(f"[!!] Конфигурация: {exc}", file=sys.stderr)
        return 2

    if args.command == "smoke":
        return cmd_smoke(config)
    if args.command == "download":
        return cmd_download(config, args.days or config.history_days)
    if args.command == "news":
        return cmd_news(config)
    if args.command == "train":
        return cmd_train(config, args.capital)
    if args.command == "backtest":
        return cmd_backtest(config, args.days, args.capital)
    if args.command == "run":
        return cmd_run(config, args.iterations)
    if args.command == "report":
        return cmd_report(config)
    if args.command == "watchdog":
        return cmd_watchdog(config, args.max_stale_min, args.restart)
    if args.command == "digest":
        return cmd_digest(config)
    if args.command == "normalize":
        return cmd_normalize(config)
    if args.command == "reset-sandbox":
        return cmd_reset_sandbox(config, assume_yes=args.yes)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

"""CLI торгового бота: download / news / train / backtest / run / report / smoke.

Примеры:
    python cli.py smoke                 # проверка токена, счёта и API
    python cli.py download --days 720   # выгрузка истории свечей
    python cli.py news                  # выгрузка новостей
    python cli.py train                 # сравнение моделей, выбор лучшей
    python cli.py backtest              # бэктест стратегии с лучшей моделью
    python cli.py run                   # торговый цикл в sandbox
    python cli.py report                # состояние счёта и журнал сделок
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import sys
from dataclasses import replace
from pathlib import Path

import pandas as pd

import ui
from backtest import run_backtest, save_report
from config import Config
from features import build_features, ensure_news_columns
from models.baseline import ARIMAReturn, ETSReturn, MovingAverageReturn, NaiveZero, PersistenceReturn
from models.registry import (
    evaluate_all,
    load_artifact,
    train_and_save,
    walk_forward_baselines,
    walk_forward_lgbm,
)
from strategy import RiskConfig
from tbank.api import TBankAPI
from tbank.market_data import connect, download_candles, load_candles, load_news, store_news
from tbank.rest import TBankRestClient


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
    return TBankAPI(TBankRestClient(config.token, config.mode))


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
    """Таблица сравнения моделей: лучшая строка выделена, sharpe окрашен по знаку."""

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
                f"{int(m['n_points']):d}",
            ]
        )

    def cell_paint(r: int, c: int, text: str) -> str:
        name = metrics.index[r]
        if name == best:
            return ui.paint(text, ui.BRIGHT_GREEN, ui.BOLD)
        if c == 5:  # колонка sharpe
            v = metrics.iloc[r]["strategy_sharpe"]
            if pd.isna(v):
                return text
            if v > 0:
                return ui.paint(text, ui.GREEN)
            if v < 0:
                return ui.paint(text, ui.RED)
            return ui.paint(text, ui.DIM)
        return text

    return ui.render_table(
        ["", "модель", "rmse", "mae", "dir_acc", "sharpe", "точек"],
        rows,
        aligns=["c", "l", "r", "r", "r", "r", "r"],
        paint_cell=cell_paint,
    )


def cmd_train(config: Config) -> int:
    conn = connect(config.db_path)
    summary = {}
    trained = []
    for ticker in config.tickers:
        df = load_candles(conn, ticker, config.candle_interval)
        if df.empty:
            warn(f"{ticker}: нет свечей в {config.db_path} — выполните `python cli.py download`")
            continue
        if len(df) < 500:
            warn(f"{ticker}: всего {len(df)} свечей — мало для обучения (нужно >= 500), пропускаем. "
                 f"Возможно, бумага торгуется недавно.")
            continue
        trained.append(ticker)
        print(ui.header(f"{ticker} · {len(df)} свечей · горизонт {config.forecast_horizon} бар(а)"))
        metrics, _ = evaluate_all(df, config.forecast_horizon)
        # train_and_save выбирает лучшую модель и пишет порог; таблица печатаем до/после логов
        print(_render_metrics_table(metrics.sort_values("strategy_sharpe", ascending=False, na_position="last"), metrics["strategy_sharpe"].idxmax()))
        artifact = train_and_save(
            df, config.forecast_horizon, config.models_dir, ticker, config.candle_interval,
            cost_floor=2 * (config.commission_pct + config.slippage_pct),
        )
        summary[ticker] = {"best": artifact.kind, **artifact.metrics}
        best = ui.paint(artifact.kind, ui.BRIGHT_GREEN, ui.BOLD)
        sharpe = ui.fmt_signed(artifact.metrics.get("strategy_sharpe", 0.0), "{:+.2f}")
        print(
            f"  Лучшая модель: {best} (strategy_sharpe {sharpe}) · "
            f"порог входа {ui.paint(f'{artifact.threshold:.4f}', ui.CYAN)}\n"
        )
    conn.close()
    if summary:
        config.reports_dir.mkdir(parents=True, exist_ok=True)
        (config.reports_dir / "train_summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(
            f"Итого обучено: {len(summary)}/{len(config.tickers)}. "
            f"Артефакты: {ui.paint(str(config.models_dir), ui.CYAN)}, "
            f"сводка: {ui.paint(str(config.reports_dir / 'train_summary.json'), ui.CYAN)}"
        )
    return 1 if not summary else 0


def cmd_backtest(config: Config, days: int | None = None) -> int:
    conn = connect(config.db_path)
    risk = RiskConfig(
        max_position_pct=config.max_position_pct,
        stop_loss_pct=config.stop_loss_pct,
        take_profit_pct=config.take_profit_pct,
        min_abs_return=config.min_abs_return,
        commission_pct=config.commission_pct,
    )
    for ticker in config.tickers:
        df = load_candles(conn, ticker, config.candle_interval)
        if df.empty:
            print(f"[!!] {ticker}: нет свечей в {config.db_path} — выполните `python cli.py download`")
            continue
        artifact = load_artifact(config.models_dir, ticker, config.candle_interval, config.forecast_horizon)
        features = ensure_news_columns(build_features(df, config.forecast_horizon))

        # порог входа как в боте: max(MIN_ABS_RETURN, рекомендация модели)
        eff_risk = replace(
            risk,
            min_abs_return=max(risk.min_abs_return, artifact.threshold or 0.0),
        )
        print(
            ui.header(f"{ticker} · модель {artifact.kind} · порог входа {eff_risk.min_abs_return:.4f}")
        )

        # walk-forward предсказания лучшей моделью: только на прошлом, без утечки
        # (артефактная модель обучена на всей истории — для оценки на ней непригодна)
        test_points = features.index[int(len(features) * 0.75):]
        if artifact.kind == "lgbm":
            preds = walk_forward_lgbm(features, test_points, config.forecast_horizon)
        else:
            factories = {
                "naive_zero": NaiveZero,
                "persistence": lambda: PersistenceReturn(config.forecast_horizon),
                "ma5_ret": lambda: MovingAverageReturn(config.forecast_horizon, k=5),
                "arima": lambda: ARIMAReturn(config.forecast_horizon),
                "ets": lambda: ETSReturn(config.forecast_horizon),
            }
            preds = walk_forward_baselines(
                df["close"], features["target"], config.forecast_horizon,
                factories[artifact.kind], test_points, refit_every=48,
            )
        preds_test = preds

        result = run_backtest(df, preds_test, eff_risk, ticker=ticker)
        path = save_report(result, config.reports_dir, f"{ticker}_{config.candle_interval}_h{config.forecast_horizon}")
        m = result.metrics
        print(
            f"  Доходность {ui.fmt_signed(m['total_return'])} | "
            f"sharpe {ui.fmt_signed(m['sharpe'], '{:+.2f}')} | "
            f"просадка {ui.fmt_signed(m['max_drawdown'])} | "
            f"сделок {m['n_trades']} | win-rate {m['win_rate']:.0%}"
        )
        if m["n_trades"] == 0:
            warn("сделок нет: прогнозы модели не превышали порог входа (консервативно, деньги целы)")
        print(f"  Отчёт: {ui.paint(str(path), ui.CYAN)}\n")
    conn.close()
    print(f"Все отчёты: {ui.paint(str(config.reports_dir), ui.CYAN)}")
    return 0


def cmd_run(config: Config, max_iterations: int | None) -> int:
    from bot import TradingBot

    bot = TradingBot(config)
    bot.run_forever(max_iterations=max_iterations)
    return 0


def cmd_report(config: Config) -> int:
    api = make_api(config)
    accounts = api.get_accounts()
    if not accounts:
        warn("Счетов нет (в sandbox выполните: python cli.py smoke)")
        return 1
    account_id = accounts[0]["id"]
    portfolio = api.get_portfolio(account_id)
    total = portfolio["total_amount_rub"]
    initial = config.sandbox_initial_rub
    pnl = total - initial

    print(ui.header(f"СЧЁТ {account_id} · {config.mode}"))
    print(
        f"  Капитал:  {ui.paint(ui.fmt_money(total) + ' руб', ui.BOLD)} | "
        f"свободно: {ui.fmt_money(portfolio['cash_rub'])} руб | "
        f"P&L: {ui.fmt_signed(pnl, '{:+,.0f} руб')} "
        f"({ui.fmt_signed(total / initial - 1)} к старту {ui.fmt_money(initial)})"
    )

    print(ui.header("ПОЗИЦИИ"))
    if not portfolio["positions"]:
        print("  (нет открытых позиций)")
    else:
        rows = []
        for figi, pos in portfolio["positions"].items():
            avg = pos["average_position_price"]
            cur = pos["current_price"]
            pnl_pos = (cur / avg - 1) if avg > 0 and cur > 0 else 0.0
            rows.append(
                [
                    figi,
                    f"{pos['quantity']:.0f} шт",
                    f"{avg:.2f}",
                    f"{cur:.2f}",
                    ui.fmt_signed(pnl_pos),
                ]
            )

        print(ui.render_table(
            ["инструмент", "кол-во", "ср. цена", "текущая", "PnL"],
            rows,
            aligns=["l", "r", "r", "r", "r"],
        ))

    print(ui.header("СДЕЛКИ"))
    journal = config.reports_dir / "journal.csv"
    if journal.exists():
        import csv as _csv

        with open(journal, encoding="utf-8") as f:
            rows = list(_csv.DictReader(f))
        buys = sum(1 for r in rows if r["action"] == "BUY")
        sells = len(rows) - buys
        print(f"  Всего {len(rows)} операций: {ui.paint(f'покупок {buys}', ui.GREEN)}, "
              f"{ui.paint(f'продаж {sells}', ui.RED)}. Последние 10:")
        for row in rows[-10:]:
            action_color = ui.GREEN if row["action"] == "BUY" else ui.RED
            action = ui.paint(f"{row['action']:<4}", action_color)
            print(f"    {row['time']}  {action} {row['ticker']:<5} {row['lots']} лот(ов) "
                  f"по {row['price']} · {row['reason']}")
    else:
        print("  Журнала сделок ещё нет — появится после первого запуска бота.")

    print(ui.header("КРИВАЯ КАПИТАЛА (живой прогон)"))
    equity_file = config.reports_dir / "equity_live.csv"
    if equity_file.exists():
        equity = pd.read_csv(equity_file)
        print(f"  Точек: {len(equity)} (файл {equity_file.name})")
        print(
            f"  старт:  {equity.iloc[0]['time']} · {ui.fmt_money(equity.iloc[0]['total_rub'])} руб"
        )
        print(
            f"  сейчас: {equity.iloc[-1]['time']} · "
            f"{ui.fmt_signed(equity.iloc[-1]['total_rub'] / initial - 1)} к старту · "
            f"{ui.fmt_money(equity.iloc[-1]['total_rub'])} руб"
        )
        if len(equity) > 2:
            peak = equity["total_rub"].max()
            print(
                f"  максимум: {ui.fmt_money(peak)} руб | минимум: {ui.fmt_money(equity['total_rub'].min())} руб"
            )
    else:
        print("  Пока нет данных — появится после запуска бота (reports/equity_live.csv).")
    return 0


OFFLINE_COMMANDS = {"train", "backtest"}


def main() -> int:
    parser = argparse.ArgumentParser(description="Торговый бот T-Invest API (sandbox) с ML-прогнозом и новостным анализом")
    parser.add_argument("command", choices=["smoke", "download", "news", "train", "backtest", "run", "report"])
    parser.add_argument("--days", type=int, default=None, help="глубина истории в днях (download)")
    parser.add_argument("--iterations", type=int, default=None, help="число итераций цикла (run)")
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
        return cmd_train(config)
    if args.command == "backtest":
        return cmd_backtest(config, args.days)
    if args.command == "run":
        return cmd_run(config, args.iterations)
    if args.command == "report":
        return cmd_report(config)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

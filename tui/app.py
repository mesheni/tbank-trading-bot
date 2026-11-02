"""Read-only Textual-дашборд демона (D3).

Виджет только читает файлы reports/ через tui.data: никаких записей,
subprocess, сокетов и обращений к API. Закрытие окна не влияет на демон.
"""
from __future__ import annotations

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import DataTable, Static

import tui.data as data

_BADGE_COLORS = {
    "running": "green",
    "stale": "yellow",
    "offline": "red",
    "corrupt": "red",
}


def _fmt_rub(value: object) -> str:
    if value is None:
        return "--"
    try:
        return f"{float(value):,.2f} ₽"
    except (TypeError, ValueError):
        return "--"


def _fmt_age(age_min: object) -> str:
    if age_min is None:
        return "нет отметки"
    try:
        return f"{float(age_min):.1f} мин назад"
    except (TypeError, ValueError):
        return "нет отметки"


def _fmt_session_pnl(pnl: dict | None) -> str:
    if pnl is None:
        return "—"
    money = f"{pnl['abs_rub']:+,.0f}".replace(",", " ")
    if pnl.get("pct") is None:
        return f"{money} ₽"
    return f"{money} ₽ ({pnl['pct']:+.1%})"


def _fmt_signed_rub(value: object) -> str:
    if value is None:
        return "--"
    try:
        return f"{float(value):+,.2f} ₽"
    except (TypeError, ValueError):
        return "--"


class TbankApp(App):
    """Дашборд состояния демона; обновляется из файлов каждые TUI_REFRESH_SEC."""

    TITLE = "tbank"
    SUB_TITLE = "демон"
    CSS = """
    Screen { layout: vertical; }
    #mode { height: 1; }
    #body { height: 1fr; }
    #left {
        width: 46;
        border: round $primary;
        padding: 0 1;
    }
    #right {
        width: 1fr;
        border: round $primary;
        padding: 0 1;
    }
    .section { height: 1; text-style: bold; }
    #daemon { height: auto; }
    #positions { height: 1fr; }
    #positions-empty { height: 1; }
    #journal { height: 1fr; }
    #log { height: 1; }
    """

    def __init__(self, config) -> None:
        super().__init__()
        self.tbank_config = config

    def compose(self) -> ComposeResult:
        yield Static(self._mode_banner(), id="mode")
        with Horizontal(id="body"):
            with Vertical(id="left"):
                yield Static("", id="daemon")
                yield Static("ПОЗИЦИИ", classes="section")
                yield DataTable(id="positions")
                yield Static("", id="positions-empty")
            with Vertical(id="right"):
                yield Static("ЖУРНАЛ СДЕЛОК", classes="section")
                yield DataTable(id="journal")
        yield Static("", id="log")

    def on_mount(self) -> None:
        table = self.query_one("#journal", DataTable)
        table.add_columns("время", "тикер", "действие", "лоты", "цена")
        table.zebra_stripes = True
        positions_table = self.query_one("#positions", DataTable)
        positions_table.add_columns("тикер", "лоты", "цена", "PnL")
        positions_table.zebra_stripes = True
        self.refresh_dashboard()
        self.set_interval(self.tbank_config.tui_refresh_sec, self.refresh_dashboard)

    def _mode_banner(self) -> str:
        """Режим обычным текстом; REAL — красным и жирным средствами разметки Textual."""
        if self.tbank_config.mode == "real":
            return "[bold red] РЕЖИМ: REAL [/]"
        return " РЕЖИМ: SANDBOX "

    def _daemon_markup(self, state: dict) -> str:
        status_badge = data.badge(state)
        color = _BADGE_COLORS.get(status_badge, "red")
        equity = state.get("equity_rub")
        cash = state.get("cash_rub")
        equity_point = data.equity_last(self.tbank_config.reports_dir)
        if equity is None and equity_point is not None:
            equity = equity_point["total_rub"]
        if cash is None and equity_point is not None:
            cash = equity_point["cash_rub"]
        pnl = data.session_pnl(self.tbank_config.reports_dir)
        pending = data.pending_orders_count(self.tbank_config.reports_dir)
        kill_switch = data.kill_switch_state(self.tbank_config.reports_dir)
        if kill_switch is None:
            kill_text = "нет"
        elif kill_switch.get("corrupt"):
            kill_text = "файл повреждён"
        else:
            reason = kill_switch.get("reason") or kill_switch.get("kind") or "активен"
            kill_text = f"[red]АКТИВЕН[/] — {reason}"
        lines = [
            f"ДЕМОН: [{color}]{status_badge}[/]",
            f"состояние: {state.get('state') or '--'}",
            f"pid: {state.get('pid') if state.get('pid') is not None else '--'}",
            f"итерация: {state.get('iteration', '--')}",
            f"обновлён: {state.get('updated_at') or '--'}",
            f"heartbeat: {_fmt_age(state.get('heartbeat_age_min'))}",
            "",
            f"капитал: {_fmt_rub(equity)}",
            f"кэш: {_fmt_rub(cash)}",
            f"P&L сессии: {_fmt_session_pnl(pnl)}",
            f"позиций: {state.get('positions_count', '--')}",
            f"заявок в работе: {pending}",
            f"kill-switch: {kill_text}",
        ]
        return "\n".join(lines)

    def refresh_dashboard(self) -> None:
        """Перечитывает файлы и обновляет виджеты (без записи и сети)."""
        state = data.daemon_state(self.tbank_config.reports_dir)
        self.query_one("#daemon", Static).update(self._daemon_markup(state))

        position_rows = data.positions(state)
        positions_table = self.query_one("#positions", DataTable)
        positions_table.clear()
        for row in position_rows:
            positions_table.add_row(
                str(row.get("ticker", "") or ""),
                str(row.get("lots", "") or ""),
                _fmt_rub(row.get("price")),
                _fmt_signed_rub(row.get("pnl_rub")),
            )
        positions_empty = self.query_one("#positions-empty", Static)
        positions_table.display = bool(position_rows)
        positions_empty.display = not position_rows
        if not position_rows:
            positions_empty.update("позиции: нет данных (вне сессии)")

        table = self.query_one("#journal", DataTable)
        table.clear()
        for row in data.journal_tail(self.tbank_config.reports_dir, 20):
            table.add_row(
                str(row.get("time", "") or ""),
                str(row.get("ticker", "") or ""),
                str(row.get("action", "") or ""),
                str(row.get("lots", "") or ""),
                str(row.get("price", "") or ""),
            )

        log_file = self.tbank_config.log_file
        self.query_one("#log", Static).update(
            f"лог: {log_file}" if log_file is not None else "лог: не настроен"
        )

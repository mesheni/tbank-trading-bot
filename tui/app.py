"""Read-only Textual-дашборд демона (D3) с управлением через подтверждение (D4).

Виджет читает файлы reports/ через tui.data. Команды демону (start/stop/reset
kill-switch) идут через tui.controls: очередь файлов + ack, без сокетов. TUI
никогда не дренирует очередь и не пишет kill_switch.json при живом демоне.
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import DataTable, RichLog, Static

import bot
import control
import tui.controls as controls
import tui.data as data
from tui import _VAR_DIR

_BADGE_COLORS = {
    "running": "green",
    "stale": "yellow",
    "offline": "red",
    "corrupt": "red",
}


def _resolve_log_path(config) -> Path | None:
    """Лог демона: LOG_FILE из конфига, иначе var/daemon.log, иначе var/daemon.out."""
    if config.log_file is not None:
        return Path(config.log_file)
    for name in ("daemon.log", "daemon.out"):
        candidate = _VAR_DIR / name
        if candidate.exists():
            return candidate
    return None


def _humanize_age(age_min: float | None) -> str:
    """Возраст последней сделки по-русски; None — сделок ещё не было."""
    if age_min is None:
        return "сделок нет"
    if age_min < 1.0:
        return "только что"
    minutes = int(age_min)
    if minutes < 60:
        return f"{minutes} мин назад"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} ч назад"
    return f"{hours // 24} дн назад"


def _ack_received(reports_dir, command_id: str) -> dict | None:
    """Тонкая обёртка над ack-файлом: в тестах подменяется фейковым «демоном»."""
    return controls.read_ack(reports_dir, command_id)


class TbankApp(App):
    """Дашборд состояния демона; обновляется из файлов каждые TUI_REFRESH_SEC."""

    TITLE = "tbank"
    SUB_TITLE = "демон"
    BINDINGS = [
        Binding("s", "start_daemon", "Старт демона"),
        Binding("x", "stop_daemon", "Стоп демона"),
        Binding("k", "reset_kill_switch", "Сброс kill-switch"),
    ]
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
    #log {
        height: 12;
        border: round $primary;
        padding: 0 1;
    }
    #status { height: 1; text-style: bold; }
    """

    def __init__(self, config) -> None:
        super().__init__()
        self.tbank_config = config
        # Тесты подменяют на доли секунды, чтобы не ждать реальные 60 с.
        self.ack_timeout = 60.0
        self.ack_poll_interval = 0.2

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
        yield RichLog(id="log", max_lines=200, markup=False)
        yield Static("", id="status")

    def on_mount(self) -> None:
        # Ссылки на виджеты кэшируются: refresh-таймер не должен искать их в DOM,
        # пока модал/выключение приложения меняют стек экранов.
        self._daemon_card = self.query_one("#daemon", Static)
        self._positions_table = self.query_one("#positions", DataTable)
        self._positions_empty = self.query_one("#positions-empty", Static)
        self._journal_table = self.query_one("#journal", DataTable)
        self._log_panel = self.query_one("#log", RichLog)
        self._status_line = self.query_one("#status", Static)
        self._journal_table.add_columns("время", "тикер", "действие", "лоты", "цена")
        self._journal_table.zebra_stripes = True
        self._positions_table.add_columns("тикер", "лоты", "цена", "PnL")
        self._positions_table.zebra_stripes = True
        self.refresh_dashboard()
        self.set_interval(self.tbank_config.tui_refresh_sec, self.refresh_dashboard)

    def _mode_banner(self) -> str:
        """Режим обычным текстом; REAL — красным и жирным средствами разметки Textual."""
        if self.tbank_config.mode == "real":
            return "[bold red] РЕЖИМ: REAL [/]"
        return " РЕЖИМ: SANDBOX "

    def _mode_text(self) -> str:
        return str(self.tbank_config.mode).upper()

    def _set_status(self, text: str) -> None:
        self._status_line.update(text)

    def _state(self) -> dict:
        return data.daemon_state(self.tbank_config.reports_dir)

    def _confirm(self, action_text: str, callback) -> None:
        self.push_screen(controls.ConfirmScreen(action_text, self._mode_text()), callback=callback)

    def action_start_daemon(self) -> None:
        if controls.systemd_managed():
            self._set_status(controls.SYSTEMD_START)
            return
        refusal = controls.start_refusal(self.tbank_config, self._state())
        if refusal is not None:
            self._set_status(refusal)
            return
        self._confirm("Запустить демон?", self._on_start_confirmed)

    def _on_start_confirmed(self, confirmed: bool | None) -> None:
        if not confirmed:
            self._set_status(controls.CANCELLED)
            return
        # Повторная проверка после модала: демон мог подняться, пока пользователь думал.
        refusal = controls.start_refusal(self.tbank_config, self._state())
        if refusal is not None:
            self._set_status(refusal)
            return
        try:
            controls.spawn_daemon()
        except OSError as exc:
            self._set_status(f"не удалось запустить демон: {exc}")
            return
        self._set_status(controls.STARTING)

    def action_stop_daemon(self) -> None:
        if controls.systemd_managed():
            self._set_status(controls.SYSTEMD_STOP)
            return
        if not controls.daemon_online(self._state()):
            self._set_status(controls.DAEMON_NOT_RUNNING)
            return
        self._confirm("Остановить демон?", self._on_stop_confirmed)

    def _on_stop_confirmed(self, confirmed: bool | None) -> None:
        if not confirmed:
            self._set_status(controls.CANCELLED)
            return
        if not controls.daemon_online(self._state()):
            self._set_status(controls.DAEMON_NOT_RUNNING)
            return
        try:
            command_id = control.enqueue_command(self.tbank_config.reports_dir, "stop")
        except (OSError, ValueError) as exc:
            self._set_status(f"ошибка канала управления: {exc}")
            return
        self._set_status("останавливаю демон...")
        self._await_ack("stop", command_id)

    def action_reset_kill_switch(self) -> None:
        self._confirm("Сбросить kill-switch?", self._on_kill_confirmed)

    def _on_kill_confirmed(self, confirmed: bool | None) -> None:
        if not confirmed:
            self._set_status(controls.CANCELLED)
            return
        reports_dir = self.tbank_config.reports_dir
        if controls.kill_switch_needs_daemon(self._state()):
            try:
                command_id = control.enqueue_command(reports_dir, "kill_switch_reset")
            except (OSError, ValueError) as exc:
                self._set_status(f"ошибка канала управления: {exc}")
                return
            self._set_status("сбрасываю kill-switch...")
            self._await_ack("kill_switch_reset", command_id)
            return
        # Демон офлайн: файл никто не перезапишет — чистим напрямую.
        try:
            bot.clear_kill_switch(reports_dir)
        except OSError as exc:
            self._set_status(f"ошибка записи kill-switch: {exc}")
            return
        self._set_status(controls.KILL_SWITCH_CLEARED)

    def _ack_outcome(self, name: str) -> bool:
        """Подтверждён ли наблюдаемый результат команды (не только факт ack).

        stop — демон опубликовал ``state=stopped``; kill_switch_reset — файл
        ``kill_switch.json`` действительно исчез. Иначе «сброшен/остановлен» было
        бы ложью: ack пишется ещё до выполнения команды.
        """
        reports_dir = self.tbank_config.reports_dir
        if name == "stop":
            return data.daemon_state(reports_dir).get("state") == "stopped"
        return bot.read_kill_switch(reports_dir) is None

    @work(exclusive=True, group="ack")
    async def _await_ack(self, name: str, command_id: str) -> None:
        """Ждём ack файлом и подтверждаем результат; ack ok=False — сразу ошибка."""
        reports_dir = self.tbank_config.reports_dir
        deadline = time.monotonic() + self.ack_timeout
        while True:
            try:
                ack = _ack_received(reports_dir, command_id)
            except (OSError, ValueError) as exc:
                self._set_status(f"ошибка проверки ack: {exc}")
                return
            if isinstance(ack, dict) and not ack.get("ok", False):
                detail = str(ack.get("detail") or "неизвестная ошибка")
                self._set_status(f"ошибка выполнения {name}: {detail}")
                return
            if isinstance(ack, dict) and ack.get("ok") and self._ack_outcome(name):
                self._set_status(
                    controls.STOPPED if name == "stop" else controls.KILL_SWITCH_RESET_CONFIRMED
                )
                return
            if time.monotonic() >= deadline:
                self._set_status(
                    controls.STOP_TIMEOUT if name == "stop" else controls.KILL_SWITCH_TIMEOUT
                )
                return
            await asyncio.sleep(self.ack_poll_interval)

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
        last_trade = _humanize_age(
            data.journal_last_age_min(self.tbank_config.reports_dir)
        )
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
            f"heartbeat: {controls.fmt_age(state.get('heartbeat_age_min'))}",
            f"последняя сделка: {last_trade}",
            "",
            f"капитал: {controls.fmt_rub(equity)}",
            f"кэш: {controls.fmt_rub(cash)}",
            f"P&L сессии: {controls.fmt_session_pnl(pnl)}",
            f"позиций: {state.get('positions_count', '--')}",
            f"заявок в работе: {pending}",
            f"kill-switch: {kill_text}",
        ]
        return "\n".join(lines)

    def refresh_dashboard(self) -> None:
        """Перечитывает файлы и обновляет виджеты (без записи и сети)."""
        state = data.daemon_state(self.tbank_config.reports_dir)
        self._daemon_card.update(self._daemon_markup(state))

        position_rows = data.positions(state)
        self._positions_table.clear()
        for row in position_rows:
            self._positions_table.add_row(
                str(row.get("ticker", "") or ""),
                str(row.get("lots", "") or ""),
                controls.fmt_rub(row.get("price")),
                controls.fmt_signed_rub(row.get("pnl_rub")),
            )
        self._positions_table.display = bool(position_rows)
        self._positions_empty.display = not position_rows
        if not position_rows:
            self._positions_empty.update("позиции: нет данных (вне сессии)")

        self._journal_table.clear()
        for row in data.journal_tail(self.tbank_config.reports_dir, 20):
            self._journal_table.add_row(
                str(row.get("time", "") or ""),
                str(row.get("ticker", "") or ""),
                str(row.get("action", "") or ""),
                str(row.get("lots", "") or ""),
                str(row.get("price", "") or ""),
            )

        self._refresh_log()

    def _log_content(self) -> list[str]:
        """Хвост лога (30 строк) с заголовком; нет файла — явное сообщение."""
        path = _resolve_log_path(self.tbank_config)
        if path is None:
            return ["ЛОГ: --", "лог не найден"]
        header = f"ЛОГ: {path}"
        if not path.exists():
            return [header, "лог не найден"]
        lines = data.tail_log(path, 30)
        if not lines:
            return [header, "(лог пуст)"]
        return [header, *lines]

    def _refresh_log(self) -> None:
        """Перерисовывает RichLog и прокручивает его к самой новой строке."""
        self._log_panel.clear()
        for line in self._log_content():
            self._log_panel.write(line)
        self._log_panel.scroll_end(animate=False)

"""Управление демоном из TUI: подтверждающий модал и файловые операции (D4).

Здесь нет торговых решений: только команды `stop` / `kill_switch_reset` через
очередь ``reports/commands`` (никогда не ``drain_commands`` — это работа демона),
запуск ``cli.py daemon run`` отдельным процессом и ожидание ack по файлу.

``TUI_SYSTEMD_MANAGED=1`` читается из окружения напрямую (ставится systemd-юнитом):
под ``Restart=always`` остановка из TUI бессмысленна, поэтому s/x не предлагаются.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static

import control
from tui.data import badge

ROOT = Path(__file__).resolve().parents[1]

ALREADY_RUNNING = "демон уже запущен"
STARTING = "демон запускается..."
STOPPED = "демон остановлен"
STOP_TIMEOUT = "демон не подтвердил остановку"
DAEMON_NOT_RUNNING = "демон не запущен"
KILL_SWITCH_CLEARED = "kill-switch сброшен"
KILL_SWITCH_RESET_CONFIRMED = "kill-switch сброшен (подтверждено демоном)"
KILL_SWITCH_TIMEOUT = "демон не подтвердил сброс kill-switch"
CANCELLED = "отменено"
SYSTEMD_START = "управляется systemd: systemctl start tbank-bot"
SYSTEMD_STOP = "управляется systemd: systemctl stop tbank-bot"

# Дословно сообщение cli._real_trading_guard: одна семантика у CLI и TUI.
REAL_TRADING_REFUSAL = (
    "[!!] MODE=real: для торговли на реальном счёте задайте CONFIRM_REAL_TRADING=YES "
    "в окружении или .env"
)

# Windows: DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP — демон переживает закрытие TUI.
_WIN_DETACHED_PROCESS = 0x00000008
_WIN_CREATE_NEW_PROCESS_GROUP = 0x00000200


def systemd_managed() -> bool:
    """True, если TUI запущен рядом с systemd-юнитом (env ставит deploy/tbank-bot.service)."""
    return os.getenv("TUI_SYSTEMD_MANAGED", "").strip() == "1"


def fmt_rub(value: object) -> str:
    """Сумма в рублях или «--» для отсутствующего/битого значения."""
    if value is None:
        return "--"
    try:
        return f"{float(value):,.2f} ₽"
    except (TypeError, ValueError):
        return "--"


def fmt_signed_rub(value: object) -> str:
    """Сумма со знаком (PnL позиции) или «--»."""
    if value is None:
        return "--"
    try:
        return f"{float(value):+,.2f} ₽"
    except (TypeError, ValueError):
        return "--"


def fmt_age(age_min: object) -> str:
    """Возраст heartbeat в минутах."""
    if age_min is None:
        return "нет отметки"
    try:
        return f"{float(age_min):.1f} мин назад"
    except (TypeError, ValueError):
        return "нет отметки"


def fmt_session_pnl(pnl: dict | None) -> str:
    """P&L сессии: рубли и процент, если есть точка отсчёта."""
    if pnl is None:
        return "—"
    money = f"{pnl['abs_rub']:+,.0f}".replace(",", " ")
    if pnl.get("pct") is None:
        return f"{money} ₽"
    return f"{money} ₽ ({pnl['pct']:+.1%})"


def daemon_online(state: dict) -> bool:
    """Демон считается живым, если статус ``running``/``stopping`` или свежий heartbeat.

    Явный ``stopped`` сильнее heartbeat: после мягкой остановки heartbeat ещё свежий,
    но команды слать уже некому.
    """
    if state.get("corrupt"):
        return False
    state_name = state.get("state")
    if state_name in ("running", "stopping"):
        return True
    if state_name == "stopped":
        return False
    return badge(state) in ("running", "stale")


def kill_switch_needs_daemon(state: dict) -> bool:
    """Сброс kill-switch идёт через демон, если он жив ИЛИ статус повреждён (fail closed).

    Прямая запись ``kill_switch.json`` при живом демоне запрещена: он перезапишет файл
    своим состоянием. Повреждённый статус трактуем как «возможно жив» — безопаснее.
    """
    return daemon_online(state) or bool(state.get("corrupt"))


def start_refusal(config, state: dict) -> str | None:
    """Причина не запускать демон или None, если запуск разрешён."""
    if daemon_online(state):
        return ALREADY_RUNNING
    if config.mode == "real" and not getattr(config, "confirm_real_trading", False):
        return REAL_TRADING_REFUSAL
    return None


def daemon_argv() -> list[str]:
    """Команда запуска: тот же интерпретатор, что у TUI (venv python), без shell."""
    return [sys.executable, str(ROOT / "cli.py"), "daemon", "run"]


def _daemon_out_path() -> Path:
    """Куда писать stdout/stderr отдельного демона (в тестах подменяется на tmp)."""
    return ROOT / "var" / "daemon.out"


def spawn_daemon() -> subprocess.Popen:
    """Запускает ``daemon run`` отсоединённым процессом; окно TUI можно закрыть."""
    out_path = _daemon_out_path()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    kwargs: dict[str, object] = {
        "cwd": str(ROOT),
        "stdin": subprocess.DEVNULL,
        "shell": False,  # argv-список, без оболочки — инъекция невозможна
    }
    if os.name == "nt":
        kwargs["creationflags"] = _WIN_DETACHED_PROCESS | _WIN_CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    with open(out_path, "ab") as stream:
        return subprocess.Popen(daemon_argv(), stdout=stream, stderr=subprocess.STDOUT, **kwargs)


def read_ack(reports_dir, command_id: str) -> bool:
    """Ack получен? Читаем файл напрямую; битый JSON — «ещё нет» (дожимает дедлайн)."""
    path = control.paths(reports_dir)["acks"] / f"{command_id}.json"
    if not path.exists():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(payload, dict)


class ConfirmScreen(ModalScreen[bool]):
    """Модал подтверждения: текст действия, текущий режим, фокус по умолчанию — «Отмена»."""

    BINDINGS = [
        Binding("escape", "cancel", "Отмена"),
        # s/x/k не должны открывать второй модал поверх первого.
        Binding("s", "noop", show=False),
        Binding("x", "noop", show=False),
        Binding("k", "noop", show=False),
    ]
    CSS = """
    ConfirmScreen { align: center middle; }
    #confirm-dialog {
        width: 64;
        height: auto;
        border: thick $primary;
        background: $surface;
        padding: 1 2;
    }
    #confirm-title { text-style: bold; }
    #confirm-mode { color: $text-muted; }
    #confirm-question { margin-top: 1; }
    #confirm-buttons { height: auto; align-horizontal: right; margin-top: 1; }
    #confirm-buttons Button { margin-left: 1; }
    """

    def __init__(self, action_text: str, mode: str) -> None:
        super().__init__()
        self.action_text = action_text
        self.mode_text = mode

    def compose(self):
        with Vertical(id="confirm-dialog"):
            yield Static(self.action_text, id="confirm-title")
            yield Static(f"Режим: {self.mode_text}", id="confirm-mode")
            yield Static("Подтвердить действие?", id="confirm-question")
            with Horizontal(id="confirm-buttons"):
                yield Button("Отмена", id="cancel")
                yield Button("Подтвердить", id="confirm", variant="error")

    def on_mount(self) -> None:
        self.query_one("#cancel", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm")

    def action_cancel(self) -> None:
        self.dismiss(False)

    def action_noop(self) -> None:
        """Перехват s/x/k, пока открыт модал: только явный выбор кнопки."""

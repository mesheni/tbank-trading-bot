"""Файловый канал управления демоном: статус-снимок, очередь команд, PID-lock.

Данные — обычные файлы в ``reports/``, без сокетов и HTTP; single-writer:
``daemon_status.json`` пишет только демон, команды кладёт клиент в
``commands/<utc-ts>-<uuid>.json``, демон переносит их в ``acks/<id>.json``;
``daemon.lock``/``daemon.pid`` не дают запустить второй экземпляр.
"""
from __future__ import annotations

import ctypes
import datetime as dt
import json
import logging
import os
import uuid
from pathlib import Path

log = logging.getLogger(__name__)

# Разрешённые команды: всё остальное — ошибка клиента, а не «на всякий случай».
ALLOWED_COMMANDS = frozenset({"stop", "kill_switch_reset"})
ALLOWED_STATES = frozenset({"running", "stopping", "stopped"})

# Поля, которые обязаны присутствовать в каждом снимке состояния.
STATUS_DEFAULTS: dict[str, object] = {
    "pid": None,
    "state": "stopped",
    "mode": None,
    "iteration": 0,
    "equity_rub": None,
    "cash_rub": None,
    "positions_count": 0,
    "kill_switch_active": False,
    "kill_switch_reason": None,
    "last_error": None,
}

# Мусор в очереди не должен спамить лог на каждом тике (демон опрашивает 1 с).
_WARNED_BAD_COMMANDS: set[str] = set()

# Liveness на Windows: OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION) + GetExitCodeProcess.
_WIN_STILL_ACTIVE = 259
_WIN_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_WIN_ERROR_ACCESS_DENIED = 5

if os.name == "nt":
    _KERNEL32 = ctypes.windll.kernel32
    _KERNEL32.OpenProcess.restype = ctypes.c_void_p
    _KERNEL32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    _KERNEL32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    _KERNEL32.CloseHandle.argtypes = [ctypes.c_void_p]


def paths(reports_dir) -> dict[str, Path]:
    """Пути канала управления внутри каталога отчётов.

    Ключи: ``pid`` (daemon.pid), ``lock`` (daemon.lock), ``status``
    (daemon_status.json), ``commands`` (commands/), ``acks`` (acks/).
    Сам каталог не создаётся — это делает конкретная операция записи.
    """
    root = Path(reports_dir)
    return {
        "pid": root / "daemon.pid",
        "lock": root / "daemon.lock",
        "status": root / "daemon_status.json",
        "commands": root / "commands",
        "acks": root / "acks",
    }


def _utc_now() -> str:
    """Текущее время UTC в ISO-8601 (секунды)."""
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _utc_stamp() -> str:
    """Метка UTC для имени файла команды: лексикографический порядок = хронология."""
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _write_text_atomic(path: Path, text: str) -> None:
    """Атомарная запись: временный файл рядом + os.replace (как bot.py:99-105)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _write_json_atomic(path: Path, payload: dict) -> None:
    """Атомарная запись JSON в UTF-8 без экранирования кириллицы."""
    _write_text_atomic(path, json.dumps(payload, ensure_ascii=False, indent=2))


def write_status(reports_dir, payload: dict) -> None:
    """Атомарно пишет daemon_status.json; пустые поля — из STATUS_DEFAULTS.

    ``updated_at`` всегда равен моменту записи; неизвестное состояние —
    ValueError (молча писать мусор нельзя).
    """
    status = dict(STATUS_DEFAULTS)
    status.update(payload)
    state = status.get("state")
    if state not in ALLOWED_STATES:
        raise ValueError(f"Недопустимое состояние демона: {state!r}")

    stamp = _utc_now()
    pid = status.get("pid")
    status["pid"] = int(pid) if pid is not None else os.getpid()
    status["state"] = state
    status["started_at"] = status.get("started_at") or stamp
    status["updated_at"] = stamp
    status["iteration"] = int(status.get("iteration") or 0)
    status["positions_count"] = int(status.get("positions_count") or 0)
    status["kill_switch_active"] = bool(status.get("kill_switch_active"))
    for key in ("equity_rub", "cash_rub"):
        value = status.get(key)
        status[key] = float(value) if value is not None else None

    _write_json_atomic(paths(reports_dir)["status"], status)


def read_status(reports_dir) -> dict | None:
    """Снимок состояния с диска; None — файла нет, {"corrupt": True} — fail closed."""
    path = paths(reports_dir)["status"]
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"corrupt": True}
    return payload if isinstance(payload, dict) else {"corrupt": True}


def enqueue_command(reports_dir, name: str) -> str:
    """Кладёт команду в очередь и возвращает её id (имя файла без .json).

    Разрешены только ``stop`` и ``kill_switch_reset``; неизвестное имя —
    ValueError до создания каталогов и файлов.
    """
    if name not in ALLOWED_COMMANDS:
        allowed = ", ".join(sorted(ALLOWED_COMMANDS))
        raise ValueError(f"Недопустимая команда: {name!r}; разрешены: {allowed}")

    command_id = f"{_utc_stamp()}-{uuid.uuid4().hex}"
    target = paths(reports_dir)["commands"] / f"{command_id}.json"
    _write_json_atomic(target, {"name": name, "requested_at": _utc_now()})
    return command_id


def _warn_bad_command(path: Path, reason: str) -> None:
    """Предупреждает о мусоре в очереди один раз за жизнь процесса."""
    key = str(path)
    if key in _WARNED_BAD_COMMANDS:
        return
    _WARNED_BAD_COMMANDS.add(key)
    log.warning("Пропущена некорректная команда %s: %s", path.name, reason)


def drain_commands(reports_dir) -> list[dict]:
    """Обрабатывает очередь по именам: ack -> удаление; мусор пропускается с warning.

    Ack пишется до удаления команды: at-least-once, команда не теряется.
    """
    channel = paths(reports_dir)
    commands_dir = channel["commands"]
    acks_dir = channel["acks"]
    commands_dir.mkdir(parents=True, exist_ok=True)
    acks_dir.mkdir(parents=True, exist_ok=True)

    drained: list[dict] = []
    for path in sorted(commands_dir.iterdir(), key=lambda item: item.name):
        if not path.is_file():
            continue
        if path.suffix != ".json":
            _warn_bad_command(path, "не JSON-файл команды")
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _warn_bad_command(path, "не читается или битый JSON")
            continue
        name = payload.get("name") if isinstance(payload, dict) else None
        if name not in ALLOWED_COMMANDS:
            _warn_bad_command(path, f"неизвестная команда {name!r}")
            continue

        command_id = path.stem
        handled_at = _utc_now()
        ack = {"name": name, "handled_at": handled_at, "ok": True, "detail": ""}
        try:
            _write_json_atomic(acks_dir / f"{command_id}.json", ack)
        except OSError as exc:
            log.warning("Не удалось записать ack для команды %s: %s", command_id, exc)
            continue
        try:
            path.unlink()
        except OSError as exc:
            log.warning("Не удалось удалить обработанную команду %s: %s", command_id, exc)
            continue
        drained.append({"id": command_id, **ack})
    return drained


def _pid_alive(pid: int) -> bool:
    """Жив ли процесс: os.kill(pid, 0) на POSIX, OpenProcess на Windows (без psutil).

    PID ≤ 0 не живой; переиспользование PID обрабатывается прагматично.
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        return _pid_alive_windows(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _pid_alive_windows(pid: int) -> bool:
    """Liveness на Windows через kernel32 (без сторонних зависимостей)."""
    handle = _KERNEL32.OpenProcess(_WIN_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        # Нет доступа — процесс существует, но принадлежит другому пользователю.
        return _KERNEL32.GetLastError() == _WIN_ERROR_ACCESS_DENIED
    try:
        code = ctypes.c_ulong()
        if not _KERNEL32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == _WIN_STILL_ACTIVE
    finally:
        _KERNEL32.CloseHandle(handle)


def _read_lock_pid(path: Path) -> int | None:
    """PID из lock-файла; None — файла нет, он пуст, битый или pid ≤ 0."""
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        pid = int(raw)
    except ValueError:
        return None
    return pid if pid > 0 else None


def acquire_lock(reports_dir) -> bool:
    """Захватывает daemon.lock; False — lock уже держит живой процесс.

    Создание O_CREAT|O_EXCL; устаревший/повреждённый lock перехватывается
    атомарной перезаписью (теоретическое окно гонки приемлемо для файлов).
    """
    channel = paths(reports_dir)
    lock_path = channel["lock"]
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    pid = os.getpid()

    try:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        holder = _read_lock_pid(lock_path)
        if holder is not None and _pid_alive(holder):
            log.info("Демон уже запущен (pid %s) — lock не захвачен", holder)
            return False
        if holder is None:
            log.warning("Файл %s повреждён — перехватываю lock", lock_path.name)
        else:
            log.warning("Снимаю устаревший lock мёртвого процесса pid %s", holder)
        _write_text_atomic(lock_path, f"{pid}\n")
    else:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(f"{pid}\n")

    _write_text_atomic(channel["pid"], f"{pid}\n")
    return True


def release_lock(reports_dir) -> bool:
    """Снимает свой lock и daemon.pid; чужой живой lock не трогает (False)."""
    channel = paths(reports_dir)
    lock_path = channel["lock"]
    if not lock_path.exists():
        return False
    holder = _read_lock_pid(lock_path)
    if holder is not None and holder != os.getpid():
        log.warning("lock принадлежит pid %s — не снимаю", holder)
        return False
    try:
        lock_path.unlink()
    except FileNotFoundError:
        return False
    try:
        channel["pid"].unlink()
    except FileNotFoundError:
        pass
    return True

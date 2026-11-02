"""Проверки файлов автозапуска: Windows-планировщик и systemd-юнит."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy"
AUTOSTART = DEPLOY / "windows" / "install-autostart.ps1"


def _service_text() -> str:
    return (DEPLOY / "tbank-bot.service").read_text(encoding="utf-8")


def test_systemd_unit_runs_headless_daemon():
    text = _service_text()
    assert "ExecStart=__APP_DIR__/.venv/bin/python cli.py daemon run" in text
    assert "daemon run" in text
    # Поведение сервиса не меняется: предстартовая проверка конфига и автоперезапуск.
    assert "ExecStartPre=__APP_DIR__/.venv/bin/python cli.py check-config" in text
    assert "Restart=always" in text


def test_systemd_unit_marks_tui_as_systemd_managed():
    assert "Environment=TUI_SYSTEMD_MANAGED=1" in _service_text()


def test_windows_autostart_script_exists_with_expected_content():
    assert AUTOSTART.is_file(), "нет deploy/windows/install-autostart.ps1"
    text = AUTOSTART.read_text(encoding="utf-8-sig")
    for needle in ("ONLOGON", "tbank-daemon", "-Uninstall", "pythonw"):
        assert needle in text, f"в скрипте нет {needle}"
    # Удаление при отсутствующей задаче - штатный путь, а не ошибка.
    assert "не найдена" in text


def test_windows_autostart_script_is_utf8_with_bom():
    raw = AUTOSTART.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf"), "нужен UTF-8 BOM: иначе PowerShell 5.1 читает кириллицу как ANSI"
    # Декодирование без BOM тоже должно давать читаемый русский текст (защита от мохибайка).
    assert "Автозапуск" in raw.decode("utf-8-sig")


def test_deploy_md_points_to_windows_autostart_script():
    text = (ROOT / "DEPLOY.md").read_text(encoding="utf-8")
    assert "install-autostart.ps1" in text

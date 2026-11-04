"""Проверки файлов автозапуска: Windows-планировщик и systemd-юнит."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy"
AUTOSTART = DEPLOY / "windows" / "install-autostart.ps1"
WEB_SERVICE = DEPLOY / "tbank-web.service"
SUDOERS = DEPLOY / "sudoers-tbank-web"
CADDYFILE = DEPLOY / "Caddyfile.example"


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


def test_web_unit_runs_panel_and_marks_systemd_managed():
    assert WEB_SERVICE.is_file(), "нет deploy/tbank-web.service"
    text = WEB_SERVICE.read_text(encoding="utf-8")
    assert "ExecStart=__APP_DIR__/.venv/bin/python cli.py web" in text
    # Панель не должна поднимать свой демон: старт/стоп идут через systemctl.
    assert "Environment=TUI_SYSTEMD_MANAGED=1" in text
    assert "EnvironmentFile=__APP_DIR__/.env" in text
    assert "Restart=on-failure" in text
    assert "RestartSec=5" in text


def test_sudoers_snippet_allows_only_start_and_stop_of_bot_unit():
    assert SUDOERS.is_file(), "нет deploy/sudoers-tbank-web"
    text = SUDOERS.read_text(encoding="utf-8")
    rules = [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert rules == [
        "__WEB_USER__ ALL=(root) NOPASSWD: /bin/systemctl start tbank-bot",
        "__WEB_USER__ ALL=(root) NOPASSWD: /bin/systemctl stop tbank-bot",
    ]
    assert "*" not in text, "в sudoers не должно быть шаблонов"


def test_deploy_files_have_no_public_bind():
    assert CADDYFILE.is_file(), "нет deploy/Caddyfile.example"
    offenders = sorted(
        str(path.relative_to(ROOT))
        for path in DEPLOY.rglob("*")
        if path.is_file() and "0.0.0.0" in path.read_text(encoding="utf-8", errors="ignore")
    )
    assert not offenders, f"публичный bind в deploy/: {offenders}"
    caddy = CADDYFILE.read_text(encoding="utf-8")
    assert "reverse_proxy 127.0.0.1:8000" in caddy
    assert "0.0.0.0" not in caddy


def test_deploy_md_has_web_panel_section():
    text = (ROOT / "DEPLOY.md").read_text(encoding="utf-8")
    assert "### Веб-панель" in text or "Веб-панель" in text

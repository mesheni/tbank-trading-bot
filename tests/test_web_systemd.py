"""T6: узкий адаптер systemctl — argv без shell, валидация action/unit, таймаут, Windows-отказ.

subprocess.run и os-поверхность подменяются: ни один тест не запускает настоящий systemctl.
"""
from __future__ import annotations

import os
import subprocess
from types import SimpleNamespace

import pytest

import web.systemd as web_systemd
from web.systemd import SystemctlResult, run_systemctl

WINDOWS_DETAIL = "systemctl недоступен на этой ОС"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("SYSTEMD_UNIT", raising=False)


def _fake_os(*, name: str = "posix", euid: int = 1000) -> SimpleNamespace:
    return SimpleNamespace(name=name, geteuid=lambda: euid, getenv=os.getenv)


def _patch_os(monkeypatch, *, name: str = "posix", euid: int = 1000) -> None:
    monkeypatch.setattr(web_systemd, "os", _fake_os(name=name, euid=euid))


class _RunSpy:
    """Замена subprocess.run: запоминает argv/kwargs и отдаёт заданный результат."""

    def __init__(self, returncode: int = 0, stderr: str = "") -> None:
        self.calls: list[tuple[list[str], dict]] = []
        self.returncode = returncode
        self.stderr = stderr

    def __call__(self, argv: list[str], **kwargs) -> SimpleNamespace:
        self.calls.append((argv, kwargs))
        return SimpleNamespace(returncode=self.returncode, stderr=self.stderr, stdout="")


def _patch_run(monkeypatch, spy) -> None:
    monkeypatch.setattr(web_systemd.subprocess, "run", spy)


def test_non_root_posix_argv_uses_sudo(monkeypatch):
    _patch_os(monkeypatch, euid=1000)
    spy = _RunSpy()
    _patch_run(monkeypatch, spy)

    result = run_systemctl("stop")

    assert result == SystemctlResult(ok=True, returncode=0, detail="")
    assert len(spy.calls) == 1
    argv, kwargs = spy.calls[0]
    assert argv == ["sudo", "-n", "/bin/systemctl", "stop", "tbank-bot"]
    assert kwargs["shell"] is False
    assert kwargs["timeout"] == 20.0


def test_root_posix_argv_has_no_sudo(monkeypatch):
    _patch_os(monkeypatch, euid=0)
    spy = _RunSpy()
    _patch_run(monkeypatch, spy)

    result = run_systemctl("start")

    assert result.ok is True
    argv, kwargs = spy.calls[0]
    assert argv == ["/bin/systemctl", "start", "tbank-bot"]
    assert "sudo" not in argv
    assert kwargs["shell"] is False


def test_invalid_action_raises_value_error(monkeypatch):
    _patch_os(monkeypatch)
    spy = _RunSpy()
    _patch_run(monkeypatch, spy)

    with pytest.raises(ValueError):
        run_systemctl("restart")

    assert spy.calls == []


def test_invalid_unit_with_shell_metacharacters_raises(monkeypatch):
    _patch_os(monkeypatch)
    spy = _RunSpy()
    _patch_run(monkeypatch, spy)

    with pytest.raises(ValueError):
        run_systemctl("stop", "x;rm")

    assert spy.calls == []


@pytest.mark.parametrize("bad_unit", ["", "has space", "юнит", "x;rm -rf /", "unit\n"])
def test_malformed_unit_raises_value_error(monkeypatch, bad_unit):
    _patch_os(monkeypatch)
    spy = _RunSpy()
    _patch_run(monkeypatch, spy)

    with pytest.raises(ValueError):
        run_systemctl("stop", bad_unit)

    assert spy.calls == []


def test_unit_from_environment_is_used(monkeypatch):
    monkeypatch.setenv("SYSTEMD_UNIT", " custom-bot@1.service ")
    _patch_os(monkeypatch, euid=1000)
    spy = _RunSpy()
    _patch_run(monkeypatch, spy)

    run_systemctl("stop")

    argv, _ = spy.calls[0]
    assert argv == ["sudo", "-n", "/bin/systemctl", "stop", "custom-bot@1.service"]


def test_nonzero_exit_returns_error_with_trimmed_stderr(monkeypatch):
    _patch_os(monkeypatch)
    noisy = "  " + ("boom " * 100) + "  "
    spy = _RunSpy(returncode=5, stderr=noisy)
    _patch_run(monkeypatch, spy)

    result = run_systemctl("stop")

    assert result.ok is False
    assert result.returncode == 5
    assert result.detail == ("boom " * 100).strip()[:200].rstrip()
    assert len(result.detail) <= 200
    assert result.detail == result.detail.strip()


def test_timeout_returns_timeout_error_without_raising(monkeypatch):
    _patch_os(monkeypatch)

    def _timeout(argv, **kwargs):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout"))

    _patch_run(monkeypatch, _timeout)

    result = run_systemctl("start")

    assert result.ok is False
    assert result.detail == "timeout"
    assert result.returncode is None


def test_oserror_returns_error_with_trimmed_detail(monkeypatch):
    _patch_os(monkeypatch)

    def _missing(argv, **kwargs):
        raise FileNotFoundError("sudo: not found")

    _patch_run(monkeypatch, _missing)

    result = run_systemctl("stop")

    assert result.ok is False
    assert result.returncode is None
    assert result.detail == "sudo: not found"


def test_windows_branch_never_calls_subprocess(monkeypatch):
    _patch_os(monkeypatch, name="nt")
    spy = _RunSpy()
    _patch_run(monkeypatch, spy)

    result = run_systemctl("stop")

    assert result == SystemctlResult(ok=False, returncode=None, detail=WINDOWS_DETAIL)
    assert spy.calls == []

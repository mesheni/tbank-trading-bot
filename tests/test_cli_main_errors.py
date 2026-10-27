"""main(): ошибки API и сети превращаются в понятное сообщение, а не трейсбек."""
from __future__ import annotations

import sys

import requests

import cli
from tbank.rest import APIError


def _run_status(monkeypatch, command) -> int:
    monkeypatch.setattr(sys, "argv", ["cli.py", "status"])
    monkeypatch.setattr(cli, "cmd_status", command)
    return cli.main()


def test_main_reports_api_error_without_traceback(monkeypatch, capsys) -> None:
    def boom(config):
        raise APIError(500, "boom")

    rc = _run_status(monkeypatch, boom)

    captured = capsys.readouterr()
    assert rc == 2
    assert "Ошибка API" in captured.err
    assert "HTTP 500: boom" in captured.err


def test_main_reports_network_error(monkeypatch, capsys) -> None:
    def boom(config):
        raise requests.ConnectionError("no route to host")

    rc = _run_status(monkeypatch, boom)

    captured = capsys.readouterr()
    assert rc == 2
    assert "Нет соединения с API" in captured.err
    assert "no route to host" in captured.err


def test_main_reports_runtime_error(monkeypatch, capsys) -> None:
    def boom(config):
        raise RuntimeError("счёт не найден")

    rc = _run_status(monkeypatch, boom)

    captured = capsys.readouterr()
    assert rc == 2
    assert "[!!] счёт не найден" in captured.err

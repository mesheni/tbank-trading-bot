"""U2: прогресс-строки `[i/N]` для download и train.

Проверяем только префиксы/счётчики строк: elapsed-время — не контракт
(никаких wall-clock ассертов), поэтому тесты не флакуют.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pandas as pd
import pytest

import cli


class FakeDownloadAPI:
    def resolve_instruments(self, tickers):
        return {t: {"figi": f"FIGI-{t}", "uid": f"uid-{t}", "lot": 1} for t in tickers}


def _patch_download(monkeypatch, tickers, fake_download):
    monkeypatch.setattr(cli, "make_api", lambda config: FakeDownloadAPI())
    monkeypatch.setattr(cli, "connect", lambda db_path: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(cli, "store_instruments", lambda conn, instruments: None)
    monkeypatch.setattr(cli, "download_candles", fake_download)
    return SimpleNamespace(
        tickers=list(tickers), candle_interval="hour", db_path="mem.sqlite"
    )


def _progress_lines(out: str) -> list[str]:
    return [line for line in out.splitlines() if line.startswith("[")]


# ---------- characterization: поведение до U2 ----------

def test_download_output_characterization_before_progress(tmp_path, monkeypatch, capsys):
    """Фиксирует уже существующие строки: [i/N] ticker и «ticker: N свечей»."""
    config = _patch_download(monkeypatch, ["SBER", "GAZP", "LKOH"], lambda *a, **k: 5)

    assert cli.cmd_download(config, days=3) == 0

    out = capsys.readouterr().out
    assert "[1/3] SBER" in out
    assert "[2/3] GAZP" in out
    assert "[3/3] LKOH" in out
    assert "SBER: 5 свечей" in out
    assert "\r" not in out


# ---------- download: U2 ----------

def test_download_progress_has_counts_elapsed_and_eta(tmp_path, monkeypatch, capsys):
    config = _patch_download(
        monkeypatch, ["SBER", "GAZP", "LKOH"], lambda *a, **k: 5
    )

    assert cli.cmd_download(config, days=3) == 0

    out = capsys.readouterr().out
    lines = _progress_lines(out)
    assert len(lines) == 3  # ровно N строк, никакой «двойной» печати

    assert lines[0].startswith("[1/3] SBER: 5 свечей, ")
    assert lines[0].endswith("s")
    assert "ETA" not in lines[0]  # ETA появляется только после первого тикера

    assert lines[1].startswith("[2/3] GAZP: 5 свечей, ")
    assert "ETA ~" in lines[1]
    assert lines[2].startswith("[3/3] LKOH: 5 свечей, ")
    assert "ETA ~" in lines[2]
    assert "\r" not in out


def test_download_failed_ticker_reports_error_and_keeps_exception(
    tmp_path, monkeypatch, capsys
):
    def flaky(api, conn, figi, interval, days_back=None, ticker=None):
        if ticker == "GAZP":
            raise RuntimeError("API down")
        return 4

    config = _patch_download(monkeypatch, ["SBER", "GAZP", "LKOH"], flaky)

    with pytest.raises(RuntimeError, match="API down"):
        cli.cmd_download(config, days=3)

    lines = _progress_lines(capsys.readouterr().out)
    failed = [line for line in lines if line.startswith("[2/3]")]
    assert len(failed) == 1
    assert "ошибка" in failed[0]
    assert len(lines) == 2  # третий тикер не начинался
    assert "\r" not in "".join(lines)


def test_download_ticker_with_spaces_stays_one_line(tmp_path, monkeypatch, capsys):
    config = _patch_download(monkeypatch, ["SBER TEST"], lambda *a, **k: 1)

    assert cli.cmd_download(config, days=1) == 0

    lines = _progress_lines(capsys.readouterr().out)
    assert len(lines) == 1
    assert lines[0].startswith("[1/1] SBER TEST: 1 свечей, ")


# ---------- train: U2 ----------

def _train_config(tmp_path, tickers):
    return SimpleNamespace(
        db_path=tmp_path / "db.sqlite",
        tickers=list(tickers),
        candle_interval="hour",
        forecast_horizon=1,
        commission_pct=0.0004,
        slippage_pct=0.0002,
        models_dir=tmp_path / "models",
        reports_dir=tmp_path / "reports",
    )


def _fake_metrics() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "rmse": [0.01],
            "mae": [0.01],
            "directional_acc": [0.6],
            "strategy_sharpe": [1.0],
            "strategy_sharpe_net": [0.5],
            "live_sharpe_net": [0.5],
            "live_trades": [10],
            "n_points": [100],
        },
        index=["persistence"],
    )


def test_train_prints_progress_per_ticker_model_phase(tmp_path, monkeypatch, capsys, candles):
    monkeypatch.setattr(cli, "connect", lambda _path: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(cli, "load_candles", lambda _conn, _ticker, _interval: candles)
    monkeypatch.setattr(cli, "evaluate_all", lambda *a, **k: (_fake_metrics(), None))
    monkeypatch.setattr(
        cli,
        "train_and_save",
        lambda *a, **k: SimpleNamespace(
            kind="persistence", metrics={"strategy_sharpe_net": 0.5}, threshold=0.004
        ),
    )

    config = _train_config(tmp_path, ["SBER", "GAZP"])

    assert cli.cmd_train(config) == 0

    out = capsys.readouterr().out
    lines = _progress_lines(out)
    assert len(lines) == 2
    assert lines[0].startswith("[1/2] модель SBER ")
    assert lines[1].startswith("[2/2] модель GAZP ")
    # registry реально оценивает 6 моделей: 5 бейзлайнов из model_factories + lgbm,
    # который evaluate_all регистрирует отдельно (см. models/registry.py:269-293).
    assert "(6 моделей)" in lines[0]
    assert "(6 моделей)" in lines[1]
    assert "..." in lines[0]
    assert lines[0].endswith("s")
    assert "\r" not in out

    payload = json.loads((tmp_path / "reports" / "train_summary.json").read_text(encoding="utf-8"))
    assert sorted(payload["tickers"]) == ["GAZP", "SBER"]

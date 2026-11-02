"""tui.data: чистые функции чтения состояния демона (без Textual и терминала)."""
from __future__ import annotations

import datetime as dt
import json
import time

import pytest

from tui import data

NOW = dt.datetime(2026, 1, 1, 12, 0, tzinfo=dt.timezone.utc)


def _write(reports, name: str, text: str) -> None:
    path = reports / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _write_status(reports, **overrides) -> None:
    payload = {
        "pid": 4242,
        "state": "running",
        "started_at": "2026-01-01T10:00:00+00:00",
        "updated_at": "2026-01-01T11:59:00+00:00",
        "mode": "sandbox",
        "iteration": 7,
        "equity_rub": 1_000_000.0,
        "cash_rub": 900_000.0,
        "positions_count": 2,
        "kill_switch_active": False,
        "kill_switch_reason": None,
        "last_error": None,
    }
    payload.update(overrides)
    _write(reports, "daemon_status.json", json.dumps(payload, ensure_ascii=False))


def _write_heartbeat(reports, minutes_ago: float) -> None:
    stamp = NOW - dt.timedelta(minutes=minutes_ago)
    _write(reports, "heartbeat", stamp.isoformat())


# ---------- badge / daemon_state ----------

def test_missing_status_is_offline(tmp_path):
    state = data.daemon_state(tmp_path, now=NOW)
    assert state["status"] is None
    assert state["corrupt"] is False
    assert data.badge(state, now=NOW) == "offline"


def test_fresh_heartbeat_is_running(tmp_path):
    _write_heartbeat(tmp_path, 2)
    assert data.badge(data.daemon_state(tmp_path, now=NOW), now=NOW) == "running"


def test_six_minute_heartbeat_is_stale(tmp_path):
    _write_heartbeat(tmp_path, 6)
    assert data.badge(data.daemon_state(tmp_path, now=NOW), now=NOW) == "stale"


def test_twenty_one_minute_heartbeat_is_offline(tmp_path):
    _write_heartbeat(tmp_path, 21)
    assert data.badge(data.daemon_state(tmp_path, now=NOW), now=NOW) == "offline"


@pytest.mark.parametrize(
    ("age", "expected"),
    [
        (0.0, "running"),
        (5.0, "running"),  # граница включительно: 5.0 ещё running
        (5.01, "stale"),
        (20.0, "stale"),  # граница включительно: 20.0 ещё stale
        (20.01, "offline"),
    ],
)
def test_badge_boundaries(age, expected):
    assert data.badge({"heartbeat_age_min": age}) == expected


def test_corrupt_status_is_corrupt(tmp_path):
    _write(tmp_path, "daemon_status.json", "{broken")
    state = data.daemon_state(tmp_path, now=NOW)
    assert state["corrupt"] is True
    assert data.badge(state, now=NOW) == "corrupt"


# ---------- badge: daemon_status.json semantics (todo 9) ----------
# Вне сессии демон не пишет heartbeat, но daemon_status.json обновляется каждый тик:
# свежий статус со state=running — это running без heartbeat вообще.

def test_status_running_without_heartbeat_is_running(tmp_path):
    _write_status(tmp_path, updated_at=(NOW - dt.timedelta(seconds=30)).isoformat())
    state = data.daemon_state(tmp_path, now=NOW)
    assert state["status"] is not None
    assert data.badge(state, now=NOW) == "running"


def test_status_running_wins_over_stale_heartbeat(tmp_path):
    _write_status(tmp_path, updated_at=(NOW - dt.timedelta(seconds=30)).isoformat())
    _write_heartbeat(tmp_path, 30)  # по старой логике было бы offline
    assert data.badge(data.daemon_state(tmp_path, now=NOW), now=NOW) == "running"


def test_status_stopped_is_offline_even_with_fresh_heartbeat(tmp_path):
    _write_status(tmp_path, state="stopped", updated_at=NOW.isoformat())
    _write_heartbeat(tmp_path, 1)  # свежий heartbeat после мягкой остановки
    assert data.badge(data.daemon_state(tmp_path, now=NOW), now=NOW) == "offline"


def test_status_running_without_updated_at_is_stale(tmp_path):
    _write_status(tmp_path, updated_at=None)
    assert data.badge(data.daemon_state(tmp_path, now=NOW), now=NOW) == "stale"


def test_status_unknown_state_is_offline(tmp_path):
    _write_status(tmp_path, state="weird")
    assert data.badge(data.daemon_state(tmp_path, now=NOW), now=NOW) == "offline"


@pytest.mark.parametrize(
    ("age_sec", "expected"),
    [
        (0.0, "running"),
        (data.STATUS_FRESH_SEC, "running"),  # граница включительно
        (data.STATUS_FRESH_SEC + 0.01, "stale"),
        (1200.0, "stale"),  # 20 мин — граница STALE_MAX_MIN включительно
        (1200.01, "offline"),
    ],
)
def test_status_badge_boundaries(tmp_path, age_sec, expected):
    stamp = NOW - dt.timedelta(seconds=age_sec)
    _write_status(tmp_path, updated_at=stamp.isoformat())
    assert data.badge(data.daemon_state(tmp_path, now=NOW), now=NOW) == expected


def test_malformed_heartbeat_does_not_raise(tmp_path):
    _write(tmp_path, "heartbeat", "not-a-date")
    state = data.daemon_state(tmp_path, now=NOW)
    assert state["heartbeat_age_min"] is None
    assert data.badge(state, now=NOW) == "offline"


def test_daemon_state_merges_status_and_heartbeat_age(tmp_path):
    _write_status(tmp_path, iteration=11, equity_rub=None, cash_rub=None)
    _write_heartbeat(tmp_path, 3)
    state = data.daemon_state(tmp_path, now=NOW)
    assert state["state"] == "running"
    assert state["iteration"] == 11
    assert state["pid"] == 4242
    assert state["equity_rub"] is None
    assert state["source"] == "heartbeat"
    assert state["heartbeat_age_min"] == pytest.approx(3.0)


def test_equity_last_activity_used_when_no_heartbeat(tmp_path):
    _write(
        tmp_path,
        "equity_live.csv",
        "time,total_rub,cash_rub,positions_rub\n"
        f"{(NOW - dt.timedelta(minutes=4)).isoformat()},1000,900,100\n",
    )
    state = data.daemon_state(tmp_path, now=NOW)
    assert state["source"] == "equity_live.csv"
    assert data.badge(state, now=NOW) == "running"


# ---------- equity_last ----------

def test_equity_last_returns_last_row(tmp_path):
    _write(
        tmp_path,
        "equity_live.csv",
        "time,total_rub,cash_rub,positions_rub\n"
        "2026-01-01T10:00:00+00:00,1000.5,900.5,100.0\n"
        "2026-01-01T11:00:00+00:00,1100.5,950.5,150.0\n",
    )
    point = data.equity_last(tmp_path)
    assert point == {
        "time": "2026-01-01T11:00:00+00:00",
        "total_rub": 1100.5,
        "cash_rub": 950.5,
        "positions_rub": 150.0,
    }


@pytest.mark.parametrize(
    "text",
    [
        "",  # пустой файл
        "time,total_rub,cash_rub,positions_rub\n",  # только заголовок
        "time,total_rub,cash_rub\n2026-01-01T10:00:00+00:00,1,2\n",  # нет колонки
        "time,total_rub,cash_rub,positions_rub\n2026-01-01T10:00:00+00:00,x,2,3\n",  # не число
    ],
)
def test_equity_malformed_returns_none(tmp_path, text):
    _write(tmp_path, "equity_live.csv", text)
    assert data.equity_last(tmp_path) is None


def test_equity_missing_file_returns_none(tmp_path):
    assert data.equity_last(tmp_path) is None


# ---------- journal_tail ----------

def test_journal_tail_returns_last_20_of_25(tmp_path):
    lines = ["time,ticker,action,lots,price,reason,order_id"]
    for index in range(25):
        lines.append(f"2026-01-01T10:00:{index:02d}+00:00,T{index},BUY,1,100.0,test,o{index}")
    _write(tmp_path, "journal.csv", "\n".join(lines) + "\n")

    rows = data.journal_tail(tmp_path, 20)
    assert len(rows) == 20
    assert rows[0]["ticker"] == "T5"
    assert rows[-1]["ticker"] == "T24"


def test_journal_tail_missing_or_empty(tmp_path):
    assert data.journal_tail(tmp_path) == []
    _write(tmp_path, "journal.csv", "time,ticker,action,lots,price,reason,order_id\n")
    assert data.journal_tail(tmp_path) == []


# ---------- pending_orders_count ----------

def test_pending_orders_count_missing_is_zero(tmp_path):
    assert data.pending_orders_count(tmp_path) == 0


def test_pending_orders_count_counts_entries(tmp_path):
    _write(tmp_path, "pending_orders.json", json.dumps({"SBER": "1", "GAZP": "2"}))
    assert data.pending_orders_count(tmp_path) == 2


def test_pending_orders_corrupt_is_all(tmp_path):
    _write(tmp_path, "pending_orders.json", "{broken")
    assert data.pending_orders_count(tmp_path) == "all"


def test_pending_orders_fail_closed_sentinel_is_all(tmp_path):
    _write(tmp_path, "pending_orders.json", json.dumps({"*": "corrupt-pending-file"}))
    assert data.pending_orders_count(tmp_path) == "all"


def test_pending_orders_non_dict_is_all(tmp_path):
    _write(tmp_path, "pending_orders.json", json.dumps([1, 2, 3]))
    assert data.pending_orders_count(tmp_path) == "all"


# ---------- kill_switch_state ----------

def test_kill_switch_state_reads_dict(tmp_path):
    _write(tmp_path, "kill_switch.json", json.dumps({"reason": "drawdown", "active": True}))
    assert data.kill_switch_state(tmp_path) == {"reason": "drawdown", "active": True}


def test_kill_switch_state_missing_is_none(tmp_path):
    assert data.kill_switch_state(tmp_path) is None


def test_kill_switch_state_corrupt_is_flagged(tmp_path):
    _write(tmp_path, "kill_switch.json", "{broken")
    assert data.kill_switch_state(tmp_path) == {"corrupt": True}


# ---------- fix round: heartbeat decode (blocker) ----------

def test_non_utf8_heartbeat_does_not_raise(tmp_path):
    (tmp_path / "heartbeat").write_bytes(bytes([255, 254]))
    state = data.daemon_state(tmp_path, now=NOW)
    assert state["heartbeat_age_min"] is None
    assert data.badge(state, now=NOW) in {"stale", "offline"}


def test_non_utf8_heartbeat_with_equity_still_uses_equity(tmp_path):
    (tmp_path / "heartbeat").write_bytes(bytes([255, 254]))
    _write(
        tmp_path,
        "equity_live.csv",
        "time,total_rub,cash_rub,positions_rub\n"
        f"{(NOW - dt.timedelta(minutes=2)).isoformat()},1000,900,100\n",
    )
    state = data.daemon_state(tmp_path, now=NOW)
    assert data.badge(state, now=NOW) == "running"


# ---------- fix round: journal header validation ----------

def test_journal_tail_rejects_foreign_header(tmp_path):
    _write(tmp_path, "journal.csv", "a,b\n1,2\n")
    assert data.journal_tail(tmp_path) == []


# ---------- fix round: positions exposure ----------

def test_positions_defaults_to_empty():
    assert data.positions({}) == []
    assert data.positions({"positions": "junk"}) == []
    assert data.positions({"positions": [None, 1]}) == []


def test_positions_returns_dict_rows():
    rows = [
        {"ticker": "SBER", "lots": 10, "price": 110.0, "pnl_rub": 100.0},
        {"ticker": "GAZP", "lots": 2, "price": 250.0, "pnl_rub": -10.0},
    ]
    assert data.positions({"positions": rows}) == rows


# ---------- fix round: session P&L ----------

def test_session_pnl_single_point_is_none(tmp_path):
    _write(
        tmp_path,
        "equity_live.csv",
        "time,total_rub,cash_rub,positions_rub\n"
        "2026-01-01T10:00:00+00:00,1000,900,100\n",
    )
    assert data.session_pnl(tmp_path) is None


def test_session_pnl_two_points(tmp_path):
    _write(
        tmp_path,
        "equity_live.csv",
        "time,total_rub,cash_rub,positions_rub\n"
        "2026-01-01T10:00:00+00:00,1000,900,100\n"
        "2026-01-01T11:00:00+00:00,1012,900,112\n",
    )
    assert data.session_pnl(tmp_path) == {"abs_rub": 12.0, "pct": pytest.approx(0.012)}


def test_session_pnl_missing_or_empty_is_none(tmp_path):
    assert data.session_pnl(tmp_path) is None
    _write(tmp_path, "equity_live.csv", "time,total_rub,cash_rub,positions_rub\n")
    assert data.session_pnl(tmp_path) is None
    _write(tmp_path, "equity_live.csv", "time,total_rub,cash_rub,positions_rub\n1000,1,2,3\n")
    assert data.session_pnl(tmp_path) is None


# ---------- tail_log (todo 9) ----------

def test_tail_log_missing_file_is_empty(tmp_path):
    assert data.tail_log(tmp_path / "missing.log") == []


def test_tail_log_empty_file_is_empty(tmp_path):
    (tmp_path / "empty.log").write_text("", encoding="utf-8")
    assert data.tail_log(tmp_path / "empty.log") == []


def test_tail_log_returns_last_n_lines(tmp_path):
    path = tmp_path / "app.log"
    path.write_text("".join(f"line {index:03d}\n" for index in range(40)), encoding="utf-8")
    lines = data.tail_log(path, 30)
    assert len(lines) == 30
    assert lines[0] == "line 010"
    assert lines[-1] == "line 039"


def test_tail_log_binary_junk_does_not_raise(tmp_path):
    path = tmp_path / "binary.log"
    path.write_bytes(bytes([0xFF, 0xFE, 0x00, 0x01, 0x0A]) + b"tail\n")
    assert data.tail_log(path, 30) == []


def test_tail_log_non_nul_binary_is_empty(tmp_path):
    # 0x80..0xFF без NUL и без переводов строк — невалидный UTF-8, не текст.
    path = tmp_path / "binary-no-nul.log"
    path.write_bytes(bytes([0x80, 0x81, 0xFF, 0xFE, 0xC3]))
    assert data.tail_log(path, 30) == []


def test_tail_log_multibyte_split_at_block_boundary(tmp_path):
    # Строки по 10 001 байт: граница 8192-байтного блока приходится на
    # середину двухбайтной «Ж»; хвост обязан декодироваться без потерь.
    path = tmp_path / "cyrillic.log"
    line = "Ж" * 5000
    path.write_text("\n".join([line] * 40) + "\n", encoding="utf-8")
    lines = data.tail_log(path, 30)
    assert len(lines) == 30
    assert lines[0] == line
    assert lines[-1] == line
    assert "\ufffd" not in "\n".join(lines)


def test_tail_log_10mb_file_is_fast_and_exact(tmp_path):
    path = tmp_path / "big.log"
    # newline="\n" отключает CRLF-трансляцию на Windows: размер одинаков
    # на Windows и POSIX (1_000_000 * 13 байт = 13_000_000 > 10 MiB).
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("".join(f"line {index:07d}\n" for index in range(1_000_000)))
    assert path.stat().st_size > 10 * 1024 * 1024

    started = time.monotonic()
    lines = data.tail_log(path, 30)
    elapsed = time.monotonic() - started

    assert len(lines) == 30
    assert lines[0] == "line 0999970"
    assert lines[-1] == "line 0999999"
    assert elapsed < 1.0

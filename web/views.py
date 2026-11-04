"""HTML rendering for the read-only web panel (T4).

No template engine: every dynamic value passes through :func:`esc`. The page
mirrors the Textual dashboard layout (mode banner, daemon card, positions,
journal, log tail, status line) and uses the same Russian labels as
``tui/app.py``. The dashboard script is external (CSP ``script-src 'self'``).
"""
from __future__ import annotations

from web.view_format import (
    UNKNOWN,
    badge_class,
    dash,
    esc,
    fmt_age,
    fmt_rub,
    fmt_session_pnl,
    fmt_signed_rub,
    kill_switch_text,
    mode_text,
    refresh_ms,
)

_STYLE = """
:root { color-scheme: dark; }
* { box-sizing: border-box; }
body { margin: 0; padding: 16px; background: #101418; color: #e6e6e6;
       font: 14px/1.45 "Segoe UI", system-ui, sans-serif; }
h1 { font-size: 20px; margin: 0 0 12px; }
h2 { font-size: 16px; margin: 0 0 8px; }
h3.section { font-size: 13px; letter-spacing: .08em; margin: 18px 0 8px; color: #9fb3c8; }
#mode { display: inline-block; padding: 6px 12px; margin-bottom: 14px;
        border: 1px solid #33506b; border-radius: 6px; background: #16202a; font-weight: 600; }
.mode-real { border-color: #8b2f2f; background: #2a1414; color: #ff9c9c; }
#body { display: flex; flex-wrap: wrap; gap: 18px; align-items: flex-start; }
#left, #right { flex: 1 1 420px; min-width: 320px; }
.card { border: 1px solid #26313b; border-radius: 8px; padding: 12px 14px; background: #151b21; }
.card p { margin: 3px 0; }
table { width: 100%; border-collapse: collapse; background: #151b21; }
th, td { border: 1px solid #26313b; padding: 5px 8px; text-align: left; vertical-align: top; }
th { color: #9fb3c8; font-weight: 600; }
#log { min-height: 160px; max-height: 340px; overflow: auto; margin: 0;
       padding: 10px 12px; border: 1px solid #26313b; border-radius: 8px;
       background: #0b0f13; white-space: pre-wrap; word-break: break-word; }
#status { margin-top: 12px; color: #8aa0b4; }
.flash { margin: 12px 0 0; padding: 8px 12px; border: 1px solid #33506b; border-radius: 6px;
         background: #16202a; color: #cfe3f5; }
.badge { padding: 1px 8px; border-radius: 999px; font-size: 12px; text-transform: uppercase; }
.badge-running { background: #14351f; color: #6fdc8c; }
.badge-stale { background: #3a2f12; color: #f0c419; }
.badge-offline { background: #2b2b2b; color: #a8a8a8; }
.badge-corrupt { background: #3a1616; color: #ff8a8a; }
.logout { margin-top: 14px; }
button { padding: 6px 14px; border-radius: 6px; border: 1px solid #3b4a58;
         background: #1c2732; color: #e6e6e6; cursor: pointer; }
button:hover { background: #24323f; }
.login-page { display: flex; justify-content: center; align-items: center; min-height: 100vh; }
.login-page main { width: 100%; max-width: 360px; border: 1px solid #26313b;
                   border-radius: 10px; padding: 22px; background: #151b21; }
.login-page label { display: block; margin: 12px 0 4px; color: #9fb3c8; }
.login-page input { width: 100%; padding: 8px 10px; border-radius: 6px;
                    border: 1px solid #3b4a58; background: #0b0f13; color: #e6e6e6; }
.login-page button { width: 100%; margin-top: 16px; }
.error { padding: 10px 12px; border: 1px solid #7a2c2c; border-radius: 6px;
         background: #2a1414; color: #ffb4b4; }
"""


def _position_rows(positions: list) -> str:
    rows = []
    for row in positions:
        if not isinstance(row, dict):
            continue
        rows.append(
            "<tr>"
            f"<td>{esc(row.get('ticker'))}</td>"
            f"<td>{esc(row.get('lots'))}</td>"
            f"<td>{esc(fmt_rub(row.get('price')))}</td>"
            f"<td>{esc(fmt_signed_rub(row.get('pnl_rub')))}</td>"
            "</tr>"
        )
    return "".join(rows)


def _journal_rows(journal: list) -> str:
    rows = []
    for row in journal:
        if not isinstance(row, dict):
            continue
        rows.append(
            "<tr>"
            f"<td>{esc(row.get('time'))}</td>"
            f"<td>{esc(row.get('ticker'))}</td>"
            f"<td>{esc(row.get('action'))}</td>"
            f"<td>{esc(row.get('lots'))}</td>"
            f"<td>{esc(row.get('price'))}</td>"
            "</tr>"
        )
    return "".join(rows)


def login_page(csrf: str, error: str | None = None) -> str:
    """Login form; the CSRF token is issued through the server-side session."""
    error_block = (
        f'<p class="error" role="alert">{esc(error)}</p>' if error else ""
    )
    return f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Вход — торговый бот</title>
<style>{_STYLE}</style>
</head>
<body class="login-page">
<main>
<h1>ВХОД В ПАНЕЛЬ</h1>
{error_block}
<form method="post" action="/login">
<input type="hidden" name="csrf_token" value="{esc(csrf)}">
<label for="username">Логин</label>
<input id="username" name="username" type="text" autocomplete="username" required>
<label for="password">Пароль</label>
<input id="password" name="password" type="password" autocomplete="current-password" required>
<button type="submit">Войти</button>
</form>
</main>
</body>
</html>"""


def error_page(message: str) -> str:
    """Standalone error page for CSRF/throttle refusals (no secrets echoed)."""
    return f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Ошибка — торговый бот</title>
<style>{_STYLE}</style>
</head>
<body>
<main class="card">
<h1>ЗАПРОС ОТКЛОНЁН</h1>
<p class="error" role="alert">{esc(message)}</p>
<p><a href="/login">Вернуться ко входу</a></p>
</main>
</body>
</html>"""


def dashboard_page(config, snapshot: dict, csrf: str, flash: str | None = None) -> str:
    """Dashboard HTML built from one ``build_snapshot`` result.

    ``flash`` is the one-shot control outcome message; it is escaped like every
    other dynamic value and rendered on its own line next to the status line
    (the refresh script rewrites ``#status`` and must not erase it).
    """
    daemon = snapshot.get("daemon") if isinstance(snapshot.get("daemon"), dict) else {}
    positions = snapshot.get("positions") if isinstance(snapshot.get("positions"), list) else []
    journal = snapshot.get("journal") if isinstance(snapshot.get("journal"), list) else []
    log_lines = snapshot.get("log") if isinstance(snapshot.get("log"), list) else []
    equity_point = snapshot.get("equity") if isinstance(snapshot.get("equity"), dict) else {}

    equity = daemon.get("equity_rub")
    cash = daemon.get("cash_rub")
    if equity is None:
        equity = equity_point.get("total_rub")
    if cash is None:
        cash = equity_point.get("cash_rub")
    positions_count = daemon.get("positions_count")
    if positions_count is None:
        positions_count = len(positions)

    banner, banner_class = mode_text(config)
    badge_value = snapshot.get("badge")
    badge_css = badge_class(badge_value)
    empty_style = "none" if positions else "block"
    log_text = "\n".join(str(line) for line in log_lines) or "лог пуст"
    generated_at = dash(snapshot.get("generated_at"))
    flash_block = f'<p id="flash" class="flash" role="status">{esc(flash)}</p>' if flash else ""

    return f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Торговый бот — панель</title>
<style>{_STYLE}</style>
</head>
<body data-refresh-ms="{refresh_ms(config)}">
<div id="mode" class="{banner_class}">{banner}</div>
<main id="body">
<section id="left">
<div class="card">
<h2>ДЕМОН: <span id="badge" class="badge badge-{esc(badge_css)}">{esc(badge_value) or UNKNOWN}</span></h2>
<p>состояние: <span id="state">{dash(daemon.get('state'))}</span></p>
<p>pid: <span id="pid">{dash(daemon.get('pid'))}</span></p>
<p>итерация: <span id="iteration">{dash(daemon.get('iteration'))}</span></p>
<p>обновлён: <span id="updated">{dash(daemon.get('updated_at'))}</span></p>
<p>heartbeat: <span id="heartbeat">{esc(fmt_age(daemon.get('heartbeat_age_min')))}</span></p>
<p>последняя сделка: <span id="last-trade">{esc(fmt_age(snapshot.get('journal_last_age_min')))}</span></p>
<p>капитал: <span id="equity">{esc(fmt_rub(equity))}</span></p>
<p>кэш: <span id="cash">{esc(fmt_rub(cash))}</span></p>
<p>P&L сессии: <span id="pnl">{esc(fmt_session_pnl(snapshot.get('session_pnl')))}</span></p>
<p>позиций: <span id="positions-count">{dash(positions_count)}</span></p>
<p>заявок в работе: <span id="pending">{dash(snapshot.get('pending_orders'))}</span></p>
<p>kill-switch: <span id="kill-switch">{esc(kill_switch_text(snapshot.get('kill_switch')))}</span></p>
</div>
<h3 class="section">ПОЗИЦИИ</h3>
<table>
<thead><tr><th>тикер</th><th>лоты</th><th>цена</th><th>PnL</th></tr></thead>
<tbody id="positions-body">{_position_rows(positions)}</tbody>
</table>
<p id="positions-empty" style="display: {empty_style}">позиции: нет данных (вне сессии)</p>
</section>
<section id="right">
<h3 class="section">ЖУРНАЛ СДЕЛОК</h3>
<table>
<thead><tr><th>время</th><th>тикер</th><th>действие</th><th>лоты</th><th>цена</th></tr></thead>
<tbody id="journal-body">{_journal_rows(journal)}</tbody>
</table>
</section>
</main>
<h3 class="section">ЛОГ</h3>
<pre id="log">{esc(log_text)}</pre>
{flash_block}
<div id="status">обновлено: {generated_at}</div>
<form method="post" action="/logout" class="logout">
<input type="hidden" name="csrf_token" value="{esc(csrf)}">
<button type="submit">Выйти</button>
</form>
<script src="/static/dashboard.js"></script>
</body>
</html>"""

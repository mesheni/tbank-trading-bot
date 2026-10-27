# C3 — CLI/UI/reporting evidence

## Findings

### F-C3.1 [High] report hint contradicts normalize behavior
- **File:** `cli.py:613-618, 1075-1080`
- **Severity:** High
- **Confirmed:** Yes
- **Description:** `cmd_report` at line 613-618 tells user "Выполните `python cli.py normalize`: излишек будет выведен и внесён в учёт" (Run normalize: excess will be withdrawn). But `cmd_normalize` at line 1075-1080 explicitly says withdrawal is impossible ("вывода из песочницы в API нет") and points to `reset-sandbox` instead. Two user-facing surfaces contradict each other.
- **Impact:** User confusion; incorrect expectation about normalize behavior.

### F-C3.2 [High] reset-sandbox input() crashes in non-TTY
- **File:** `cli.py:1128-1132`
- **Severity:** High
- **Confirmed:** Yes
- **Description:** `input("  Подтвердите (YES): ")` at line 1129 without EOFError handling. Cron/CI invocation without `--yes` produces Python traceback instead of a clear "use --yes" message.
- **Reproduction:** Run `cli.py reset-sandbox` in a cron job → `input()` raises EOFError → uncaught → traceback printed.
- **Impact:** Poor operator experience; unhelpful error message.

### F-C3.3 [High] main() has no top-level exception handler
- **File:** `cli.py:1216-1259`
- **Severity:** High
- **Confirmed:** Yes
- **Description:** `main()` dispatches commands without try/except. API errors in `cmd_report` (line 574, `api.get_accounts()`), `compose_digest` (line 948), `cmd_smoke` all produce raw Python tracebacks. Users see `requests.exceptions.ConnectionError` instead of "нет сети — проверьте подключение".
- **Impact:** Unhelpful error messages for common failure modes.

### F-C3.4 [High] smoke reports success despite partial failure
- **File:** `cli.py:170-175`
- **Severity:** High
- **Confirmed:** Yes
- **Description:** `cmd_smoke` catches news fetch failure at line 170-171 (`log.warning`) but still calls `ok("Smoke-тест пройден")` at line 175 and returns 0. Partial failure is reported as success.
- **Impact:** False positive in CI/health checks.

### F-C3.5 [High] status misses real-mode budget warning
- **File:** `cli.py:585-587, 786, 963-965`
- **Severity:** High
- **Confirmed:** Yes
- **Description:** `cmd_report` (line 585) and `compose_digest` (line 963) call `_budget_base_warning()` and display it when `MODE=real` without `ACCOUNT_BUDGET_RUB`. `cmd_status` at line 786 prints `config.budget_rub` without this check. The user sees "бюджет 1 000 000" in real mode, which is the sandbox default — not the real capital.
- **Impact:** Misleading budget display in real mode.

### F-C3.6 [High] No confirmation for MODE=real run (UI aspect)
- **File:** `cli.py:465-482`
- **Severity:** High
- **Confirmed:** Yes
- **Description:** `cmd_run` starts real trading without interactive confirmation. A user who accidentally sets `MODE=real` in `.env` gets immediate live trading with no "are you sure?" prompt.
- **Impact:** Accidental real-money trading without warning.

### F-C3.7 [Medium] No progress indicator for long operations
- **File:** `cli.py:179-189` (download), `cli.py:262-349` (train)
- **Severity:** Medium
- **Confirmed:** Yes
- **Description:** `cmd_download` processes 16 tickers × 720 days in chunks (per `tbank/market_data.py:123-129`) with only per-ticker log lines. No `[i/N]` counter, no elapsed/remaining estimate. `cmd_train` takes 15-25 min (`README.md:237-240`) with no progress feedback.
- **Impact:** Poor operator experience; no indication of progress or ETA.

### F-C3.8 [Medium] Digest has no coverage/duration notes
- **File:** `cli.py:945-1008`
- **Severity:** Medium
- **Confirmed:** Yes
- **Description:** `compose_digest` omits period coverage notes that `cmd_report` includes (via `_period_pnl`). Subject line is static ("дайджест дня") with no date — email clients thread all digests together. Body has no indication of how many days of data are covered.
- **Impact:** Confusing email thread; no temporal context.

### F-C3.9 [Medium] send_throttled in-memory vs watchdog file marker inconsistency
- **File:** `notify.py:58, 90-102`, `cli.py:930-940`
- **Severity:** Medium
- **Confirmed:** Yes
- **Description:** `Notifier._last_sent` is an in-memory dict (line 58). Process restart resets all throttle state. Watchdog uses a durable file marker `watchdog_last_alert` (line 930-940). Inconsistent durability: bot's iteration-error throttle resets after every restart (potential email spam), while watchdog throttle persists.
- **Impact:** Inconsistent dedup behavior across notification types.

### F-C3.10 [Medium] warn() goes to stdout, other errors to stderr
- **File:** `cli.py:106-107, 1057, 1109, 1213, 1227`
- **Severity:** Medium
- **Confirmed:** Yes
- **Description:** `warn()` at line 106-107 uses bare `print()` (stdout). Other error paths (lines 1057, 1109, 1213, 1227) use `print(..., file=sys.stderr)`. Warnings disappear when users capture only stderr (e.g., `2>errors.log`).
- **Impact:** Warnings lost in common logging setups.

### F-C3.11 [Medium] Ambiguous P&L period heading
- **File:** `cli.py:620, 563-567`
- **Severity:** Medium
- **Confirmed:** Yes
- **Description:** Line 620 prints `"P&L ПО ПЕРИОДАМ (без учёта пополнений/выводов)"` but `_period_pnl` at lines 563-567 actually subtracts flows (`pnl = (last - base) - moved`). The label says "without accounting for flows" but the code does account for them. Better wording: "с поправкой на движения денег".
- **Impact:** Misleading report header.

### F-C3.12 [Medium] Encoding mojibake in launchers/service/cron
- **File:** `tbank.bat`, `tbank.sh`, `deploy/tbank-bot.service`, `deploy/tbank-bot.cron`
- **Severity:** Medium
- **Confirmed:** Yes
- **Description:** These files contain Cyrillic comments that render as garbage under the default Windows code page (CP-1251/CP-866). Users on Windows see unreadable instructions on first run.
- **Impact:** Poor first-run experience on Windows.

### F-C3.13 [Medium] No --json/--plain/--quiet/--dry-run/--no-color flags
- **File:** `cli.py:1154-1194`, `ui.py:44-57`
- **Severity:** Medium
- **Confirmed:** Yes
- **Description:** Parser has no machine-readable output flag. Color control only via environment variables (`NO_COLOR`, `FORCE_COLOR`) which are undiscoverable from `--help`. No `--quiet` for cron, no `--dry-run` for testing, no `--json` for automation.
- **Impact:** Poor automation/scripting support.

### F-C3.14 [Low] README test count drift
- **File:** `README.md:320`
- **Severity:** Low
- **Confirmed:** Yes
- **Description:** README claims "181 офлайн-тест" but the repo contains 182 `def test_*` functions (verified by grep). Minor doc drift.
- **Impact:** Documentation inaccuracy.

### F-C3.15 [Low] ui.header docstring mismatch
- **File:** `ui.py:160-163`
- **Severity:** Low
- **Confirmed:** Yes
- **Description:** Docstring says `'── Заголовок ──────────────'` (box-drawing) but code emits `"-- {title} "` (ASCII dashes). Cosmetic.
- **Impact:** Documentation inaccuracy.

### F-C3.16 [Low] ui.fmt_money uses thin space U+2009
- **File:** `ui.py:109-111`
- **Severity:** Low
- **Confirmed:** Yes
- **Description:** When colors are enabled, `fmt_money` replaces commas with U+2009 (THIN SPACE). This can break copy-paste to Excel/spreadsheet (thin space may not parse as number separator). Conditioned on `color_enabled()` of stdout.
- **Impact:** Minor usability issue when copying amounts.

### F-C3.17 [Low] Two metrics files with same basename, different schemas
- **File:** `models/registry.py:445-453`, `backtest.py:249`
- **Severity:** Low
- **Confirmed:** Yes
- **Description:** Same finding as F-C2.8. `models_artifacts/metrics_*.json` (train) and `reports/metrics_*.json` (backtest) share the same naming pattern but have different schemas. Easy to misread.
- **Impact:** Usability hazard.

## Test coverage gaps (from C3 inventory)

| Surface | Test file | Covered? |
|---|---|---|
| ui.py paint/fmt/table | tests/test_ui.py | Yes |
| CLI parser/env/status/setup | tests/test_usability.py | Yes |
| Notifier disabled/SSL/fail | tests/test_monitoring.py | Partial |
| Watchdog fresh/stale | tests/test_monitoring.py | Yes |
| **cmd_report** | — | **No** |
| **compose_digest / cmd_digest** | — | **No** |
| **cmd_download** | — | **No** |
| **cmd_news** | — | **No** |
| **cmd_smoke** | — | **No** |
| **ui._detect env/isatty/VT** | — | **No** |
| **STARTTLS (port 587)** | — | **No** |
| **bot.run_forever loop** | — | **No** |
| **Kill-switch email content** | tests/test_risk_ml.py | Partial (state only) |
| **reset-sandbox --yes path** | tests/test_trader.py | Partial |
| **main() dispatch errors** | — | **No** |

## Summary
- 5 High findings (normalize contradiction, EOFError, no exception handler, smoke success-on-fail, status warning gap)
- 7 Medium findings (progress, digest, throttle inconsistency, warn stream, P&L heading, encoding, no flags)
- 4 Low findings (README drift, docstring, thin space, metrics naming)
- 14 test coverage gaps identified
- Total: 17 findings (including coverage gaps)

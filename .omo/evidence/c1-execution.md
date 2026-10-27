# C1 — Trading execution & risk evidence

## Findings

### F-C1.1 [Critical] Order cancellation leaves unconfirmed state
- **File:** `tbank/trader.py:104-152`
- **Severity:** Critical
- **Confirmed:** Yes
- **Description:** `_execute()` polls `get_order_state` up to 3 times (2s each), then cancels if status not final. If cancel fails, it logs a warning and continues. No reconciliation on the next cycle: an uncancelled open order from the previous iteration could be filled later, creating an unexpected position. The next `step_ticker` call has no knowledge of prior open orders.
- **Reproduction:** Network timeout during `cancel_order` → order remains active on exchange → next cycle's `decide()` sees no local position → submits another order → double exposure.
- **Impact:** Double position or unintended trade execution.

### F-C1.2 [High] Kill-switch resets on restart
- **File:** `bot.py:171-172, 525-543`
- **Severity:** High
- **Confirmed:** Yes
- **Description:** `_entries_allowed` is initialized to `True` in `__init__`. After crash or restart, the drawdown gate is lost. The bot resumes trading immediately even if drawdown was the reason for the previous stop. No persistent marker (file or DB) records the kill-switch state.
- **Reproduction:** Bot hits MAX_DRAWDOWN_PCT → kills entries → crashes → restarts → `_entries_allowed = True` → resumes trading below budget threshold.
- **Impact:** Loss of capital protection after restart.

### F-C1.3 [High] Shared throttle keys suppress BUY/SELL alerts
- **File:** `bot.py:367, 376, 424, 436`, `notify.py:90-102`
- **Severity:** High
- **Confirmed:** Yes
- **Description:** Both BUY and SELL use the same throttle keys: `partial-fill:{ticker}` and `order-not-filled:{ticker}`. A rejected/partial BUY silences the corresponding SELL alert for 30 minutes. This directly undermines the stop-loss escalation documented at `bot.py:406-421` — if a SELL (stop-loss) is rejected, the alert may be suppressed by a prior BUY rejection.
- **Reproduction:** BUY order rejected at T=0 → throttle key `order-not-filled:SBER` set. At T=5min, SELL (stop-loss) rejected → alert suppressed because same key was used 5 min ago (< 30 min).
- **Impact:** Critical trading alerts silently lost.

### F-C1.4 [High] ORDER_FALLBACK_TO_MARKET dangerous in real mode
- **File:** `.env.example:102-105`, `tbank/api.py:340-380`
- **Severity:** High
- **Confirmed:** Yes
- **Description:** Default `ORDER_FALLBACK_TO_MARKET=1` in `.env.example`. `post_order()` catches HTTP 400 on limit orders and retries as market order. For `MODE=real`, a stale limit price becomes an uncontrolled market order. The README warns but there is no runtime guard in `bot.py` or `cli.py` that prevents this in real mode.
- **Reproduction:** `MODE=real` with `ORDER_FALLBACK_TO_MARKET=1` → limit order rejected (price stale) → automatically resubmitted as market order → fills at unexpected price.
- **Impact:** Uncontrolled trade execution at market price.

### F-C1.5 [High] No candle freshness guard in live loop
- **File:** `bot.py:185-201`
- **Severity:** High
- **Confirmed:** Yes
- **Description:** `refresh_candles` catches ALL exceptions and falls back to the DB cache without checking data age. If the API is down for hours, the bot continues trading on stale prices. No staleness threshold, no alert, no halt.
- **Reproduction:** API down for 2 hours → `download_candles` fails → exception caught → `load_candles` returns 2-hour-old data → bot trades on stale prices.
- **Impact:** Trading on outdated market data.

### F-C1.6 [High] No confirmation for MODE=real run
- **File:** `cli.py:465-482`
- **Severity:** High
- **Confirmed:** Yes
- **Description:** `cmd_run` starts real trading immediately without interactive confirmation when `MODE=real`. A misconfigured `.env` or accidental mode switch leads to immediate live trading.
- **Impact:** Accidental real-money trading.

### F-C1.7 [Medium] Silent account selection in real mode
- **File:** `cli.py:117-124`
- **Severity:** Medium
- **Confirmed:** Yes
- **Description:** `_pick_account` returns `accounts[0]["id"]` when `ACCOUNT_ID` is unset. In real mode with multiple accounts (brokerage, IIS), this silently picks the first account without warning.
- **Impact:** Trading on wrong account.

### F-C1.8 [Medium] Offline commands bypass config.validate()
- **File:** `cli.py:1150-1151, 1221-1225`
- **Severity:** Medium
- **Confirmed:** Yes
- **Description:** `MODE_ONLY_COMMANDS` skips `config.validate()` for `train/backtest/status/send-test-email`. `backtest` will run with `STOP_LOSS_PCT=5` (invalid, should be 0-1) while `run` would refuse.
- **Impact:** Silent misconfiguration produces misleading backtest results.

## Summary
- 1 Critical finding (order reconciliation gap)
- 5 High findings (kill-switch reset, throttle collision, market fallback, stale data, no real-mode confirmation)
- 2 Medium findings (silent account pick, validation bypass)
- Total: 8 findings

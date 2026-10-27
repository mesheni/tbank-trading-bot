# C4 — Reliability/security/deploy evidence

## Findings

### F-C4.1 [High] No candle freshness guard in live loop
- **File:** `bot.py:185-201`
- **Severity:** High
- **Confirmed:** Yes
- **Description:** `refresh_candles` at line 185-201 catches ALL exceptions from `download_candles` and falls back to `load_candles` from DB. No staleness check: if the API has been down for hours, the bot trades on old data. No alert, no halt.
- **Reproduction:** API unreachable for 3 hours → `download_candles` raises → exception caught → `load_candles` returns 3-hour-old candles → bot makes decisions on stale data.
- **Impact:** Trading on outdated prices.

### F-C4.2 [High] Kill-switch not persisted
- **File:** `bot.py:171-172, 525-543`
- **Severity:** High
- **Confirmed:** Yes
- **Description:** `_entries_allowed` is initialized to `True` at line 172. `_check_kill_switch` at line 525-543 sets it to `False` on drawdown breach but only in memory. No file/DB marker. Restart resets the gate.
- **Reproduction:** Bot hits drawdown → kill-switch fires → bot crashes (OOM, network) → restart → `_entries_allowed = True` → resumes trading below budget.
- **Impact:** Capital protection lost on restart.

### F-C4.3 [High] ORDER_FALLBACK_TO_MARKET safety
- **File:** `.env.example:102-105`, `tbank/api.py:340-380`
- **Severity:** High
- **Confirmed:** Yes
- **Description:** Default is `ORDER_FALLBACK_TO_MARKET=1`. `post_order` at `api.py:361-379` catches HTTP 400 on limit orders and retries as market. README warns to set to 0 for real mode, but no runtime guard prevents this in `MODE=real`. If a user copies the default `.env.example` to `.env` and switches to `MODE=real`, market fallback is active.
- **Impact:** Uncontrolled market order execution.

### F-C4.4 [High] Token exposure risk in .env
- **File:** `.env.example`, `.gitignore`
- **Severity:** High (potential)
- **Hypothesis:** `.gitignore` includes `.env` (line 4) — good. But `.env.example` at line 4 has `T_INVEST_TOKEN=` with no value. If a user accidentally pastes the real token into `.env.example` and commits, the token is exposed. No pre-commit hook or CI check prevents this.
- **Impact:** Token leakage via accidental commit.

### F-C4.5 [Medium] SQLite WAL + busy_timeout but no connection pooling
- **File:** `tbank/market_data.py:18-96`
- **Severity:** Medium
- **Confirmed:** Yes
- **Description:** `connect()` at line 18-96 enables WAL mode and 5s busy_timeout. Good for concurrent readers. But the bot holds a single connection (`bot.py:152`) and cron jobs (download/train) may write to the same DB. `DEPLOY.md:409` warns about "database is locked" if `download` runs in parallel with the bot. No connection retry or advisory lock.
- **Impact:** Potential DB lock contention during cron retrain.

### F-C4.6 [Medium] Log rotation configured but no external log management
- **File:** `cli.py:1197-1213`
- **Severity:** Medium
- **Confirmed:** Yes
- **Description:** `RotatingFileHandler` at line 1203-1206 rotates at `log_max_bytes` (default 10MB) with 5 backups. Total max log = 60MB. No external log management (logrotate, journald size limit). On a long-running server, 60MB is manageable but there's no alert if disk fills.
- **Impact:** Minor; disk space not monitored.

### F-C4.7 [Medium] systemd RestartSec=30 but no startup probe
- **File:** `deploy/tbank-bot.service:13-14`
- **Severity:** Medium
- **Confirmed:** Yes
- **Description:** `RestartSec=30` means systemd waits 30s before restarting. No `ExecStartPre` to validate config/token before starting. A misconfigured `.env` causes immediate crash → restart → crash loop (every 30s). The watchdog at `cli.py:884-942` can detect staleness but only after 20 min.
- **Impact:** 20-minute gap before crash-loop detected.

### F-C4.9 [Medium] API rate limiting: 0.25s between requests, 3 retries
- **File:** `tbank/rest.py:140-235`
- **Severity:** Medium
- **Confirmed:** Yes
- **Description:** `_throttle()` enforces 0.25s between requests (line 145, `min_request_interval=0.25`). Retries at line 199-235: 3 attempts with linear backoff (1.5s × attempt). On 429, reads `Retry-After` header. On 5xx, retries with backoff. On 4xx (other than 400 handled by fallback), raises immediately. No circuit breaker for sustained failures.
- **Impact:** Potential rapid retry storm during extended API outage.

### F-C4.10 [Medium] No health check endpoint or structured status
- **File:** —
- **Severity:** Medium
- **Confirmed:** Yes
- **Description:** No HTTP health check endpoint. Monitoring relies on heartbeat file (`bot.py:108-111`) checked by `cmd_watchdog` (`cli.py:884-942`). No Prometheus metrics, no structured JSON status. External monitoring tools cannot easily integrate.
- **Impact:** Limited monitoring integration options.

### F-C4.11 [Low] .gitignore includes *.cer/*.crt/*.pem but not *.key
- **File:** `.gitignore:6-8`
- **Severity:** Low
- **Confirmed:** Yes
- **Description:** `.gitignore` excludes certificate files but not private keys (`.key`). If a user generates a private key in the project directory, it could be committed. Low risk since the bot doesn't use private keys.
- **Impact:** Theoretical key exposure.

### F-C4.12 [Low] deploy/install.sh runs apt-get as root
- **File:** `deploy/install.sh:18-24`
- **Severity:** Low
- **Confirmed:** Yes
- **Description:** `install.sh` checks `id -u == 0` and runs `apt-get install -y python3-venv python3-pip libgomp1`. Standard for VPS deployment. No sudo hardening or package pinning. Low risk for typical VPS use.
- **Impact:** Minimal; standard practice.

## Summary
- 4 High findings (candle freshness, kill-switch persistence, market fallback, token exposure)
- 5 Medium findings (SQLite locking, log rotation, systemd probe, rate limiting, health check)
- 2 Low findings (gitignore keys, install.sh root)
- Total: 11 findings

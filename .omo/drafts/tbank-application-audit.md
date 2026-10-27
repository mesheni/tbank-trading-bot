---
slug: tbank-application-audit
status: awaiting-approval
intent: unclear
pending-action: .omo/plans/tbank-application-audit.md written
approach: Read-only audit producing a prioritised Russian-language report covering trading execution/risk, data/ML/backtest, CLI/UI, reliability/security, test coverage. No code edits, no live API, no broker interaction.
---

# Draft: tbank-application-audit

## Components (topology ledger)

| id | outcome (one line) | status | evidence path |
|---|---|---|---|
| C1 | Trading execution/risk correctness | active | .omo/evidence/c1-execution.md |
| C2 | Data/ML/backtest pipeline correctness | active | .omo/evidence/c2-ml.md |
| C3 | CLI/UI/reporting quality | active | .omo/evidence/c3-ui.md |
| C4 | Reliability/security/deploy | active | .omo/evidence/c4-ops.md |
| C5 | Test coverage completeness | active | .omo/evidence/c5-tests.md |

## Open assumptions (announced defaults)

| assumption | adopted default | rationale | reversible? |
|---|---|---|---|
| Audit deliverable is a written report, not code fixes | report only | user asked for analysis; fixes are a separate scope | yes |
| No live broker interaction | read-only, offline only | safety; sandbox token may not be present | yes |
| Findings are prioritised by plausible impact, not confirmed bug only | include hypotheses marked as such | audit value comes from surfacing risk, not just confirmed defects | no |
| Report language | Russian | user request is in Russian | yes |
| Test baseline | pytest --collect-only + static analysis | venv not present; avoid installing deps | yes |

## Findings (cited - path:lines)

### Critical / High

1. **Live LGBM prediction uses stale features** — `bot.py:276-292` calls `features.iloc[-1]`, but `features.py:95-97` drops last `horizon` rows (NaN forward target). For horizon H the feature row is H bars behind the actual latest price. Backtest walk-forward uses the same truncated features but aligns predictions to feature-index timestamps, so live and backtest use different feature snapshots. Needs confirmation by running a synthetic case.
   - Files: `bot.py:276-292`, `features.py:42-98`, `models/registry.py:211-241`

2. **Backtest vs live execution-price semantics** — `backtest.py:68-116` applies `exec_price = price * (1 + slip)` for buys and `price * (1 - slip)` for sells; `bot.py:361` uses `avg_exec_price` from broker response. Backtest does not simulate partial fills, commission-min-rub in all paths, or latency.
   - Files: `backtest.py:68-116, 118-162`, `bot.py:358-456`

3. **Order cancellation leaves unconfirmed state** — `tbank/trader.py:104-152` polls `get_order_state` up to 3 times (2s each), then cancels if not final. If cancel fails, it only logs warning and sets status="cancelled" after successful cancel. No reconciliation on next cycle; a stale open order could cause duplicate positions on the next iteration.
   - Files: `tbank/trader.py:104-152`

4. **ORDER_FALLBACK_TO_MARKET default 1 in sandbox; dangerous if copied to real** — `.env.example:105` sets `ORDER_FALLBACK_TO_MARKET=1`. `tbank/api.py:340-380` retries with `ORDER_TYPE_MARKET` on HTTP 400 when fallback enabled. For `MODE=real`, a bad limit price becomes an uncontrolled market order. README warns, but no runtime guard in `bot.py`/`cli.py`.
   - Files: `.env.example:102-105`, `tbank/api.py:340-380`, `config.py`

5. **No candle freshness guard in live loop** — `bot.py:185-201` `refresh_candles` catches all exceptions and falls back to stale DB cache without checking age. If API is down for hours, the bot trades on outdated prices.
   - Files: `bot.py:185-201`, `tbank/market_data.py:101-131`

6. **Kill-switch resets on restart** — `bot.py:171-172` initializes `_entries_allowed = True`. After a crash or restart, the drawdown gate is lost; the bot resumes trading even if drawdown was the reason for the previous stop. No persistent marker.
   - Files: `bot.py:171-172, 525-543`

7. **Shared throttle keys suppress BUY/SELL alerts** — `bot.py:367-379` (buy) and `bot.py:423-439` (sell) both use `partial-fill:{ticker}` and `order-not-filled:{ticker}`. A rejected BUY silences a rejected SELL for 30 min, directly undermining the stop-loss escalation documented at `bot.py:406-421`.
   - Files: `bot.py:367, 376, 424, 436`, `notify.py:90-102`

8. **report hint contradicts normalize behavior** — `cli.py:613-618` tells user to run `normalize` to withdraw excess, but `cli.py:1075-1080` says withdrawal is impossible and points to `reset-sandbox`.
   - Files: `cli.py:613-618, 1075-1080`

9. **train_summary.json emits bare NaN** — `cli.py:336-348` uses `json.dumps(payload)` with `**artifact.metrics` which may contain NaN (e.g. `live_sharpe_net` when `live_trades=0`). Python's `json.dumps` default `allow_nan=True` writes non-RFC-8259 JSON. External tools reject it.
   - Files: `cli.py:336-348`, `models/registry.py:104-180`

10. **reset-sandbox input() crashes in non-TTY** — `cli.py:1128-1132` calls `input()` without handling EOFError. Cron/CI without `--yes` gets traceback instead of "use --yes".
    - Files: `cli.py:1128-1132`

11. **main() has no top-level exception handler** — `cli.py:1216-1259`. API/network errors in `cmd_report`, `cmd_digest`, `cmd_smoke` produce raw tracebacks.
    - Files: `cli.py:1216-1259`

12. **Silent account selection in real mode** — `cli.py:117-124` returns `accounts[0]` when `ACCOUNT_ID` unset. Multiple accounts in real mode could silently pick wrong account.
    - Files: `cli.py:117-124`

13. **Offline commands bypass config.validate()** — `cli.py:1150-1151, 1221-1225`. `backtest` can run with invalid risk params (e.g. `STOP_LOSS_PCT=5`).
    - Files: `cli.py:1150-1151, 1221-1225`

14. **status misses real-mode budget warning** — `cli.py:778-852` prints `config.budget_rub` without `_budget_base_warning` check that `report` and `digest` show.
    - Files: `cli.py:585-587, 786, 963-965`

15. **No progress indicator for long operations** — `cmd_download` (`cli.py:179-189`), `cmd_train` (`cli.py:262-349`). Train takes 15-25 min with no ETA or i/N counter.
    - Files: `cli.py:179-189, 262-349`

16. **Digest has no coverage/duration notes** — `cli.py:945-1008` omits period coverage that `cmd_report` includes. Subject line is static (no date).
    - Files: `cli.py:945-1008`

17. **send_throttled in-memory only** — `notify.py:58, 90-102`. Process restart resets dedup; watchdog uses durable file marker. Inconsistent.
    - Files: `notify.py:58, 90-102`, `cli.py:930-940`

18. **No confirmation for MODE=real run** — `cli.py:465-482` starts real trading without interactive confirmation.
    - Files: `cli.py:465-482`

19. **Encoding mojibake in launchers** — `tbank.bat`, `tbank.sh`, `deploy/tbank-bot.service`, `deploy/tbank-bot.cron` have Cyrillic comments that render as garbage under default Windows code page.
    - Files: `tbank.bat`, `tbank.sh`, `deploy/tbank-bot.service`, `deploy/tbank-bot.cron`

20. **Ambiguous P&L period heading** — `cli.py:620` says "без учёта пополнений/выводов" but `_period_pnl` at `cli.py:563-567` actually subtracts flows.
    - Files: `cli.py:620, 563-567`

### Medium

21. **No --json/--plain/--quiet/--dry-run/--no-color flags** — `cli.py:1154-1194`. Color control only via env vars (`ui.py:44-57`).
22. **Two files named metrics_<ticker>.json with different schemas** — `models_artifacts/metrics_*.json` (train) vs `reports/metrics_*.json` (backtest).
23. **ui.header docstring mismatch** — `ui.py:160-163` docstring says box-drawing but code uses ASCII "--".
24. **warn() goes to stdout, other errors to stderr** — `cli.py:106-107` vs `cli.py:1057, 1109, 1213, 1227`.
25. **smoke reports success despite partial failure** — `cli.py:170-175`.
26. **README test count drift** — `README.md:320` says 181, repo has 182 test functions.
27. **No tests for cmd_report, compose_digest, cmd_download, cmd_news** — partial coverage map in C5.
28. **STARTTLS path (port 587) untested** — `notify.py:81-84`.
29. **Bot iteration-error email throttles in-memory** — same issue as #17.
30. **digest API errors uncaught** — `cli.py:1011-1025` no try/except around API calls.

### Low / Cosmetic

31. **ui._visible_len uses len() not wcwidth** — `ui.py:116-118`. Cyrillic is narrow so no issue for current locale.
32. **ui.fmt_money uses thin space U+2009 when colors enabled** — `ui.py:109-111`. Can break copy-paste to Excel.
33. **_enabled cache keyed by id(stream)** — `ui.py:59-64`. id reuse risk if stream GC'd (test-only concern).
34. **backtest.py save_report dangling heading** — "## Кривая капитала" with no content after it.

## Decisions (with rationale)

| decision | rationale |
|---|---|
| Report-only audit, no code changes | user asked for analysis; fixes are separate scope |
| No live broker or sandbox calls | safety; token may not exist |
| Russian-language report | user request is in Russian |
| Prioritise by impact (Critical/High/Medium/Low) | standard audit format; helps triage |
| Include hypotheses as well as confirmed bugs | audit value from surfacing risk |
| Test baseline via static analysis + --collect-only | venv not installed; avoid mutating environment |

## Scope IN

- C1: Trading execution/risk — order flow, sizing, stops, kills, reconciliation
- C2: Data/ML/backtest — features, training, evaluation, backtest simulation
- C3: CLI/UI — commands, output, reports, notifications, email
- C4: Ops/reliability — config validation, TLS, retries, deploy, monitoring
- C5: Test coverage — gaps, missing tests, stale artifacts

## Scope OUT (Must NOT have)

- No code edits to application source
- No installation of dependencies (no pip install)
- No live API calls or sandbox operations
- No financial advice or trading recommendations
- No changes to .env, .gitignore, or user-owned files (.omo/, .zcodeignore)
- No performance benchmarking (only static analysis of hot paths)
- No security penetration testing (only static review of credential handling)

## Approval gate
status: awaiting-approval

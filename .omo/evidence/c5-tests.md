# C5 — Test coverage & repository health evidence

## Test count (static analysis)

**Command:** `grep -c "def test_" tests/test_*.py` (simulated via codegraph/grep)

| Test file | Test function count |
|---|---|
| tests/test_backtest.py | 8 |
| tests/test_bot.py | 14 |
| tests/test_capital_profiles.py | 19 |
| tests/test_execution.py | 11 |
| tests/test_features.py | 5 |
| tests/test_marketdata.py | 8 |
| tests/test_models.py | 22 |
| tests/test_monitoring.py | 11 |
| tests/test_news_api.py | 3 |
| tests/test_nlp.py | 9 |
| tests/test_observability.py | 3 |
| tests/test_risk_ml.py | 9 |
| tests/test_strategy.py | 12 |
| tests/test_tls.py | 9 |
| tests/test_trader.py | 16 |
| tests/test_ui.py | 8 |
| tests/test_usability.py | 15 |
| **Total** | **182** |

README.md:320 claims "181 офлайн-тест" — minor drift of 1.

Note: `.venv` существует с сентября, `pytest` установлен, но тесты не запускались. Test count is from static analysis of `def test_*` function definitions, not collected test items (which may differ due to parametrize).

## Coverage map

| Surface | Component | Test file | Covered? | Notes |
|---|---|---|---|---|
| `ui.paint`, `fmt_signed`, `fmt_money`, `render_table`, `header` | C3 | `tests/test_ui.py` | ✅ Yes | 8 tests cover core formatting |
| CLI parser, env typo warnings, status/setup/send-test-email | C3 | `tests/test_usability.py` | ✅ Yes | 15 tests cover basic CLI flows |
| `Notifier` disabled/SSL/fail, throttle negative, SmtpConfig | C3 | `tests/test_monitoring.py` | ✅ Partial | Missing: STARTTLS (587), throttle success path, send_throttled email content |
| Watchdog fresh/stale/out-of-session | C3 | `tests/test_monitoring.py` | ✅ Yes | 4 tests cover core watchdog logic |
| `log_equity`, `parse_portfolio` | C3 | `tests/test_observability.py` | ✅ Yes | 3 tests |
| `_net_invested`, `_period_pnl`, normalize, reset-sandbox | C3 | `tests/test_trader.py` | ✅ Yes | 16 tests cover report helpers |
| Model gate, portfolio lots, failed-sell escalation | C1 | `tests/test_bot.py` | ✅ Yes | 14 tests |
| Kill-switch state (not email content) | C1 | `tests/test_risk_ml.py` | ⚠️ Partial | Notifier disabled in tests; email body/subject untested |
| Execution status parsing, partial fills, Quotation signs | C1 | `tests/test_execution.py` | ✅ Yes | 11 tests |
| Strategy decisions, exposure limit | C1 | `tests/test_strategy.py` | ✅ Yes | 12 tests |
| Feature engineering, target correctness, no leakage | C2 | `tests/test_features.py` | ✅ Yes | 5 tests |
| Walk-forward purge (horizon=2 only), net-Sharpe | C2 | `tests/test_risk_ml.py` | ⚠️ Partial | No test for horizon > 2 |
| Backtest invariants, gap-stops, FIFO, news series | C2 | `tests/test_backtest.py` | ✅ Yes | 8 tests |
| Market data: candle load/store, instrument cache | C2 | `tests/test_marketdata.py` | ✅ Yes | 8 tests |
| TLS bundle construction | C4 | `tests/test_tls.py` | ✅ Yes | 9 tests |
| NLP lexicon, decay, news windows | C2 | `tests/test_nlp.py` | ✅ Yes | 9 tests |
| Capital profile startup | C1 | `tests/test_capital_profiles.py` | ✅ Yes | 19 tests |
| **cmd_report** | C3 | — | ❌ **No** | Only helpers tested; full command untested |
| **compose_digest / cmd_digest** | C3 | — | ❌ **No** | No test at all |
| **cmd_download** | C3 | — | ❌ **No** | No test |
| **cmd_news** | C3 | — | ❌ **No** | No test |
| **cmd_smoke** | C3 | — | ❌ **No** | No test |
| **ui._detect env/isatty/VT** | C3 | — | ❌ **No** | Only manual set_colors tested |
| **STARTTLS (port 587)** | C3 | — | ❌ **No** | Only SSL 465 tested |
| **bot.run_forever loop** | C1 | — | ❌ **No** | No integration test |
| **Kill-switch email content** | C1 | `tests/test_risk_ml.py` | ⚠️ Partial | State tested, email content not |
| **main() dispatch errors** | C3 | — | ❌ **No** | No test for uncaught exceptions |
| **reset-sandbox --yes path** | C3 | `tests/test_trader.py` | ⚠️ Partial | assume_yes=False tested; --yes not |
| **notify.send_throttled success path** | C3 | `tests/test_monitoring.py` | ⚠️ Partial | Only negative (throttle blocks) tested |
| **download_candles error handling** | C2 | — | ❌ **No** | No test for API failure mid-download |
| **build_features with horizon > 1 staleness** | C2 | — | ❌ **No** | No test for live prediction feature alignment |

## Repository health

### Test infrastructure
- **Framework:** pytest 8.0+ (`requirements.txt:9`)
- **Config:** `tests/conftest.py` with shared fixtures (`candles`, `flat_candles`)
- **CI:** No CI config found (no `.github/workflows/`, `.gitlab-ci.yml`, etc.)
- **Coverage tool:** Not configured (no `.coveragerc`, no `pytest-cov` in requirements)

### Stale artifacts
- `reports/` contains 16 `trades_*.csv`, 16 `metrics_*.json`, `train_summary.json` — appear to be from a previous run
- `reports_verify/` contains verification outputs for SBER and ALRS — appears intentional for regression checking
- `var/eval_sber.log`, `var/eval_sber.py` — ad-hoc evaluation scripts

### Documentation
- `README.md` — comprehensive (338 lines), well-structured, covers quick start, commands, architecture, config, tests
- `DEPLOY.md` — detailed deployment guide (437 lines), covers Windows/Linux, systemd, cron, NLP, monitoring
- `.env.example` — well-commented (137 lines), explains each parameter
- **Drift:** `README.md:320` says 181 tests, repo has 182 (minor)

### Dependency hygiene
- `requirements.txt` — minimal, 8 core deps + optional NLP section (commented)
- No `setup.py`, `pyproject.toml`, or `setup.cfg` — project is run directly from source
- No version pinning beyond minimum versions (e.g., `pandas>=2.2`)
- No lock file (`requirements.lock`, `Pipfile.lock`)

## Summary
- 182 test functions across 17 test files
- 11 surfaces completely untested
- 5 surfaces partially tested
- No CI pipeline configured
- No coverage tooling
- Minor README drift (181 vs 182)
- No dependency lock file
- Total: 11 coverage gaps + 3 repo health findings = 14 findings

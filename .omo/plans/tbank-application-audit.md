# tbank-application-audit - Work Plan

## TL;DR (For humans)

**What you'll get:** A Russian-language audit report (`AUDIT_REPORT.md`) covering trading execution risks, ML/backtest correctness, CLI/UI quality, reliability/security, and test coverage — with prioritised findings, file:line citations, evidence, and recommendations. No code changes.

**Why this approach:** Read-only analysis across 5 independent components, each producing cited evidence, then synthesised into one report. Maximises parallelism (5 components at once) while keeping verification strict.

**What it will NOT do:**
- Change any application code
- Call the live T-Invest API or sandbox
- Install dependencies or modify `.env`
- Provide financial or trading recommendations

**Effort:** Medium
**Risk:** Low — read-only, no side effects
**Decisions I made for you:**
- Report-only (no fixes); fixes are a separate scope
- Russian language (matching your request)
- No live API/broker interaction (safety)
- Test baseline via static analysis + `pytest --collect-only` (venv not installed; avoids mutation)
- Findings include hypotheses clearly marked as such (audit value from surfacing risk)

Your next move: the plan is written. Run `$start-work` to execute the audit.

---

> TL;DR (machine): Medium effort, Low risk. 5 parallel evidence-gathering tasks → 1 synthesis → final review wave. Deliverable: AUDIT_REPORT.md.

## Scope
### Must have
- Russian-language audit report with prioritised findings (Critical/High/Medium/Low)
- File:line citations for every finding
- Evidence files under `.omo/evidence/` for each component
- Coverage of 5 components: execution/risk, data/ML/backtest, CLI/UI, ops/security, tests
- Recommendations section with concrete next steps
- Clear separation of confirmed defects vs hypotheses

### Must NOT have (guardrails, anti-slop, scope boundaries)
- No edits to any `.py`, `.md` (except AUDIT_REPORT.md), `.env`, `.sh`, `.bat`, `.service`, `.cron` files
- No `pip install`, no venv creation, no dependency changes
- No live API calls (no HTTP to invest-public-api.tbank.ru)
- No sandbox account operations
- No financial advice or trading recommendations
- No changes to user-owned files (`.omo/`, `.zcodeignore`, `data/`, `reports/`, `models_artifacts/`)

## Verification strategy
> Zero human intervention - all verification is agent-executed.
- Test decision: none (audit, not implementation) + static evidence review
- Evidence: `.omo/evidence/c{1-5}-*.md` for each component, `.omo/evidence/audit-report.md` for synthesis
- Verification of evidence: each evidence file must contain ≥3 cited findings with file:line references, distinguish confirmed vs hypothesised, and include reproduction scenario or reasoning chain
- Final wave: 4 reviewers (F1 plan compliance, F2 evidence quality, F3 cross-reference, F4 scope fidelity)

## Execution strategy
### Parallel execution waves

**Wave 1** (5 parallel evidence tasks): C1–C5 run simultaneously, no inter-dependencies.
**Wave 2** (1 synthesis task): depends on all of Wave 1. Produces `AUDIT_REPORT.md`.
**Wave 3** (Final verification): F1–F4 run in parallel after Wave 2.

### Dependency matrix
| Todo | Depends on | Blocks | Can parallelize with |
|---|---|---|---|
| T1 (C1) | — | T6 | T2, T3, T4, T5 |
| T2 (C2) | — | T6 | T1, T3, T4, T5 |
| T3 (C3) | — | T6 | T1, T2, T4, T5 |
| T4 (C4) | — | T6 | T1, T2, T3, T5 |
| T5 (C5) | — | T6 | T1, T2, T3, T4 |
| T6 (Report) | T1, T2, T3, T4, T5 | F1-F4 | — |
| F1-F4 | T6 | — | F1, F2, F3, F4 |

## Todos
> Implementation + Test = ONE todo. Never separate.

- [x] 1. C1 — Trading execution & risk evidence
  What to do: Investigate order flow (`bot.py:296-456`, `tbank/trader.py:104-173`), sizing/risk (`strategy.py:55-114`), kill-switch (`bot.py:525-543`), reconciliation gaps, partial-fill handling, throttle-key collision (`bot.py:367-436`). Write evidence to `.omo/evidence/c1-execution.md` with ≥5 cited findings.
  Parallelization: Wave 1 | Blocked by: — | Blocks: T6
  References: `bot.py:296-456, 525-543`, `tbank/trader.py:104-173`, `strategy.py:28-114`, `notify.py:90-102`, `.env.example:102-105`, `tbank/api.py:340-380`
  Acceptance criteria: `.omo/evidence/c1-execution.md` exists, contains ≥5 findings each with file:line, severity (Critical/High/Medium/Low), confirmed/hypothesis label, and reproduction reasoning.
  QA scenarios: happy — Read evidence, verify each finding cites a real line; failure — evidence missing or <5 findings → reject.
  Commit: N

- [x] 2. C2 — Data/ML/backtest evidence
  What to do: Investigate feature engineering (`features.py:42-98`), live prediction staleness (`bot.py:276-292`), walk-forward protocol (`models/registry.py:211-241`), backtest simulation (`backtest.py:30-162`), execution-price divergence, model gate consistency (`bot.py:254-274` vs `cli.py:394-398`), train_summary.json NaN issue (`cli.py:336-348`). Write evidence to `.omo/evidence/c2-ml.md`.
  Parallelization: Wave 1 | Blocked by: — | Blocks: T6
  References: `features.py:42-98, 135-174`, `bot.py:276-292, 254-274`, `models/registry.py:104-241, 331-458`, `backtest.py:30-222`, `cli.py:336-348, 394-398`, `models/boosting.py:31-85`
  Acceptance criteria: `.omo/evidence/c2-ml.md` exists with ≥5 findings, each with file:line, severity, confirmed/hypothesis, reasoning chain.
  QA scenarios: happy — Read evidence, verify findings reference real code; failure — <5 findings or missing citations → reject.
  Commit: N

- [x] 3. C3 — CLI/UI/reporting evidence
  What to do: Inventory all user-facing surfaces (console, reports, email, launchers). Assess: `warn()` stream inconsistency (`cli.py:106-107`), `report` normalize hint contradiction (`cli.py:613-618` vs `:1075-1080`), `smoke` success-on-partial-failure (`cli.py:170-175`), ambiguous P&L heading (`cli.py:620`), missing progress indicators, `reset-sandbox` EOFError (`cli.py:1129`), `main()` no exception handler (`cli.py:1216-1259`), silent account selection (`cli.py:117-124`), offline commands skip validation (`cli.py:1150-1151`), encoding mojibake in launchers, no `--json`/`--plain`/`--no-color` flags, `status` missing real-mode warning, notification throttle-key collision, `send_throttled` in-memory vs watchdog file marker, `digest` missing coverage notes, `ui.fmt_money` thin space U+2009, `ui.header` docstring mismatch. Write evidence to `.omo/evidence/c3-ui.md`.
  Parallelization: Wave 1 | Blocked by: — | Blocks: T6
  References: `cli.py:106-107, 117-124, 170-175, 262-349, 465-482, 572-722, 778-852, 884-942, 945-1025, 1048-1147, 1150-1259`, `ui.py:31-163`, `notify.py:27-102`, `bot.py:341-456, 487-543`, `tbank.bat`, `tbank.sh`, `deploy/tbank-bot.service`, `deploy/tbank-bot.cron`, `tests/test_ui.py`, `tests/test_usability.py`, `tests/test_monitoring.py`
  Acceptance criteria: `.omo/evidence/c3-ui.md` exists with ≥8 findings, each with file:line, severity, confirmed/hypothesis.
  QA scenarios: happy — Read evidence, verify surface inventory matches code; failure — <8 findings or missing key issues (normalize contradiction, EOFError, exception handling) → reject.
  Commit: N

- [x] 4. C4 — Reliability/security/deploy evidence
  What to do: Review config validation (`config.py:220-247`), `.env` secret handling (`.env.example`, `.gitignore`), TLS bundle (`tbank/rest.py:170-192`), API retries/throttling (`tbank/rest.py:140-235`), candle freshness (`bot.py:185-201`), kill-switch persistence (`bot.py:171-172, 525-543`), heartbeat/watchdog (`bot.py:108-111`, `cli.py:884-942`), systemd/cron (`deploy/tbank-bot.service`, `deploy/tbank-bot.cron`), `MODE=real` guards, `ORDER_FALLBACK_TO_MARKET` safety, log rotation (`cli.py:1197-1213`), SQLite WAL/locking (`tbank/market_data.py:18-96`). Write evidence to `.omo/evidence/c4-ops.md`.
  Parallelization: Wave 1 | Blocked by: — | Blocks: T6
  References: `config.py:1-247`, `.env.example`, `.gitignore`, `tbank/rest.py:34-235`, `tbank/market_data.py:18-170`, `bot.py:108-111, 185-201, 458-523`, `cli.py:884-942, 1197-1213`, `deploy/tbank-bot.service`, `deploy/tbank-bot.cron`, `deploy/install.sh`, `tbank/api.py:340-380`
  Acceptance criteria: `.omo/evidence/c4-ops.md` exists with ≥5 findings, each with file:line, severity, confirmed/hypothesis.
  QA scenarios: happy — Read evidence, verify each finding cites real config/code; failure — <5 findings or missing key issues (candle freshness, kill-switch reset, fallback safety) → reject.
  Commit: N

- [x] 5. C5 — Test coverage & repository health evidence
  What to do: Run `python -m pytest --collect-only -q tests/` (if possible) or static count of `def test_*` across all test files. Map which surfaces are tested vs untested (from C3 inventory). Identify: missing tests for `cmd_report`, `compose_digest`, `cmd_download`, `cmd_news`, `cmd_smoke`, STARTTLS path, `ui._detect`, `bot.run_forever`, kill-switch email content. Check README drift (`README.md:320` says 181, repo has 182). Identify stale artifacts in `reports/`, `reports_verify/`. Write evidence to `.omo/evidence/c5-tests.md`.
  Parallelization: Wave 1 | Blocked by: — | Blocks: T6
  References: `tests/test_*.py` (all 17 files), `README.md:314-327`, `requirements.txt`, `conftest.py`, `reports/*.csv`, `reports/*.json`, `reports_verify/`
  Acceptance criteria: `.omo/evidence/c5-tests.md` exists with: (a) test count from static analysis, (b) coverage map table (surface → test file → tested yes/no), (c) ≥5 gaps identified with file:line.
  QA scenarios: happy — Read evidence, verify test count matches `grep -c "def test_" tests/test_*.py`; failure — count mismatch or missing coverage map → reject.
  Commit: N

- [x] 6. Synthesize audit report
  What to do: Read all 5 evidence files from `.omo/evidence/c{1-5}-*.md`. Produce `AUDIT_REPORT.md` in Russian at repo root with sections: Executive Summary, Methodology, Findings (Critical → Low), Recommendations, Appendix (evidence index). Each finding must cite file:line, component (C1-C5), severity, confirmed/hypothesis, impact, and recommendation. Final wave reviews this report.
  Parallelization: Wave 2 | Blocked by: T1, T2, T3, T4, T5 | Blocks: F1-F4
  References: `.omo/evidence/c1-execution.md`, `.omo/evidence/c2-ml.md`, `.omo/evidence/c3-ui.md`, `.omo/evidence/c4-ops.md`, `.omo/evidence/c5-tests.md`
  Acceptance criteria: `AUDIT_REPORT.md` exists at repo root, written in Russian, contains all findings from all 5 evidence files, deduplicated and prioritised, with executive summary ≤200 words, and recommendations section with ≥5 actionable items.
  QA scenarios: happy — Read report, verify it cites ≥20 distinct findings across all 5 components, check Russian language, verify executive summary present; failure — missing component or <20 findings → reject.
  Commit: Y | docs(audit): add comprehensive application audit report

## Final verification wave
> Runs in parallel after ALL todos. ALL must APPROVE. Surface results and wait for the user's explicit okay before declaring complete.
- [x] F1. Plan compliance audit — APPROVE (minor: #15 severity label, #29/#37 file:line, #13 impact)
- [x] F2. Evidence quality review — APPROVE (minor: F-C1.7 citation, severity inflation, duplicates)
- [x] F3. Cross-reference check — APPROVE (minor: F-C2.6 merged, C5 repo health absent, counts ~40 vs 38)
- [x] F4. Scope fidelity — APPROVE (minor: .zcodeignore pre-existing, pytest cache + ca_bundle.pem modified today)

## Commit strategy
Single commit after audit report synthesis: `docs(audit): add comprehensive application audit report`. Contains only `AUDIT_REPORT.md` and `.omo/` artifacts.

## Success criteria
1. `AUDIT_REPORT.md` exists at repo root, written in Russian
2. Report contains ≥20 distinct findings across all 5 components
3. Every finding has file:line citation, severity, confirmed/hypothesis label
4. Executive summary ≤200 words
5. Recommendations section with ≥5 actionable items
6. All final wave reviewers (F1-F4) APPROVE
7. No application code, config, or user data was modified

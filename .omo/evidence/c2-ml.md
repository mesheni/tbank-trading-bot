# C2 — Data/ML/backtest evidence

## Findings

### F-C2.1 [Critical] Live LGBM prediction uses stale features
- **File:** `bot.py:276-292`, `features.py:42-98`, `models/registry.py:211-241`
- **Severity:** Critical
- **Confirmed:** Yes (code path analysis; needs runtime confirmation)
- **Description:** `build_features()` at `features.py:95-97` computes forward target (`close.shift(-horizon)`) and drops last `horizon` rows (NaN target). `bot.predict()` at `bot.py:283` uses `features.iloc[-1]` — the last row of the truncated DataFrame. For horizon H, this row's features are computed from candles up to bar `N-H`, while the price used for the decision is at bar `N` (the latest candle). The prediction features are H bars behind the decision price.
  
  In backtest, `walk_forward_lgbm` predicts on the same truncated features but aligns predictions to feature-index timestamps via `preds[ts] = float(value)`. The `run_backtest` loop at `backtest.py:121` gets `preds.get(ts, 0.0)` — if `ts` is not in the feature index, prediction defaults to 0.0 (HOLD). So backtest uses features correctly for its timestamps, but live uses features from a different (stale) timestamp.
  
  **Net effect:** The model predicts return for a bar that is H bars old, but the decision is made on the current bar's price. For horizon=1 this is 1 bar stale; for horizon=4 it is 4 bars stale.
- **Reproduction:** Train model on features → in live, `features.iloc[-1]` is at index T-H → prediction describes market at T-H → price at T is used for BUY/SELL decision → model signal and price are misaligned by H bars.
- **Impact:** Trading signals based on stale feature snapshots.

### F-C2.2 [High] Backtest vs live execution-price semantics diverge
- **File:** `backtest.py:68-116, 118-162`, `bot.py:358-456`
- **Severity:** High
- **Confirmed:** Yes
- **Description:** Backtest applies `exec_price = price * (1 + slip)` for buys and `price * (1 - slip)` for sells. It does not simulate: (a) partial fills, (b) commission_min_rub on all paths consistently, (c) order latency or queue position, (d) cancel-and-replace scenarios. Live execution uses `avg_exec_price` from broker response which may differ significantly from `price * (1 ± slip)`.
- **Impact:** Backtest results overestimate or underestimate real execution quality.

### F-C2.3 [High] train_summary.json emits bare NaN (invalid JSON)
- **File:** `cli.py:336-348`, `models/registry.py:104-180`
- **Severity:** High
- **Confirmed:** Yes (file `reports/train_summary.json` contains bare NaN)
- **Description:** `json.dumps(payload)` at `cli.py:342` uses default `allow_nan=True`. `artifact.metrics` at `models/registry.py:171-180` includes `live_sharpe_net` which is `float("nan")` when `live_trades == 0`. Python's `json.dumps` writes bare `NaN` literal which is NOT valid RFC-8259 JSON. External tools (JavaScript `JSON.parse`, strict parsers) reject it.
- **Reproduction:** Train a ticker where no model crosses the entry threshold → `live_trades=0` → `live_sharpe_net=NaN` → `train_summary.json` contains `"live_sharpe_net": NaN` → `JSON.parse()` fails.
- **Impact:** Interoperability failure with external tools.

### F-C2.4 [High] Model gate inconsistency between backtest and live
- **File:** `bot.py:254-274`, `cli.py:394-398`
- **Severity:** High
- **Confirmed:** Yes
- **Description:** Live `_model_allowed()` at `bot.py:254-274` gates on `directional_acc >= MIN_MODEL_DIR_ACC` OR `live_sharpe_net > 0` OR `strategy_sharpe_net > 0`. Backtest at `cli.py:394-398` applies `min_abs_return = max(risk.min_abs_return, artifact.threshold)` but does NOT check the model quality gate. Backtest always runs regardless of model quality; live may block entries. Results are not comparable.
- **Impact:** Backtest results do not predict live behavior when model quality is marginal.

### F-C2.5 [Medium] No test for walk-forward purge correctness with horizon > 1
- **File:** `tests/test_risk_ml.py:17-47`, `models/registry.py:226-241`
- **Severity:** Medium
- **Confirmed:** Yes
- **Description:** `test_walk_forward_lgbm_purges_horizon_overlap` tests horizon=2 purge. No test covers horizon=4 or higher. The purge logic at `registry.py:234` (`train = features.loc[:block[0]].iloc[:-(horizon + 1)]`) must correctly exclude `horizon+1` rows from the training tail. Higher horizons have more complex edge cases.
- **Impact:** Potential data leakage with non-default horizons.

### F-C2.6 [Medium] Features computed for all bars but last horizon bars dropped — no sentinel
- **File:** `features.py:95-97`
- **Severity:** Medium
- **Confirmed:** Yes
- **Description:** `build_features` drops rows with NaN target silently. No sentinel or warning when the DataFrame is significantly truncated (e.g., short candle history). Combined with F-C2.1, this creates a silent staleness bug.
- **Impact:** Silent data loss without operator awareness.

### F-C2.7 [Low] backtest.py save_report has dangling heading
- **File:** `backtest.py:244` (approximate)
- **Severity:** Low
- **Confirmed:** Yes
- **Description:** The markdown report writes `"## Кривая капитала"` heading with no content after it. The equity curve is in a separate CSV file. The heading is misleading.
- **Impact:** Cosmetic; confusing report structure.

### F-C2.8 [Low] Two metrics files with same basename, different schemas
- **File:** `models/registry.py:445-453` (train), `backtest.py:249` (backtest)
- **Severity:** Low
- **Confirmed:** Yes
- **Description:** `models_artifacts/metrics_SBER_hour_h1.json` (train artifact, has `best` and `metrics` keys) and `reports/metrics_SBER_hour_h1.json` (backtest result, has `total_return`, `sharpe`, etc.) share the same basename pattern but have completely different schemas. Easy to misread.
- **Impact:** Usability hazard for operators reading reports.

## Summary
- 1 Critical finding (stale features in live prediction)
- 3 High findings (execution semantics, NaN JSON, model gate inconsistency)
- 2 Medium findings (purge test gap, silent truncation)
- 2 Low findings (cosmetic)
- Total: 8 findings

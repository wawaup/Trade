# Trade Quant Expert Risk Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the quant expert review into concrete Trade hardening: remove backtest look-ahead bias, make VWAP session-correct, fix paper execution accounting, preserve full multi-asset audit trails, and document the staged path from research to real-capital readiness.

**Architecture:** Keep the current lightweight Python + static frontend shape. The first implementation slice fixes correctness and auditability inside existing modules rather than adding a broker adapter. Anything involving real capital remains explicitly gated behind documentation, risk checks, idempotency, audit, broker/account-rule selection, and a future live adapter plan.

**Tech Stack:** Python standard library, `unittest`/`pytest`, existing `tradebot` modules, static `web/` files, local JSON result persistence.

---

## Review Findings Accepted For This Plan

The expert review identifies several issues that are valid in the current codebase:

- `generate_signal()` uses VWAP over the whole `intraday` window, so a multi-session input pollutes day-level VWAP.
- `run_backtest()` computes signals on bar N and fills on the same bar's close, which is look-ahead biased.
- `PaperExecutionAdapter._buy()` double counts buy fee by using `(quote - fee) / price` and also subtracting `quote + fee` from cash.
- `build_backtest_response()` serializes only the first asset's trades/equity/order intents/risk events, while summary and assets are multi-symbol.
- `build_paper_order_response()` uses `PaperExecutionAdapter(PAPER_ACCOUNT)` with no `RiskLimits`, creating a manual-order bypass.
- CSV data source exists but HTTP does not expose `dailyPath`/`intradayPath`, so real local data is not reachable from the workbench.
- T-bucket returns are shown without a core-only buy-and-hold baseline or alpha comparison.
- Frontend labels such as "智能调参" and "压力测试" imply real methodology that the backend does not yet compute.

One review detail needs updating for current rule context:

- As of FINRA Regulatory Notice 26-10, published 2026-04-20, new intraday margin standards replace former day-trading margin requirements, including old pattern-day-trader count and $25,000 minimum equity. Effective date is 2026-06-04, with member phase-in allowed until 2027-10-20. Therefore Trade docs must require broker-specific rule verification rather than hard-coding the old PDT rule as universally current.

## File Structure

- Modify `tradebot/indicators.py`: add session-aware VWAP helper.
- Modify `tradebot/strategy.py`: use session-scoped VWAP for signal generation.
- Modify `tradebot/backtest.py`: fill on next bar open, add max-layer rejection, add core-only baseline fields.
- Modify `tradebot/execution.py`: fix paper buy fee accounting.
- Modify `tradebot/serialization.py`: serialize per-asset full result detail.
- Modify `tradebot/server.py`: pass CSV paths to data sources, persist all asset details, apply paper risk limits.
- Modify `web/index.html`: make methodologically incomplete tabs honest.
- Modify `web/app.js`: pass selected data source and CSV paths when available; keep UI safe if absent.
- Modify `docs/strategy.md`, `docs/execution.md`, `docs/frontend.md`: update implemented behavior and real-capital gates.
- Add/modify tests in `tests/test_indicators.py`, `tests/test_strategy.py`, `tests/test_backtest.py`, `tests/test_execution.py`, `tests/test_server.py`, `tests/test_frontend_files.py`.

## Task 1: Session-Aware VWAP

**Files:**
- Modify: `tradebot/indicators.py`
- Modify: `tradebot/strategy.py`
- Test: `tests/test_indicators.py`, `tests/test_strategy.py`

- [ ] **Step 1: Write failing VWAP tests**

Add tests that verify `session_vwap(candles)` only uses the session containing the latest candle, and that `generate_signal()` does not let a previous day's high-price candles pollute the current day's VWAP.

- [ ] **Step 2: Run focused tests and observe failure**

Run:

```bash
PYTHONPATH=. python3 -m unittest tests.test_indicators tests.test_strategy -v
```

Expected before implementation: import failure or assertion failure for `session_vwap`.

- [ ] **Step 3: Implement session VWAP**

Add `session_vwap(candles, reset_utc_hour=8)` in `tradebot/indicators.py`, using `tradebot.data.session_start_ms()` to select only candles from the latest candle's trading session.

- [ ] **Step 4: Use session VWAP in strategy**

Replace strategy-level `vwap(intraday)` with `session_vwap(intraday)`.

- [ ] **Step 5: Re-run tests**

Run the same focused command. Expected: tests pass.

## Task 2: Next-Bar Fill And Max Layers

**Files:**
- Modify: `tradebot/backtest.py`
- Test: `tests/test_backtest.py`

- [ ] **Step 1: Write failing tests**

Add tests that verify a BUY signal generated on bar N fills at bar N+1 open plus slippage, and that a configured `max_layers` rejects further ADD_T intents even when cash remains.

- [ ] **Step 2: Run focused test and observe failure**

Run:

```bash
PYTHONPATH=. python3 -m unittest tests.test_backtest -v
```

Expected before implementation: fill price still uses current close; no max-layer rejection exists.

- [ ] **Step 3: Implement next-bar fill**

Iterate the backtest over signal index and next bar separately. Compute signal with candles through bar N, execute on bar N+1 open, and mark `executionAssumptions.fillTiming` as `next_bar_open`.

- [ ] **Step 4: Implement max-layer guardrail**

Add `max_layers` to `StrategyConfig` with a conservative default, reject buy signals when `layers >= max_layers`, and record `risk_event.type = "max_layers_rejected"`.

- [ ] **Step 5: Re-run focused tests**

Expected: backtest tests pass.

## Task 3: Paper Fee Accounting And Manual Risk Limits

**Files:**
- Modify: `tradebot/execution.py`
- Modify: `tradebot/server.py`
- Test: `tests/test_execution.py`, `tests/test_server.py`

- [ ] **Step 1: Write failing tests**

Add a paper buy accounting test that expects `cash -= quote` and `qty = (quote - fee) / price`. Add server tests that a too-large quick paper order is rejected by `max_order_quote`.

- [ ] **Step 2: Run focused tests and observe failure**

Run:

```bash
PYTHONPATH=. python3 -m unittest tests.test_execution tests.test_server -v
```

Expected before implementation: cash is reduced by `quote + fee`; quick paper order bypasses risk limits.

- [ ] **Step 3: Fix fee accounting**

Change `_buy()` so the account cash check and cash deduction use `quote`, with fee represented inside the filled quantity.

- [ ] **Step 4: Apply server-side quick paper risk limits**

Instantiate `PaperExecutionAdapter(PAPER_ACCOUNT, RiskLimits(max_order_quote=..., max_t_position_quote=..., max_spread_pct=...))` in `build_paper_order_response()`. Keep all quick trade orders paper-only.

- [ ] **Step 5: Re-run focused tests**

Expected: execution and server tests pass.

## Task 4: Full Multi-Asset Result Audit And Core Baseline

**Files:**
- Modify: `tradebot/backtest.py`
- Modify: `tradebot/serialization.py`
- Modify: `tradebot/server.py`
- Test: `tests/test_backtest.py`, `tests/test_server.py`

- [ ] **Step 1: Write failing tests**

Add tests that persisted multi-symbol backtest details contain `assetDetails` for every symbol, each with its own `trades`, `equityCurve`, `orderIntents`, and `riskEvents`. Add tests for `summary.coreOnlyReturnPct` and `summary.strategyVsCoreOnlyAlpha`.

- [ ] **Step 2: Run focused tests and observe failure**

Run:

```bash
PYTHONPATH=. python3 -m unittest tests.test_backtest tests.test_server -v
```

Expected before implementation: only first asset details exist; baseline metrics are absent.

- [ ] **Step 3: Add core baseline to `BacktestResult`**

Compute core-only ending value from daily first/last close for the core allocation, and expose `core_only_return_pct` and `strategy_vs_core_only_alpha`.

- [ ] **Step 4: Serialize all asset details**

Add serializer helper that emits each asset's full result detail. Persist it under `assetDetails`.

- [ ] **Step 5: Add aggregate baseline summary**

Build aggregate `coreOnlyReturnPct` and `strategyVsCoreOnlyAlpha` in `build_backtest_response()`.

- [ ] **Step 6: Re-run focused tests**

Expected: backtest and server tests pass.

## Task 5: CSV HTTP Access And Data Source Honesty

**Files:**
- Modify: `tradebot/server.py`
- Modify: `web/index.html`
- Modify: `web/app.js`
- Modify: `tests/test_server.py`
- Modify: `tests/test_frontend_files.py`

- [ ] **Step 1: Write failing tests**

Add server tests that `source=CSV` accepts `dailyPath` and `intradayPath` in `/api/backtest/run` and `/api/klines`. Add frontend file tests that the UI copy says simulated methodology for sensitivity/stress tabs until real calculations exist.

- [ ] **Step 2: Run focused tests and observe failure**

Run:

```bash
PYTHONPATH=. python3 -m unittest tests.test_server tests.test_frontend_files -v
```

Expected before implementation: CSV paths are ignored; frontend still implies real smart tuning/stress testing.

- [ ] **Step 3: Implement CSV path passthrough**

Parse `dailyPath` and `intradayPath` in server request builders and pass them into `DataSourceFactory.get_source(source).get_default_candles(...)`.

- [ ] **Step 4: Update frontend copy**

Rename "智能调参" to "参数敏感度示例" and "压力测试" to "压力情景示例" until backed by real analysis endpoints.

- [ ] **Step 5: Re-run focused tests**

Expected: server and frontend file tests pass.

## Task 6: Documentation And Real-Capital Gate

**Files:**
- Modify: `docs/strategy.md`
- Modify: `docs/execution.md`
- Modify: `docs/frontend.md`
- Create: `docs/real_capital_readiness.md`

- [ ] **Step 1: Update strategy docs**

Document session VWAP, next-bar-open fills, max layers, core baseline, current fixed exits, and remaining unresolved research issues.

- [ ] **Step 2: Update execution docs**

Document corrected paper fee accounting, unified paper risk limits, all-asset audit detail, CSV real-data path support, and continued paper-only boundary.

- [ ] **Step 3: Update frontend docs**

Document honest placeholder labels and future endpoints required for actual sensitivity/stress testing.

- [ ] **Step 4: Add real-capital readiness document**

Create a staged checklist from research hypothesis through data quality, backtest correction, out-of-sample validation, risk framework, market/account rules, real-data paper trading, infrastructure, staged capital rollout, and continuous governance. Include current FINRA intraday margin rule context and SEC T+1 settlement context.

- [ ] **Step 5: Verify docs and tests**

Run:

```bash
rg -n "modeled_current_close|手续费.*两次|智能调参|压力测试" docs web tests tradebot
PYTHONPATH=. python3 -m unittest discover -s tests
```

Expected: no stale false claims; all tests pass.

## Out Of Scope For This Implementation Slice

- Real broker integration.
- Secret storage.
- Idempotent live order gateway.
- Reconciliation against real broker positions.
- User login/authorization.
- Real paid data vendor integration.
- Actual walk-forward optimizer, bootstrap, or Monte Carlo engine.

These are required before real capital, but should be implemented only after the corrected paper/backtest foundation is stable.

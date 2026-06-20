# Trade Backend Gap Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans or equivalent TDD discipline to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the highest-value backend gaps between Trade and the useful thin-platform parts of QuantDinger: real data-source routing, persistent backtest results, traceable order intents/risk events, and safer paper execution.

**Architecture:** Keep Trade lightweight: Python standard library, static HTTP server, local JSON persistence, and focused pure modules. Do not introduce PostgreSQL, user accounts, MCP, or real exchange live execution in this phase. Strategy code emits signals; backtest/server code converts them to order intent records, risk events, trades, and persisted result snapshots.

**Tech Stack:** Python standard library, `unittest`/`pytest`, existing `tradebot` package, local JSON under `data/`.

---

## File Structure

- Create `tradebot/storage.py`: JSON-backed storage for backtest result history and detail lookup.
- Create `tradebot/serialization.py`: serialize `BacktestResult`, `Trade`, `OrderIntent`, risk events, candles, and equity points for API responses.
- Modify `tradebot/backtest.py`: record order intents and risk events during simulation.
- Modify `tradebot/server.py`: route K-line/backtest requests through `DataSourceFactory`, persist full results, expose result detail and paper reset endpoints, and include source/status metadata.
- Modify `tradebot/execution.py`: expand paper risk limits for stale market data, spread, and daily loss.
- Add or modify tests in `tests/test_storage.py`, `tests/test_server.py`, `tests/test_backtest.py`, `tests/test_execution.py`, and `tests/test_data_sources.py`.

## Task 1: Persistent Backtest Result Store

**Files:**
- Create: `tradebot/storage.py`
- Test: `tests/test_storage.py`

- [ ] **Step 1: Write failing tests**

Add tests that create a temporary `BacktestResultStore`, append two results, reload a new store from the same path, verify recent ordering, detail lookup, and clear behavior.

- [ ] **Step 2: Run the focused test**

Run: `PYTHONPATH=. python3 -m unittest tests.test_storage -v`

Expected before implementation: import failure for `tradebot.storage`.

- [ ] **Step 3: Implement `BacktestResultStore`**

Implement:

- `append(result: dict) -> dict`
- `list_recent(limit: int = 20) -> list[dict]`
- `get(result_id: str) -> dict | None`
- `clear() -> None`

Storage must write UTF-8 JSON atomically through a temporary file in the same directory.

- [ ] **Step 4: Re-run focused test**

Run: `PYTHONPATH=. python3 -m unittest tests.test_storage -v`

Expected: all storage tests pass.

## Task 2: Backtest Serialization and Result Detail API

**Files:**
- Create: `tradebot/serialization.py`
- Modify: `tradebot/server.py`
- Test: `tests/test_server.py`

- [ ] **Step 1: Write failing tests**

Add tests that run a backtest, verify the stored history includes `resultId`, `configSnapshot`, `executionAssumptions`, `assets`, `equityCurve`, `trades`, `orderIntents`, and `riskEvents`, then fetch the detail by id through `build_backtest_result_detail_response(result_id)`.

- [ ] **Step 2: Run the focused test**

Run: `PYTHONPATH=. python3 -m unittest tests.test_server -v`

Expected before implementation: missing detail function or missing fields.

- [ ] **Step 3: Implement serialization and detail response**

Add serializers for trades and result snapshots. Update server backtest history so `BACKTEST_RESULTS` remains an in-memory compatibility mirror, but full results are also written through `BACKTEST_STORE`.

- [ ] **Step 4: Re-run focused test**

Run: `PYTHONPATH=. python3 -m unittest tests.test_server -v`

Expected: server tests pass.

## Task 3: DataSourceFactory-Driven K-Line API

**Files:**
- Modify: `tradebot/server.py`
- Test: `tests/test_server.py`, `tests/test_data_sources.py`

- [ ] **Step 1: Write failing tests**

Add tests that call `build_klines_response("NVDA", "1m", source="Synthetic")` and assert the response includes `source`, `dataStatus`, real candle arrays, and no silent fallback. Add a bad source test that returns HTTP 400.

- [ ] **Step 2: Run focused tests**

Run: `PYTHONPATH=. python3 -m unittest tests.test_server tests.test_data_sources -v`

Expected before implementation: signature mismatch or missing source metadata.

- [ ] **Step 3: Implement source routing**

Update `build_klines_response` to call `DataSourceFactory.get_source(source).get_default_candles(...)`. Keep `Synthetic` as explicit demo default, but label it clearly in response metadata. Do not silently fallback on unknown source.

- [ ] **Step 4: Re-run focused tests**

Run: `PYTHONPATH=. python3 -m unittest tests.test_server tests.test_data_sources -v`

Expected: tests pass.

## Task 4: Traceable Order Intents and Risk Events in Backtests

**Files:**
- Modify: `tradebot/backtest.py`
- Modify: `tradebot/serialization.py`
- Test: `tests/test_backtest.py`

- [ ] **Step 1: Write failing tests**

Add tests that force a buy signal and verify `BacktestResult.order_intents` contains a normalized record with `sourceSignal=OPEN_T`, `side=buy`, `quoteAmount`, `strategyId`, and `paperOnly=true`. Add a high-spread test and verify rejected signals record a `risk_event` with `reason` mentioning spread.

- [ ] **Step 2: Run focused test**

Run: `PYTHONPATH=. python3 -m unittest tests.test_backtest -v`

Expected before implementation: empty `order_intents` or empty `risk_events`.

- [ ] **Step 3: Implement records**

Convert generated `BUY` to `OPEN_T` or `ADD_T`, generated `SELL` to `CLOSE_T`, and append order-intent dictionaries before execution decisions. Append risk events when spread quality fails, cash is insufficient, grid spacing blocks, or T bucket cap blocks.

- [ ] **Step 4: Re-run focused test**

Run: `PYTHONPATH=. python3 -m unittest tests.test_backtest -v`

Expected: tests pass.

## Task 5: Paper Reset and Stronger Paper Risk Checks

**Files:**
- Modify: `tradebot/execution.py`
- Modify: `tradebot/server.py`
- Test: `tests/test_execution.py`, `tests/test_server.py`

- [ ] **Step 1: Write failing tests**

Add tests that paper orders reject stale market data, spread above limit, and daily loss cap breaches. Add server tests for `build_paper_reset_response()` clearing cash, positions, and order log.

- [ ] **Step 2: Run focused tests**

Run: `PYTHONPATH=. python3 -m unittest tests.test_execution tests.test_server -v`

Expected before implementation: missing limits and reset function.

- [ ] **Step 3: Implement risk limits and reset endpoint**

Extend `RiskLimits` with `max_spread_pct`, `market_data_max_age_sec`, and `daily_loss_limit_quote`. Extend `PaperExecutionAdapter.execute(...)` to accept `spread_pct`, `market_data_age_sec`, and `daily_loss_quote`. Add server helper and HTTP route `POST /api/paper/reset`.

- [ ] **Step 4: Re-run focused tests**

Run: `PYTHONPATH=. python3 -m unittest tests.test_execution tests.test_server -v`

Expected: tests pass.

## Task 6: Backtest API Uses Selected Data Source

**Files:**
- Modify: `tradebot/server.py`
- Test: `tests/test_server.py`

- [ ] **Step 1: Write failing tests**

Add tests that `build_backtest_response` accepts `source="Synthetic"`, returns `source`, `configSnapshot`, `executionAssumptions`, and persisted full detail. Add an unknown source test that returns 400.

- [ ] **Step 2: Run focused tests**

Run: `PYTHONPATH=. python3 -m unittest tests.test_server -v`

Expected before implementation: source ignored or missing metadata.

- [ ] **Step 3: Implement source-aware backtest loading**

Load candles via `DataSourceFactory` per requested symbol, run `run_backtest`, aggregate metrics, persist the full result, and keep the existing `summary/assets` response shape for frontend compatibility.

- [ ] **Step 4: Re-run focused tests**

Run: `PYTHONPATH=. python3 -m unittest tests.test_server -v`

Expected: tests pass.

## Task 7: Full Verification

**Files:**
- No new files.

- [ ] **Step 1: Run focused pytest contracts**

Run: `UV_CACHE_DIR=.uv-cache PYTHONPATH=. uv run pytest tests/test_api_split.py tests/test_frontend_files.py tests/test_server.py -q`

Expected: pass.

- [ ] **Step 2: Run full unit suite**

Run: `PYTHONPATH=. python3 -m unittest discover -s tests -v`

Expected: pass.

- [ ] **Step 3: Review diff**

Run: `git diff -- tradebot tests docs/superpowers/plans`

Expected: diff is limited to planned backend work and plan documentation.

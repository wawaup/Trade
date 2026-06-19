# Trade QuantDinger Thin Platform Design

## Goal

Build Trade into a focused quant research and paper-trading platform by reusing suitable QuantDinger backend code patterns and rebuilding the weak current frontend into a QuantDinger-inspired dashboard. The first release must keep Trade lightweight: one clear workflow from data selection to T-strategy backtest, result inspection, and paper order simulation.

## Current Context

Trade currently has a small Python backend and static frontend:

- `tradebot/data.py`: Binance-style klines, CSV, session helpers, synthetic data.
- `tradebot/strategy.py`: hard-coded T strategy signal generation.
- `tradebot/backtest.py`: simple cash/T-bucket simulation.
- `tradebot/server.py`: static HTTP server and JSON endpoints.
- `web/index.html`, `web/app.js`, `web/styles.css`: current dashboard.

`QuantDinger-main` is a larger production-grade platform. This local copy includes backend, docs, screenshots, and MCP server code. It does not include the Vue frontend source; QuantDinger's README says the web UI source lives in a sibling `QuantDinger-Vue` repository. Therefore, Trade can directly reuse backend pure models/helpers where appropriate, but the frontend must be recreated from screenshots and UX structure rather than copied source files.

## Product Slice

The first implementation slice is:

1. Open a redesigned Trade dashboard.
2. Select symbols and a data source.
3. Run the current T strategy backtest.
4. Inspect K-line/VWAP, equity curve, metrics, trades, risk checks, and execution assumptions.
5. Persist or export the result.
6. View paper order intents produced by the strategy, without enabling real-money trading.

This deliberately excludes full user accounts, billing, OAuth, MCP, multi-exchange live execution, and QuantDinger's complete SaaS platform.

## Architecture

Trade will remain a lightweight Python + static web app for this phase.

```text
web/
  index.html        QuantDinger-inspired shell and page structure
  app.js            view state, API calls, chart rendering, table rendering
  styles.css        polished dashboard design

tradebot/
  data.py           existing Candle model consumers stay compatible
  data_sources.py   new DataSourceFactory-style abstraction
  strategy.py       existing T strategy remains the signal source
  execution.py      new OrderIntent, FillSnapshot, PaperExecutionAdapter
  risk.py           QuantDinger-inspired risk helpers
  backtest.py       upgraded result shape and trade/order recording
  server.py         API endpoints for state, backtest, results, paper orders

docs/
  execution.md      signal/execution contract for Trade
```

The important boundary is: strategies produce signals, signals become order intents, and execution adapters fill or reject those intents. Strategy code must not call a real exchange API.

## Direct Reuse From QuantDinger

Reuse by copying or closely adapting small, isolated code where the dependency surface is low:

- `OrderIntent`, `FillSnapshot`, `PositionSnapshot` concepts from `backend_api_python/app/services/live_trading/contracts.py`.
- `signal_to_order_sides` style normalization, adapted to Trade's T-bucket vocabulary.
- `coerce_fee_rate` and `trailing_exit_locks_net_profit` from `backend_api_python/app/utils/risk_guard.py`.
- `DataSourceFactory` naming and explicit-market design from `backend_api_python/app/data_sources/factory.py`, simplified to `Synthetic`, `CSV`, and `Binance`.
- Indicator warmup idea from `BacktestService`, implemented locally without PostgreSQL.
- Signal/execution contract language from `docs/SIGNAL_EXECUTION_STANDARD.md`, rewritten for Trade.

Do not directly copy large Flask routes, PostgreSQL migrations, user services, billing, OAuth, MCP routes, or complete exchange clients in this phase.

## Frontend Design

The frontend will follow QuantDinger's product shape but stay purpose-built for Trade:

- Left navigation: Research, Backtest, Paper Orders, Data/Results.
- Left control panel on work pages: symbols, sample range, friction, T-bucket settings, run button.
- Main center panel: K-line/VWAP chart and equity curve.
- Right or lower panels: key metrics, risk lights, assumptions, trades table, result history.
- Paper Orders page: standard order intents, simulated fills, rejection reasons, cash/position summary.

The UI should feel like a trading workbench, not a landing page. It should use dense but readable panels, clear tabs, stable chart/table dimensions, concise labels, and no marketing sections.

## Backend API

Keep endpoints simple and JSON-first:

- `GET /api/state`: dashboard state.
- `GET /api/data-sources`: available sources and current status.
- `GET /api/klines?symbol=&resolution=&source=`: chart data.
- `POST /api/backtest/run`: run T-strategy backtest.
- `GET /api/backtest/results`: recent stored results.
- `GET /api/backtest/results/<id>`: one result with trades and assumptions.
- `GET /api/paper/orders`: current paper order log.
- `POST /api/paper/reset`: reset paper account state.

If persistence is added in the first implementation, use local JSON files under a project data directory rather than introducing PostgreSQL.

## Backtest Result Contract

Each result should include:

- `resultId`
- `createdAt`
- `engineVersion`
- `configSnapshot`
- `symbols`
- `executionAssumptions`
- `summary`
- `assetRows`
- `equityCurve`
- `trades`
- `orderIntents`
- `riskEvents`

This makes frontend rendering deterministic and allows later comparison between strategy versions.

## Paper-First Trading Design

Real trading is not enabled in the first release. The first release builds the shape needed for safe live trading later.

### Trading Layers

1. `Signal`: strategy-level decision such as `OPEN_T`, `ADD_T`, `REDUCE_T`, `CLOSE_T`, or `PAUSE`.
2. `OrderIntent`: standardized order request with symbol, side, quote amount, quantity, limit/market choice, source strategy, reduce-only flag, and risk metadata.
3. `RiskCheck`: validates account cash, T-bucket cap, spread, stale data, daily loss, and max order size.
4. `ExecutionAdapter`: fills or rejects the order. The first adapter is `PaperExecutionAdapter`.
5. `FillSnapshot`: normalized fill result with filled quantity, average price, fee, status, and reason.

### Safety Gates

- Default mode is always `paper_only=true`.
- No real exchange adapter is created in the first phase.
- Future live trading must require an explicit environment flag such as `LIVE_TRADING_ENABLED=true`.
- Every order must record signal source, config snapshot, risk checks, and whether it is paper or live.
- Any stale market data, stale account state, missing price, invalid quantity, max spread breach, or daily loss breach rejects the order.
- The first UI must not include a real-money submit button.

The first release's target flow is:

```text
strategy signal -> order intent -> risk check -> paper fill/reject -> cash/position update -> frontend order log
```

## Error Handling

- API responses return explicit `error` messages for invalid input.
- Backtest failures include the symbol, data source, and config section that failed.
- Paper orders rejected by risk checks must show a human-readable `reason`.
- If the chart library fails to load, the page shows a non-blocking chart error and still renders tables.
- If QuantDinger-style copied helpers are adapted, tests lock their edge cases before UI work depends on them.

## Testing

Use focused `unittest` tests consistent with the current project:

- Data source factory normalizes source names and returns Candle-compatible data.
- Risk helpers handle decimal fee rates and profit-lock checks.
- Order intent conversion maps Trade signals correctly.
- Paper execution updates cash/position and rejects unsafe orders.
- Backtest result includes the new stable contract fields.
- Server endpoints return JSON shapes used by the frontend.
- Frontend files contain required page roots and script/style references.

Run:

```bash
python3 -m unittest discover -s tests
```

## Migration Sequence

1. Add execution contract docs and tests for new pure backend models.
2. Add `tradebot/execution.py` and `tradebot/risk.py`.
3. Add simplified data source factory while preserving existing `tradebot/data.py`.
4. Upgrade backtest result shape without changing the strategy rules.
5. Add paper execution log and endpoints.
6. Rebuild frontend shell around the new API shapes.
7. Polish visual layout using QuantDinger screenshots as reference.

## Out Of Scope

- Real exchange order placement.
- User login and roles.
- QuantDinger MCP server integration.
- Billing and membership.
- OAuth.
- Full Vue app migration.
- PostgreSQL deployment.
- Multi-tenant SaaS operations.

## Approval

Approved direction: Option C, a thin platform slice that improves frontend and backend together around the core research-to-backtest-to-paper workflow.

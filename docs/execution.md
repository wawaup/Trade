# Trade Signal and Execution Contract

## Scope

This contract covers Trade's T-bucket strategy research, backtests, and paper execution. Real exchange execution is out of scope for the current release.

## Strategy Boundary

Strategy code produces signals only. It must not call exchange APIs, mutate account state directly, or decide final fill prices.

## Signal Vocabulary

| Signal | Meaning |
|--------|---------|
| `OPEN_T` | Open the first T-bucket long position. |
| `ADD_T` | Add another T-bucket layer. |
| `REDUCE_T` | Partially reduce an existing T-bucket position. |
| `CLOSE_T` | Fully close the T-bucket position. |
| `PAUSE` | Explicitly skip trading because a guardrail is active. |

## Order Intent

Every executable signal becomes an `OrderIntent` with symbol, side, quote amount, optional quantity, source signal, source strategy, paper/live mode, and risk metadata.

## Fill Timing

Backtests fill on the current modeled candle close with configured slippage unless a future task explicitly changes the engine to next-bar-open fills. The result must expose this as `executionAssumptions.fillTiming`.

## Friction

Backtests and paper fills apply:

- fee model
- slippage bps
- synthetic spread bps
- max spread rejection

## Risk Rejection

Orders are rejected when:

- price is missing or non-positive
- quote amount is non-positive
- account cash is insufficient
- T-bucket cap would be exceeded
- spread exceeds max spread
- market data is stale
- daily loss cap is breached

## Paper-Only Default

The current release supports only paper execution. A future live adapter must require an explicit environment flag such as `LIVE_TRADING_ENABLED=true` and must record all risk checks and order provenance.

# Trade QuantDinger Thin Platform Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a focused Trade platform slice with a QuantDinger-inspired frontend, upgraded backtest result contract, and paper-first execution architecture.

**Architecture:** Keep the app as lightweight Python plus static web files. Reuse small QuantDinger backend concepts by copying/adapting isolated dataclasses and helpers, while rebuilding the frontend from the local Trade app and QuantDinger screenshots rather than importing the absent Vue source.

**Tech Stack:** Python standard library, `unittest`, existing static HTML/CSS/JS, `lightweight-charts`, current `tradebot` modules.

---

## File Structure

- Create `docs/execution.md`: Trade's signal, fill, risk, and paper/live safety contract.
- Create `tradebot/risk.py`: QuantDinger-inspired fee-rate and net-profit risk helpers.
- Create `tradebot/execution.py`: Trade signal vocabulary, order intent models, paper execution adapter.
- Create `tradebot/data_sources.py`: simplified data source factory wrapping synthetic, CSV, and Binance-compatible data.
- Modify `tradebot/backtest.py`: preserve existing return fields while adding stable result IDs, config snapshots, order intents, assumptions, and risk events.
- Modify `tradebot/server.py`: expose data sources, backtest result history, and paper order endpoints.
- Modify `web/index.html`: replace current two-page shell with Research, Backtest, Paper Orders, and Data/Results pages.
- Modify `web/app.js`: render new API shapes, paper orders, result history, and improved chart/table state.
- Modify `web/styles.css`: QuantDinger-inspired dashboard layout and responsive polish.
- Add tests under `tests/`: focused backend contract tests and frontend static checks.

## Task 1: Execution Contract Document

**Files:**
- Create: `docs/execution.md`
- Test: no code test; review with `sed`

- [ ] **Step 1: Create the execution contract document**

Add `docs/execution.md`:

```markdown
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
```

- [ ] **Step 2: Review the document**

Run: `sed -n '1,220p' docs/execution.md`

Expected: the document contains signal vocabulary, fill timing, friction, risk rejection, and paper-only default sections.

- [ ] **Step 3: Commit**

```bash
git add docs/execution.md
git commit -m "docs: add trade execution contract"
```

## Task 2: Risk Helpers

**Files:**
- Create: `tradebot/risk.py`
- Test: `tests/test_risk.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_risk.py`:

```python
import unittest

from tradebot.risk import coerce_fee_rate, trailing_exit_locks_net_profit


class RiskHelperTests(unittest.TestCase):
    def test_coerce_fee_rate_accepts_decimal_rate(self):
        self.assertEqual(coerce_fee_rate(0.001), 0.001)

    def test_coerce_fee_rate_defensively_interprets_percent_like_value(self):
        self.assertAlmostEqual(coerce_fee_rate(0.1), 0.001)

    def test_coerce_fee_rate_clamps_negative_to_zero(self):
        self.assertEqual(coerce_fee_rate(-0.5), 0.0)

    def test_long_trailing_exit_must_cover_round_trip_fee(self):
        self.assertFalse(
            trailing_exit_locks_net_profit(
                "long",
                entry_price=100.0,
                exit_price=100.10,
                fee_rate=0.001,
            )
        )
        self.assertTrue(
            trailing_exit_locks_net_profit(
                "long",
                entry_price=100.0,
                exit_price=100.30,
                fee_rate=0.001,
            )
        )

    def test_short_trailing_exit_must_cover_round_trip_fee(self):
        self.assertFalse(
            trailing_exit_locks_net_profit(
                "short",
                entry_price=100.0,
                exit_price=99.90,
                fee_rate=0.001,
            )
        )
        self.assertTrue(
            trailing_exit_locks_net_profit(
                "short",
                entry_price=100.0,
                exit_price=99.70,
                fee_rate=0.001,
            )
        )


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_risk -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'tradebot.risk'`.

- [ ] **Step 3: Implement risk helpers**

Create `tradebot/risk.py`:

```python
DEFAULT_TAKER_FEE_RATE = 0.001


def coerce_fee_rate(value, default=DEFAULT_TAKER_FEE_RATE):
    try:
        fee = float(value if value is not None else default)
    except (TypeError, ValueError):
        fee = float(default)
    if fee < 0:
        return 0.0
    if fee > 0.05:
        fee = fee / 100.0
    return min(fee, 0.05)


def trailing_exit_locks_net_profit(side, *, entry_price, exit_price, fee_rate, extra_buffer=0.0):
    try:
        entry = float(entry_price or 0.0)
        exit_px = float(exit_price or 0.0)
    except (TypeError, ValueError):
        return False
    if entry <= 0 or exit_px <= 0:
        return False

    fee = coerce_fee_rate(fee_rate, default=0.0)
    try:
        extra = max(0.0, float(extra_buffer or 0.0))
    except (TypeError, ValueError):
        extra = 0.0
    min_move = max(0.0, 2.0 * fee + extra)

    if side == "long":
        return exit_px >= entry * (1.0 + min_move)
    if side == "short":
        return exit_px <= entry * (1.0 - min_move)
    return False
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.test_risk -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tradebot/risk.py tests/test_risk.py
git commit -m "feat: add trade risk helpers"
```

## Task 3: Paper Execution Models

**Files:**
- Create: `tradebot/execution.py`
- Test: `tests/test_execution.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_execution.py`:

```python
import unittest

from tradebot.execution import (
    OrderIntent,
    PaperAccount,
    PaperExecutionAdapter,
    RiskLimits,
    order_intent_from_signal,
)


class ExecutionModelTests(unittest.TestCase):
    def test_order_intent_from_open_signal(self):
        intent = order_intent_from_signal(
            signal="OPEN_T",
            symbol="NVDA",
            quote_amount=350.0,
            strategy_id="t-vwap",
        )
        self.assertEqual(intent.symbol, "NVDA")
        self.assertEqual(intent.side, "buy")
        self.assertEqual(intent.source_signal, "OPEN_T")
        self.assertTrue(intent.paper_only)

    def test_order_intent_from_close_signal_is_reduce_only_sell(self):
        intent = order_intent_from_signal(
            signal="CLOSE_T",
            symbol="NVDA",
            quote_amount=0.0,
            quantity=2.0,
            strategy_id="t-vwap",
        )
        self.assertEqual(intent.side, "sell")
        self.assertTrue(intent.reduce_only)

    def test_paper_buy_updates_cash_and_position(self):
        account = PaperAccount(cash=1000.0)
        adapter = PaperExecutionAdapter(account)
        intent = OrderIntent(symbol="NVDA", side="buy", quote_amount=500.0, source_signal="OPEN_T")
        fill = adapter.execute(intent, price=100.0, fee=1.0)
        self.assertEqual(fill.status, "filled")
        self.assertAlmostEqual(account.cash, 499.0)
        self.assertAlmostEqual(account.positions["NVDA"], 4.99)

    def test_paper_rejects_insufficient_cash(self):
        account = PaperAccount(cash=100.0)
        adapter = PaperExecutionAdapter(account)
        intent = OrderIntent(symbol="NVDA", side="buy", quote_amount=500.0, source_signal="OPEN_T")
        fill = adapter.execute(intent, price=100.0, fee=1.0)
        self.assertEqual(fill.status, "rejected")
        self.assertIn("insufficient cash", fill.reason)

    def test_paper_rejects_t_bucket_cap(self):
        account = PaperAccount(cash=1000.0)
        adapter = PaperExecutionAdapter(account, RiskLimits(max_t_position_quote=300.0))
        intent = OrderIntent(symbol="NVDA", side="buy", quote_amount=500.0, source_signal="OPEN_T")
        fill = adapter.execute(intent, price=100.0, fee=1.0)
        self.assertEqual(fill.status, "rejected")
        self.assertIn("T bucket cap", fill.reason)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_execution -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'tradebot.execution'`.

- [ ] **Step 3: Implement execution models**

Create `tradebot/execution.py`:

```python
from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class OrderIntent:
    symbol: str
    side: str
    quote_amount: float = 0.0
    quantity: float = 0.0
    order_type: str = "market"
    source_signal: str = ""
    strategy_id: str = ""
    reduce_only: bool = False
    paper_only: bool = True
    risk_metadata: dict = field(default_factory=dict)


@dataclass(frozen=True)
class FillSnapshot:
    symbol: str
    side: str
    filled_qty: float
    avg_price: float
    fee: float
    status: str
    reason: str = ""


@dataclass
class PositionSnapshot:
    symbol: str
    quantity: float
    avg_price: float = 0.0


@dataclass
class RiskLimits:
    max_t_position_quote: Optional[float] = None
    max_order_quote: Optional[float] = None


@dataclass
class PaperAccount:
    cash: float
    positions: dict = field(default_factory=dict)


def order_intent_from_signal(signal, symbol, quote_amount, strategy_id, quantity=0.0):
    sig = str(signal or "").strip().upper()
    if sig in {"OPEN_T", "ADD_T"}:
        return OrderIntent(
            symbol=str(symbol).upper(),
            side="buy",
            quote_amount=float(quote_amount or 0.0),
            quantity=float(quantity or 0.0),
            source_signal=sig,
            strategy_id=str(strategy_id or ""),
        )
    if sig in {"REDUCE_T", "CLOSE_T"}:
        return OrderIntent(
            symbol=str(symbol).upper(),
            side="sell",
            quote_amount=float(quote_amount or 0.0),
            quantity=float(quantity or 0.0),
            source_signal=sig,
            strategy_id=str(strategy_id or ""),
            reduce_only=True,
        )
    raise ValueError(f"Unsupported Trade signal: {signal}")


class PaperExecutionAdapter:
    def __init__(self, account, risk_limits=None):
        self.account = account
        self.risk_limits = risk_limits or RiskLimits()
        self.fills = []

    def execute(self, intent, *, price, fee=0.0):
        try:
            px = float(price)
            order_fee = float(fee or 0.0)
        except (TypeError, ValueError):
            return self._reject(intent, "invalid price or fee")
        if px <= 0:
            return self._reject(intent, "invalid price")

        if intent.side == "buy":
            fill = self._buy(intent, px, order_fee)
        elif intent.side == "sell":
            fill = self._sell(intent, px, order_fee)
        else:
            fill = self._reject(intent, f"invalid side: {intent.side}")
        self.fills.append(fill)
        return fill

    def _buy(self, intent, price, fee):
        quote = float(intent.quote_amount or 0.0)
        if quote <= 0:
            return self._reject(intent, "buy quote amount must be positive")
        if self.risk_limits.max_order_quote is not None and quote > self.risk_limits.max_order_quote:
            return self._reject(intent, "max order quote exceeded")
        if self.risk_limits.max_t_position_quote is not None:
            current_quote = self.account.positions.get(intent.symbol, 0.0) * price
            if current_quote + quote > self.risk_limits.max_t_position_quote:
                return self._reject(intent, "T bucket cap exceeded")
        if self.account.cash < quote:
            return self._reject(intent, "insufficient cash")

        qty = max(0.0, (quote - fee) / price)
        self.account.cash -= quote
        self.account.positions[intent.symbol] = self.account.positions.get(intent.symbol, 0.0) + qty
        return FillSnapshot(intent.symbol, intent.side, qty, price, fee, "filled")

    def _sell(self, intent, price, fee):
        held = self.account.positions.get(intent.symbol, 0.0)
        qty = float(intent.quantity or held)
        if qty <= 0:
            return self._reject(intent, "sell quantity must be positive")
        if intent.reduce_only and qty > held:
            qty = held
        if qty <= 0:
            return self._reject(intent, "no position to reduce")

        gross = qty * price
        self.account.cash += gross - fee
        remaining = max(0.0, held - qty)
        if remaining:
            self.account.positions[intent.symbol] = remaining
        else:
            self.account.positions.pop(intent.symbol, None)
        return FillSnapshot(intent.symbol, intent.side, qty, price, fee, "filled")

    def _reject(self, intent, reason):
        return FillSnapshot(
            symbol=intent.symbol,
            side=intent.side,
            filled_qty=0.0,
            avg_price=0.0,
            fee=0.0,
            status="rejected",
            reason=reason,
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.test_execution -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tradebot/execution.py tests/test_execution.py
git commit -m "feat: add paper execution models"
```

## Task 4: Data Source Factory

**Files:**
- Create: `tradebot/data_sources.py`
- Test: `tests/test_data_sources.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_data_sources.py`:

```python
import unittest

from tradebot.data_sources import DataSourceFactory
from tradebot.models import Candle


class DataSourceFactoryTests(unittest.TestCase):
    def test_normalize_source_aliases(self):
        self.assertEqual(DataSourceFactory.normalize_source("synthetic"), "Synthetic")
        self.assertEqual(DataSourceFactory.normalize_source("csv"), "CSV")
        self.assertEqual(DataSourceFactory.normalize_source("binance"), "Binance")

    def test_unknown_source_raises_value_error(self):
        with self.assertRaises(ValueError):
            DataSourceFactory.normalize_source("mystery")

    def test_synthetic_source_returns_candles(self):
        source = DataSourceFactory.get_source("synthetic")
        daily, intraday = source.get_default_candles(symbol="NVDA")
        self.assertTrue(daily)
        self.assertTrue(intraday)
        self.assertIsInstance(daily[0], Candle)
        self.assertIsInstance(intraday[0], Candle)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_data_sources -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'tradebot.data_sources'`.

- [ ] **Step 3: Implement data source factory**

Create `tradebot/data_sources.py`:

```python
from pathlib import Path

from tradebot.data import fetch_spot_klines, generate_synthetic_spcx, read_candles_csv


class SyntheticDataSource:
    name = "Synthetic"

    def get_default_candles(self, symbol="SPCX", **kwargs):
        seed = sum(ord(ch) for ch in str(symbol or "SPCX"))
        return generate_synthetic_spcx(seed=seed)


class CSVDataSource:
    name = "CSV"

    def get_default_candles(self, daily_path=None, intraday_path=None, **kwargs):
        if not daily_path or not intraday_path:
            raise ValueError("CSV source requires daily_path and intraday_path")
        return read_candles_csv(Path(daily_path)), read_candles_csv(Path(intraday_path))


class BinanceDataSource:
    name = "Binance"

    def get_default_candles(self, symbol="BTCUSDT", daily_limit=60, intraday_limit=500, **kwargs):
        return (
            fetch_spot_klines(symbol, "1d", daily_limit),
            fetch_spot_klines(symbol, "1m", intraday_limit),
        )


class DataSourceFactory:
    _ALIASES = {
        "synthetic": "Synthetic",
        "mock": "Synthetic",
        "demo": "Synthetic",
        "csv": "CSV",
        "file": "CSV",
        "binance": "Binance",
        "spot": "Binance",
    }
    _SOURCES = {
        "Synthetic": SyntheticDataSource,
        "CSV": CSVDataSource,
        "Binance": BinanceDataSource,
    }

    @classmethod
    def normalize_source(cls, source):
        raw = str(source or "Synthetic").strip()
        if raw in cls._SOURCES:
            return raw
        key = raw.lower().replace("-", "_").replace(" ", "")
        if key in cls._ALIASES:
            return cls._ALIASES[key]
        raise ValueError(f"Unsupported data source: {source}")

    @classmethod
    def get_source(cls, source):
        normalized = cls.normalize_source(source)
        return cls._SOURCES[normalized]()

    @classmethod
    def list_sources(cls):
        return [
            {"id": "Synthetic", "label": "Synthetic demo data", "requiresNetwork": False},
            {"id": "CSV", "label": "Local CSV files", "requiresNetwork": False},
            {"id": "Binance", "label": "Binance public klines", "requiresNetwork": True},
        ]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.test_data_sources -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tradebot/data_sources.py tests/test_data_sources.py
git commit -m "feat: add trade data source factory"
```

## Task 5: Backtest Result Contract

**Files:**
- Modify: `tradebot/backtest.py`
- Test: `tests/test_backtest.py`

- [ ] **Step 1: Add failing test for stable result fields**

Append this test to `tests/test_backtest.py`:

```python
    def test_backtest_result_exposes_platform_contract_fields(self):
        daily, intraday = generate_synthetic_spcx()
        result = run_backtest(daily, intraday, BacktestConfig(), StrategyConfig())
        self.assertTrue(result.result_id.startswith("bt_"))
        self.assertEqual(result.engine_version, "tradebot-backtest-v2")
        self.assertIn("fillTiming", result.execution_assumptions)
        self.assertIn("slippageBps", result.config_snapshot)
        self.assertIsInstance(result.order_intents, list)
        self.assertIsInstance(result.risk_events, list)
```

Ensure `tests/test_backtest.py` imports `generate_synthetic_spcx`, `BacktestConfig`, `run_backtest`, and `StrategyConfig`. If they already exist, do not duplicate imports.

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_backtest -v`

Expected: FAIL with an `AttributeError` for `result_id`.

- [ ] **Step 3: Extend `BacktestResult` dataclass**

Modify `tradebot/backtest.py`:

```python
from dataclasses import dataclass, field
import hashlib
import json
```

Update `BacktestResult`:

```python
@dataclass(frozen=True)
class BacktestResult:
    starting_quote: float
    ending_equity: float
    cash: float
    t_position_qty: float
    t_position_value: float
    max_t_position_quote: float
    fees_paid: float
    trades: list[Trade]
    equity_curve: list[float]
    trade_pnls: list[float]
    result_id: str = ""
    engine_version: str = "tradebot-backtest-v2"
    config_snapshot: dict = field(default_factory=dict)
    execution_assumptions: dict = field(default_factory=dict)
    order_intents: list = field(default_factory=list)
    risk_events: list = field(default_factory=list)
```

- [ ] **Step 4: Populate contract fields in `run_backtest`**

Before returning `BacktestResult`, add:

```python
    config_snapshot = {
        "startingQuote": backtest_config.starting_quote,
        "coreAllocationPct": backtest_config.core_allocation_pct,
        "slippageBps": backtest_config.slippage_bps,
        "syntheticSpreadBps": backtest_config.synthetic_spread_bps,
        "maxSpreadPct": backtest_config.max_spread_pct,
        "strategy": strategy_config.__dict__,
    }
    execution_assumptions = {
        "fillTiming": "modeled_current_close",
        "feeModel": "BinanceStockFeeModel",
        "paperOnly": True,
        "engineVersion": "tradebot-backtest-v2",
    }
    result_hash = hashlib.sha1(
        json.dumps(config_snapshot, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:12]
```

Then pass these fields in the return:

```python
        result_id=f"bt_{result_hash}",
        engine_version="tradebot-backtest-v2",
        config_snapshot=config_snapshot,
        execution_assumptions=execution_assumptions,
        order_intents=[],
        risk_events=[],
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python3 -m unittest tests.test_backtest -v`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add tradebot/backtest.py tests/test_backtest.py
git commit -m "feat: expose stable backtest result contract"
```

## Task 6: Paper Orders and Result APIs

**Files:**
- Modify: `tradebot/server.py`
- Test: `tests/test_server.py`

- [ ] **Step 1: Add failing server tests**

Add tests to `tests/test_server.py` for pure response builders:

```python
    def test_build_data_sources_response(self):
        status, body = build_data_sources_response()
        self.assertEqual(status, 200)
        self.assertIn("sources", body)
        self.assertTrue(any(row["id"] == "Synthetic" for row in body["sources"]))

    def test_build_paper_orders_response(self):
        status, body = build_paper_orders_response()
        self.assertEqual(status, 200)
        self.assertIn("orders", body)
        self.assertIn("account", body)
        self.assertTrue(body["account"]["paperOnly"])
```

Add imports for `build_data_sources_response` and `build_paper_orders_response`.

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_server -v`

Expected: FAIL with import errors for the new functions.

- [ ] **Step 3: Add response builders**

In `tradebot/server.py`, import:

```python
from tradebot.data_sources import DataSourceFactory
from tradebot.execution import PaperAccount
```

Add module globals:

```python
PAPER_ACCOUNT = PaperAccount(cash=10_000.0)
PAPER_ORDER_LOG = []
BACKTEST_RESULTS = []
```

Add functions:

```python
def build_data_sources_response():
    return 200, {"sources": DataSourceFactory.list_sources()}


def build_paper_orders_response():
    return 200, {
        "account": {
            "paperOnly": True,
            "cash": PAPER_ACCOUNT.cash,
            "positions": PAPER_ACCOUNT.positions,
        },
        "orders": PAPER_ORDER_LOG,
    }


def build_backtest_results_response():
    return 200, {"results": BACKTEST_RESULTS[-20:]}
```

- [ ] **Step 4: Wire GET routes**

In `DashboardHandler.do_GET`, add routes before static fallback:

```python
        if parsed.path == "/api/data-sources":
            status, body = build_data_sources_response()
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if parsed.path == "/api/paper/orders":
            status, body = build_paper_orders_response()
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if parsed.path == "/api/backtest/results":
            status, body = build_backtest_results_response()
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
```

- [ ] **Step 5: Run server tests**

Run: `python3 -m unittest tests.test_server -v`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add tradebot/server.py tests/test_server.py
git commit -m "feat: add platform API response builders"
```

## Task 7: Frontend Shell Static Structure

**Files:**
- Modify: `web/index.html`
- Test: `tests/test_frontend_files.py`

- [ ] **Step 1: Add failing frontend structure test**

Add a test to `tests/test_frontend_files.py`:

```python
    def test_quantdinger_inspired_platform_pages_exist(self):
        html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="researchPage"', html)
        self.assertIn('id="backtestPage"', html)
        self.assertIn('id="paperOrdersPage"', html)
        self.assertIn('id="dataResultsPage"', html)
        self.assertIn('data-page-target="paperOrdersPage"', html)
```

Use the existing `ROOT` helper if present. If the file uses another root variable, follow that existing pattern.

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_frontend_files -v`

Expected: FAIL because new page IDs do not exist.

- [ ] **Step 3: Replace top-level navigation and page roots**

Modify `web/index.html` so the shell has four main buttons:

```html
<button class="active" type="button" data-page-target="researchPage">研究仪表盘</button>
<button type="button" data-page-target="backtestPage">策略回测</button>
<button type="button" data-page-target="paperOrdersPage">Paper订单</button>
<button type="button" data-page-target="dataResultsPage">数据与结果</button>
```

Ensure the file contains sections:

```html
<section class="page active" id="researchPage" aria-labelledby="researchTitle"></section>
<section class="page" id="backtestPage" aria-labelledby="backtestTitle"></section>
<section class="page" id="paperOrdersPage" aria-labelledby="paperOrdersTitle"></section>
<section class="page" id="dataResultsPage" aria-labelledby="dataResultsTitle"></section>
```

Move the existing backtest controls into `backtestPage`, existing live/allocation content into `researchPage` or remove duplicate live labels, and add placeholder tables with IDs:

```html
<tbody id="paperOrderRows"></tbody>
<tbody id="resultHistoryRows"></tbody>
<div id="dataSourceCards"></div>
```

- [ ] **Step 4: Run frontend file tests**

Run: `python3 -m unittest tests.test_frontend_files -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add web/index.html tests/test_frontend_files.py
git commit -m "feat: add platform frontend page shell"
```

## Task 8: Frontend API Rendering

**Files:**
- Modify: `web/app.js`
- Test: `tests/test_frontend_files.py`

- [ ] **Step 1: Add failing frontend JS test**

Add a test to `tests/test_frontend_files.py`:

```python
    def test_platform_frontend_fetches_new_api_endpoints(self):
        js = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
        self.assertIn("/api/data-sources", js)
        self.assertIn("/api/paper/orders", js)
        self.assertIn("/api/backtest/results", js)
        self.assertIn("function renderPaperOrders", js)
        self.assertIn("function renderDataSources", js)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_frontend_files -v`

Expected: FAIL because the JS does not yet fetch those endpoints.

- [ ] **Step 3: Add render functions**

Append or integrate these functions in `web/app.js`:

```javascript
function renderDataSources(payload) {
  const root = document.getElementById("dataSourceCards");
  if (!root) return;
  const sources = payload.sources || [];
  root.innerHTML = sources.map((source) => `
    <article class="source-card">
      <strong>${source.id}</strong>
      <span>${source.label}</span>
      <em>${source.requiresNetwork ? "需要联网" : "本地可用"}</em>
    </article>
  `).join("");
}

function renderPaperOrders(payload) {
  const root = document.getElementById("paperOrderRows");
  if (!root) return;
  const orders = payload.orders || [];
  root.innerHTML = orders.length ? orders.map((order) => `
    <tr>
      <td>${order.symbol || "-"}</td>
      <td>${order.side || "-"}</td>
      <td>${order.sourceSignal || order.source_signal || "-"}</td>
      <td>${order.status || "pending"}</td>
      <td>${order.reason || "-"}</td>
    </tr>
  `).join("") : `<tr><td colspan="5">暂无 Paper 订单。</td></tr>`;
}

function renderResultHistory(payload) {
  const root = document.getElementById("resultHistoryRows");
  if (!root) return;
  const results = payload.results || [];
  root.innerHTML = results.length ? results.map((result) => `
    <tr>
      <td>${result.resultId || result.result_id || "-"}</td>
      <td>${result.createdAt || "-"}</td>
      <td>${result.engineVersion || result.engine_version || "-"}</td>
      <td>${result.summary ? fmtPct(result.summary.totalReturnPct || 0) : "-"}</td>
    </tr>
  `).join("") : `<tr><td colspan="4">暂无保存的回测结果。</td></tr>`;
}
```

- [ ] **Step 4: Fetch new endpoints during init**

Add:

```javascript
async function loadPlatformData() {
  const [sourcesRes, paperRes, resultsRes] = await Promise.all([
    fetch("/api/data-sources"),
    fetch("/api/paper/orders"),
    fetch("/api/backtest/results"),
  ]);
  renderDataSources(await sourcesRes.json());
  renderPaperOrders(await paperRes.json());
  renderResultHistory(await resultsRes.json());
}
```

Call `loadPlatformData()` in the existing initialization path after the DOM is ready.

- [ ] **Step 5: Run frontend file tests**

Run: `python3 -m unittest tests.test_frontend_files -v`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add web/app.js tests/test_frontend_files.py
git commit -m "feat: render platform API data in frontend"
```

## Task 9: QuantDinger-Inspired Styling

**Files:**
- Modify: `web/styles.css`
- Test: `tests/test_frontend_files.py`

- [ ] **Step 1: Add failing CSS contract test**

Add a test to `tests/test_frontend_files.py`:

```python
    def test_platform_styles_define_new_surfaces(self):
        css = (ROOT / "web" / "styles.css").read_text(encoding="utf-8")
        self.assertIn(".platform-layout", css)
        self.assertIn(".source-card", css)
        self.assertIn(".paper-orders-table", css)
        self.assertIn(".result-history-table", css)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.test_frontend_files -v`

Expected: FAIL because new classes are missing.

- [ ] **Step 3: Add layout styles**

Append to `web/styles.css`:

```css
.platform-layout {
  display: grid;
  grid-template-columns: minmax(240px, 320px) minmax(0, 1fr);
  gap: 16px;
  align-items: start;
}

.platform-stack {
  display: grid;
  gap: 16px;
}

.source-card {
  display: grid;
  gap: 6px;
  padding: 14px;
  border: 1px solid rgba(132, 151, 176, 0.25);
  background: rgba(19, 27, 39, 0.72);
  border-radius: 8px;
}

.source-card strong {
  color: #f3f7ff;
}

.source-card span,
.source-card em {
  color: #aab6c8;
  font-style: normal;
}

.paper-orders-table,
.result-history-table {
  width: 100%;
  border-collapse: collapse;
  table-layout: fixed;
}

.paper-orders-table th,
.paper-orders-table td,
.result-history-table th,
.result-history-table td {
  padding: 10px 12px;
  border-bottom: 1px solid rgba(132, 151, 176, 0.18);
  overflow-wrap: anywhere;
}

@media (max-width: 900px) {
  .platform-layout {
    grid-template-columns: 1fr;
  }
}
```

- [ ] **Step 4: Run frontend file tests**

Run: `python3 -m unittest tests.test_frontend_files -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add web/styles.css tests/test_frontend_files.py
git commit -m "style: add platform dashboard surfaces"
```

## Task 10: Full Verification

**Files:**
- All changed files

- [ ] **Step 1: Run focused tests**

Run:

```bash
python3 -m unittest tests.test_risk tests.test_execution tests.test_data_sources tests.test_backtest tests.test_server tests.test_frontend_files -v
```

Expected: PASS.

- [ ] **Step 2: Run full test suite**

Run:

```bash
python3 -m unittest discover -s tests
```

Expected: PASS.

- [ ] **Step 3: Start local server**

Run:

```bash
python3 -m tradebot.server
```

Expected: terminal prints `Trade dashboard running at http://127.0.0.1:8765`.

- [ ] **Step 4: Manually inspect in browser**

Open `http://127.0.0.1:8765` and verify:

- Research page loads without overlapping text.
- Backtest page can run a backtest.
- K-line/VWAP chart renders or shows a graceful chart-library error.
- Paper Orders page shows account state and empty order table.
- Data/Results page shows data source cards and result history placeholder.

- [ ] **Step 5: Stop server**

Use `Ctrl-C` in the server terminal.

- [ ] **Step 6: Commit verification fixes if needed**

If inspection required fixes:

```bash
git add tradebot web tests docs
git commit -m "fix: polish thin platform verification issues"
```

If no fixes were needed, do not create an empty commit.

## Self-Review Notes

- Spec coverage: the plan covers docs, reusable backend helpers, data source abstraction, backtest result contract, paper-first execution, API endpoints, frontend shell, rendering, styling, and verification.
- Placeholder scan: no task uses TBD/TODO/fill-in instructions; each code step includes concrete code or exact structural edits.
- Type consistency: `OrderIntent`, `FillSnapshot`, `PaperAccount`, `RiskLimits`, and endpoint names are consistent across tasks.

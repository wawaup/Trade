import json
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse

from tradebot.allocation import allocation_config_from_dict, allocation_config_to_dict, build_allocations, default_allocation_config
from tradebot.backtest import BacktestConfig, run_backtest
from tradebot.dashboard import build_dashboard_state
from tradebot.data import generate_synthetic_spcx
from tradebot.data_sources import DataSourceFactory
from tradebot.execution import OrderIntent, PaperAccount, PaperExecutionAdapter, RiskLimits
from tradebot.metrics import calculate_metrics
from tradebot.research import AssetProfile
from tradebot.serialization import serialize_asset_result_detail, serialize_backtest_result
from tradebot.storage import BacktestResultStore
from tradebot.strategy import StrategyConfig


ROOT = Path(__file__).resolve().parents[1]
WEB_ROOT = ROOT / "web"
ALLOCATION_CONFIG = default_allocation_config()
PAPER_ACCOUNT = PaperAccount(cash=10_000.0)
PAPER_ORDER_LOG = []
BACKTEST_RESULTS = []
BACKTEST_ENGINE_VERSION = "trade-research-v1"
BACKTEST_STORE = BacktestResultStore(ROOT / "data" / "backtest_results.json")
PAPER_RISK_LIMITS = RiskLimits(
    max_order_quote=5_000.0,
    max_t_position_quote=7_500.0,
    max_spread_pct=0.005,
    daily_loss_limit_quote=500.0,
)

# Per-symbol T-position tracking for grid spacing and layer checks.
PAPER_LAST_BUY_PRICE: dict[str, float] = {}
PAPER_T_LAYERS: dict[str, int] = {}
PAPER_POSITION_COST: dict[str, float] = {}  # total cost basis per symbol
PAPER_DAILY_LOSS: float = 0.0               # accumulated realized loss today (UTC day)


def build_static_state():
    state = build_dashboard_state(allocation_config=ALLOCATION_CONFIG)
    return {
        "allocation": state["allocation"],
        "allocationRows": state["allocationRows"],
        "backtest": state["backtest"],
        "glossary": state["glossary"],
    }


def build_live_state():
    state = build_dashboard_state(allocation_config=ALLOCATION_CONFIG)
    return {"live": state["live"]}


def build_klines_response(
    symbol: str,
    resolution: str,
    source: str = "Synthetic",
    daily_path: Optional[str] = None,
    intraday_path: Optional[str] = None,
):
    try:
        normalized_source = DataSourceFactory.normalize_source(source)
        _, intraday = DataSourceFactory.get_source(normalized_source).get_default_candles(
            symbol=symbol,
            daily_path=daily_path,
            intraday_path=intraday_path,
        )
    except ValueError as exc:
        return 400, {"error": str(exc)}
    except Exception as exc:
        return 502, {"error": str(exc), "source": source}

    candles = []
    vwap = []
    cumulative_quote = 0.0
    cumulative_volume = 0.0
    for candle in intraday[-180:]:
        time_s = int(candle.open_time / 1000)
        candles.append([time_s, candle.open, candle.high, candle.low, candle.close])
        cumulative_quote += candle.quote_volume
        cumulative_volume += candle.volume
        vwap.append([time_s, cumulative_quote / cumulative_volume if cumulative_volume else candle.close])
    status = "demo" if normalized_source == "Synthetic" else "live"
    return 200, {
        "symbol": symbol,
        "resolution": resolution,
        "source": normalized_source,
        "dataStatus": status,
        "candles": candles,
        "vwap": vwap,
    }


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


def _latest_synthetic_price(symbol: str) -> float:
    _, intraday = generate_synthetic_spcx(seed=sum(ord(ch) for ch in symbol) + 2)
    return intraday[-1].close


def build_paper_order_response(raw_body: bytes):
    global PAPER_DAILY_LOSS

    try:
        payload = json.loads(raw_body.decode("utf-8"))
        symbol = str(payload.get("symbol", "NVDA")).upper()
        side = str(payload["side"]).lower()
        quote_amount = float(payload.get("quoteAmount", 0.0))
        order_type = str(payload.get("orderType", "market")).lower()
        price = float(payload.get("price") or _latest_synthetic_price(symbol))
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return 400, {"error": str(exc)}

    if side not in {"buy", "sell"}:
        return 400, {"error": "side must be buy or sell"}
    if order_type != "market":
        return 400, {"error": "only market paper orders are enabled"}

    # --- Server-side risk checks (not delegated to caller) ---
    cfg = StrategyConfig()
    if side == "buy":
        last_price = PAPER_LAST_BUY_PRICE.get(symbol)
        if last_price is not None and price > last_price * (1 - cfg.buy_grid_spacing_pct):
            reason = f"grid spacing not reached (last={last_price:.4f}, now={price:.4f})"
            order = {
                "orderId": f"po-{len(PAPER_ORDER_LOG) + 1:04d}",
                "createdAt": datetime.now(timezone.utc).isoformat(),
                "symbol": symbol, "side": side, "orderType": order_type,
                "quoteAmount": quote_amount, "price": price,
                "filledQty": 0.0, "fee": 0.0, "status": "rejected", "reason": reason,
                "paperOnly": True, "sourceSignal": "OPEN_T",
            }
            PAPER_ORDER_LOG.append(order)
            return 200, {"order": order, "account": build_paper_orders_response()[1]["account"]}

        layers = PAPER_T_LAYERS.get(symbol, 0)
        if layers >= cfg.max_layers:
            reason = f"max layers reached ({layers})"
            order = {
                "orderId": f"po-{len(PAPER_ORDER_LOG) + 1:04d}",
                "createdAt": datetime.now(timezone.utc).isoformat(),
                "symbol": symbol, "side": side, "orderType": order_type,
                "quoteAmount": quote_amount, "price": price,
                "filledQty": 0.0, "fee": 0.0, "status": "rejected", "reason": reason,
                "paperOnly": True, "sourceSignal": "OPEN_T",
            }
            PAPER_ORDER_LOG.append(order)
            return 200, {"order": order, "account": build_paper_orders_response()[1]["account"]}

    quantity = quote_amount / price if side == "sell" and quote_amount > 0 else 0.0
    intent = OrderIntent(
        symbol=symbol,
        side=side,
        quote_amount=quote_amount if side == "buy" else 0.0,
        quantity=quantity,
        order_type=order_type,
        source_signal="OPEN_T" if side == "buy" else "REDUCE_T",
        strategy_id="quick-paper-t",
        reduce_only=side == "sell",
        paper_only=True,
    )
    fee = max(0.0, quote_amount * 0.001)
    fill = PaperExecutionAdapter(PAPER_ACCOUNT, PAPER_RISK_LIMITS).execute(
        intent,
        price=price,
        fee=fee,
        daily_loss_quote=PAPER_DAILY_LOSS,
    )

    # --- Update server-side tracking state after a successful fill ---
    if fill.status == "filled":
        if side == "buy":
            PAPER_LAST_BUY_PRICE[symbol] = price
            PAPER_T_LAYERS[symbol] = PAPER_T_LAYERS.get(symbol, 0) + 1
            PAPER_POSITION_COST[symbol] = PAPER_POSITION_COST.get(symbol, 0.0) + quote_amount
        elif side == "sell" and fill.filled_qty > 0:
            total_cost = PAPER_POSITION_COST.get(symbol, 0.0)
            total_qty = PAPER_ACCOUNT.positions.get(symbol, 0.0) + fill.filled_qty
            avg_cost_per_unit = total_cost / total_qty if total_qty > 0 else price
            pnl = fill.filled_qty * price - fill.filled_qty * avg_cost_per_unit - fill.fee
            if pnl < 0:
                PAPER_DAILY_LOSS += abs(pnl)
            # Reset position tracking if fully closed
            remaining_qty = PAPER_ACCOUNT.positions.get(symbol, 0.0)
            if remaining_qty <= 0:
                PAPER_LAST_BUY_PRICE.pop(symbol, None)
                PAPER_T_LAYERS.pop(symbol, None)
                PAPER_POSITION_COST.pop(symbol, None)
            else:
                sold_cost = fill.filled_qty * avg_cost_per_unit
                PAPER_POSITION_COST[symbol] = max(0.0, total_cost - sold_cost)

    order = {
        "orderId": f"po-{len(PAPER_ORDER_LOG) + 1:04d}",
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "symbol": symbol,
        "side": side,
        "sourceSignal": intent.source_signal,
        "orderType": order_type,
        "quoteAmount": quote_amount,
        "price": price,
        "filledQty": fill.filled_qty,
        "fee": fill.fee,
        "status": fill.status,
        "reason": fill.reason,
        "paperOnly": True,
    }
    PAPER_ORDER_LOG.append(order)
    return 200, {"order": order, "account": build_paper_orders_response()[1]["account"]}


def build_paper_reset_response(raw_body: bytes = b""):
    try:
        payload = json.loads(raw_body.decode("utf-8")) if raw_body else {}
        cash = float(payload.get("cash", 10_000.0))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        return 400, {"error": str(exc)}
    if cash < 0:
        return 400, {"error": "cash must be non-negative"}

    global PAPER_DAILY_LOSS
    PAPER_ACCOUNT.cash = cash
    PAPER_ACCOUNT.positions.clear()
    PAPER_ORDER_LOG.clear()
    PAPER_LAST_BUY_PRICE.clear()
    PAPER_T_LAYERS.clear()
    PAPER_POSITION_COST.clear()
    PAPER_DAILY_LOSS = 0.0
    return build_paper_orders_response()


def build_backtest_results_response():
    stored = BACKTEST_STORE.list_recent(limit=20)
    if stored:
        BACKTEST_RESULTS[:] = stored
    return 200, {"results": BACKTEST_RESULTS[-20:]}


def build_backtest_result_detail_response(result_id: str):
    result = BACKTEST_STORE.get(result_id)
    if result is None:
        for row in BACKTEST_RESULTS:
            if row.get("resultId") == result_id:
                result = row
                break
    if result is None:
        return 404, {"error": f"backtest result not found: {result_id}"}
    return 200, {"result": result}


def append_backtest_result(result: dict) -> dict:
    stored = BACKTEST_STORE.append(result)
    BACKTEST_RESULTS.append(stored)
    return stored


def build_backtest_response(raw_body: bytes):
    try:
        payload = json.loads(raw_body.decode("utf-8"))
        symbols = [str(symbol).upper() for symbol in payload.get("symbols", [])]
        slippage = float(payload.get("slippageBps", 30))
        spread = float(payload.get("spreadBps", 20))
        source = DataSourceFactory.normalize_source(payload.get("source", "Synthetic"))
        daily_path = payload.get("dailyPath")
        intraday_path = payload.get("intradayPath")
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        return 400, {"error": str(exc)}

    all_profiles = [
        AssetProfile("SPCX", "商业航天/星链", 30, 20),
        AssetProfile("TSLA", "科技巨头/高流动性", 12, 12),
        AssetProfile("NVDA", "AI芯片/高流动性", 8, 10),
        AssetProfile("MU", "存储周期", 24, 28),
        AssetProfile("CPOX", "CPO光通信", 35, 40),
    ]
    selected = [profile for profile in all_profiles if not symbols or profile.symbol in symbols]
    if symbols and not selected:
        selected = [AssetProfile(symbol, "自定义标的", 30, 20) for symbol in symbols]
    data_source = DataSourceFactory.get_source(source)
    asset_results = []
    per_asset_quote = 15_000 / len(selected) if selected else 0.0
    for profile in selected:
        try:
            daily, intraday = data_source.get_default_candles(
                symbol=profile.symbol,
                daily_path=daily_path,
                intraday_path=intraday_path,
            )
        except Exception as exc:
            return 502, {"error": str(exc), "source": source, "symbol": profile.symbol}
        result = run_backtest(
            daily,
            intraday,
            BacktestConfig(
                starting_quote=per_asset_quote,
                core_allocation_pct=0.70,
                slippage_bps=slippage,
                synthetic_spread_bps=spread,
            ),
            StrategyConfig(),
        )
        metrics = calculate_metrics(result.starting_quote * 0.30, result.equity_curve, result.trade_pnls)
        asset_results.append({"profile": profile, "result": result, "metrics": metrics})

    aggregate_start = sum(row["result"].starting_quote * 0.30 for row in asset_results)
    aggregate_curve = [aggregate_start]
    aggregate_pnls = []
    max_global_t_exposure = 0.0
    aggregate_core_returns = []
    aggregate_alpha = []
    for row in asset_results:
        result = row["result"]
        aggregate_curve.append(aggregate_curve[-1] + (result.ending_equity - result.starting_quote * 0.30))
        aggregate_pnls.extend(result.trade_pnls)
        max_global_t_exposure += result.max_t_position_quote
        aggregate_core_returns.append(result.core_only_return_pct)
        aggregate_alpha.append(result.strategy_vs_core_only_alpha)
    aggregate_metrics = calculate_metrics(aggregate_start, aggregate_curve, aggregate_pnls)
    summary = {
        "totalReturnPct": aggregate_metrics.total_return_pct,
        "maxDrawdownPct": aggregate_metrics.max_drawdown_pct,
        "winRate": aggregate_metrics.win_rate,
        "profitFactor": aggregate_metrics.profit_factor,
        "globalTExposureCap": min(max_global_t_exposure, 15_000 * 0.30),
        "coreOnlyReturnPct": sum(aggregate_core_returns) / len(aggregate_core_returns) if aggregate_core_returns else 0.0,
        "strategyVsCoreOnlyAlpha": sum(aggregate_alpha) / len(aggregate_alpha) if aggregate_alpha else 0.0,
    }
    assets = [
        {
            "symbol": row["profile"].symbol,
            "theme": row["profile"].theme,
            "returnPct": row["metrics"].total_return_pct,
            "maxDrawdownPct": row["metrics"].max_drawdown_pct,
            "winRate": row["metrics"].win_rate,
            "profitFactor": row["metrics"].profit_factor,
            "trades": len(row["result"].trades),
            "feesPaid": row["result"].fees_paid,
        }
        for row in asset_results
    ]
    asset_details = [
        serialize_asset_result_detail(
            row["profile"].symbol,
            row["profile"].theme,
            row["result"],
            metrics={
                "returnPct": row["metrics"].total_return_pct,
                "maxDrawdownPct": row["metrics"].max_drawdown_pct,
                "winRate": row["metrics"].win_rate,
                "profitFactor": row["metrics"].profit_factor,
            },
        )
        for row in asset_results
    ]
    created_at = datetime.now(timezone.utc).isoformat()
    result_id = f"bt-{len(BACKTEST_RESULTS) + 1:04d}"
    primary_result = asset_results[0]["result"] if asset_results else None
    if primary_result is not None:
        persisted = serialize_backtest_result(
            primary_result,
            result_id=result_id,
            created_at=created_at,
            symbols=[row["profile"].symbol for row in asset_results],
            summary=summary,
            assets=assets,
            source=source,
        )
        persisted["assetDetails"] = asset_details
    else:
        persisted = {
            "resultId": result_id,
            "createdAt": created_at,
            "engineVersion": BACKTEST_ENGINE_VERSION,
            "source": source,
            "symbols": symbols,
            "configSnapshot": {"slippageBps": slippage, "spreadBps": spread},
            "executionAssumptions": {"paperOnly": True},
            "summary": summary,
            "assets": assets,
            "equityCurve": [],
            "trades": [],
            "tradePnls": [],
            "orderIntents": [],
            "riskEvents": [],
            "assetDetails": [],
        }
    append_backtest_result(persisted)
    return 200, {
        "backtest": {
            "resultId": persisted["resultId"],
            "source": persisted["source"],
            "configSnapshot": persisted["configSnapshot"],
            "executionAssumptions": persisted["executionAssumptions"],
            "summary": summary,
            "assets": assets,
        }
    }


def allocation_rows_to_dict(config):
    return [
        {
            "symbol": row.symbol,
            "totalPct": row.total_pct,
            "tPct": row.t_pct,
            "symbolBudget": row.symbol_budget,
            "coreBudget": row.core_budget,
            "tBudget": row.t_budget,
        }
        for row in build_allocations(config)
    ]


def build_config_response(raw_body: bytes):
    global ALLOCATION_CONFIG
    try:
        payload = json.loads(raw_body.decode("utf-8"))
        config = allocation_config_from_dict(payload)
        rows = build_allocations(config)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return 400, {"error": str(exc)}

    ALLOCATION_CONFIG = config
    return 200, {
        "allocation": allocation_config_to_dict(config),
        "allocationRows": [
            {
                "symbol": row.symbol,
                "totalPct": row.total_pct,
                "tPct": row.t_pct,
                "symbolBudget": row.symbol_budget,
                "coreBudget": row.core_budget,
                "tBudget": row.t_budget,
            }
            for row in rows
        ],
    }


class DashboardHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB_ROOT), **kwargs)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/state":
            payload = json.dumps(build_dashboard_state(allocation_config=ALLOCATION_CONFIG), ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if parsed.path == "/api/state/static":
            payload = json.dumps(build_static_state(), ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if parsed.path == "/api/state/live":
            payload = json.dumps(build_live_state(), ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if parsed.path == "/api/klines":
            params = parse_qs(parsed.query)
            status, body = build_klines_response(
                params.get("symbol", ["NVDA"])[0],
                params.get("resolution", ["1m"])[0],
                params.get("source", ["Synthetic"])[0],
                params.get("dailyPath", [None])[0],
                params.get("intradayPath", [None])[0],
            )
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
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
        if parsed.path.startswith("/api/backtest/results/"):
            result_id = parsed.path.rsplit("/", 1)[-1]
            status, body = build_backtest_result_detail_response(result_id)
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        return super().do_GET()

    def do_POST(self):
        if self.path == "/api/allocation":
            length = int(self.headers.get("Content-Length", "0"))
            status, body = build_config_response(self.rfile.read(length))
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if self.path == "/api/backtest/run":
            length = int(self.headers.get("Content-Length", "0"))
            status, body = build_backtest_response(self.rfile.read(length))
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if self.path == "/api/paper/orders":
            length = int(self.headers.get("Content-Length", "0"))
            status, body = build_paper_order_response(self.rfile.read(length))
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if self.path == "/api/paper/reset":
            length = int(self.headers.get("Content-Length", "0"))
            status, body = build_paper_reset_response(self.rfile.read(length))
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        self.send_error(404)


def run_server(host: str = "127.0.0.1", port: int = 8765) -> None:
    server = ThreadingHTTPServer((host, port), DashboardHandler)
    print(f"Trade dashboard running at http://{host}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    run_server()

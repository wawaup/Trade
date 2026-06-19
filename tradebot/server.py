import json
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from tradebot.allocation import allocation_config_from_dict, allocation_config_to_dict, build_allocations, default_allocation_config
from tradebot.dashboard import build_dashboard_state
from tradebot.data import generate_synthetic_spcx
from tradebot.data_sources import DataSourceFactory
from tradebot.execution import OrderIntent, PaperAccount, PaperExecutionAdapter
from tradebot.metrics import calculate_metrics
from tradebot.research import AssetProfile, run_multi_asset_research


ROOT = Path(__file__).resolve().parents[1]
WEB_ROOT = ROOT / "web"
ALLOCATION_CONFIG = default_allocation_config()
PAPER_ACCOUNT = PaperAccount(cash=10_000.0)
PAPER_ORDER_LOG = []
BACKTEST_RESULTS = []
BACKTEST_ENGINE_VERSION = "trade-research-v1"


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


def build_klines_response(symbol: str, resolution: str):
    _, intraday = generate_synthetic_spcx(seed=sum(ord(ch) for ch in symbol) + len(resolution))
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
    return 200, {"symbol": symbol, "resolution": resolution, "candles": candles, "vwap": vwap}


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
    fill = PaperExecutionAdapter(PAPER_ACCOUNT).execute(intent, price=price, fee=max(0.0, quote_amount * 0.001))
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


def build_backtest_results_response():
    return 200, {"results": BACKTEST_RESULTS[-20:]}


def append_backtest_result(summary: dict) -> dict:
    result = {
        "resultId": f"bt-{len(BACKTEST_RESULTS) + 1:04d}",
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "summary": summary,
        "engineVersion": BACKTEST_ENGINE_VERSION,
    }
    BACKTEST_RESULTS.append(result)
    return result


def build_backtest_response(raw_body: bytes):
    try:
        payload = json.loads(raw_body.decode("utf-8"))
        symbols = [str(symbol).upper() for symbol in payload.get("symbols", [])]
        slippage = float(payload.get("slippageBps", 30))
        spread = float(payload.get("spreadBps", 20))
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
    adjusted = [AssetProfile(profile.symbol, profile.theme, slippage, spread) for profile in selected]
    multi = run_multi_asset_research(adjusted, starting_quote=15_000, global_t_max_exposure_pct=0.30)
    aggregate_start = sum(row.result.starting_quote * 0.30 for row in multi.asset_results)
    aggregate_curve = [aggregate_start]
    aggregate_pnls = []
    for row in multi.asset_results:
        aggregate_curve.append(aggregate_curve[-1] + (row.result.ending_equity - row.result.starting_quote * 0.30))
        aggregate_pnls.extend(row.result.trade_pnls)
    aggregate_metrics = calculate_metrics(aggregate_start, aggregate_curve, aggregate_pnls)
    summary = {
        "totalReturnPct": aggregate_metrics.total_return_pct,
        "maxDrawdownPct": aggregate_metrics.max_drawdown_pct,
        "winRate": aggregate_metrics.win_rate,
        "profitFactor": aggregate_metrics.profit_factor,
        "globalTExposureCap": multi.max_global_t_exposure,
    }
    append_backtest_result(summary)
    return 200, {
        "backtest": {
            "summary": summary,
            "assets": [
                {
                    "symbol": row.symbol,
                    "theme": row.theme,
                    "returnPct": row.metrics.total_return_pct,
                    "maxDrawdownPct": row.metrics.max_drawdown_pct,
                    "winRate": row.metrics.win_rate,
                    "profitFactor": row.metrics.profit_factor,
                    "trades": len(row.result.trades),
                    "feesPaid": row.result.fees_paid,
                }
                for row in multi.asset_results
            ]
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
        self.send_error(404)


def run_server(host: str = "127.0.0.1", port: int = 8765) -> None:
    server = ThreadingHTTPServer((host, port), DashboardHandler)
    print(f"Trade dashboard running at http://{host}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    run_server()

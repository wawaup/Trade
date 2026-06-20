import json
import os
import secrets
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse

from tradebot.allocation import allocation_config_from_dict, allocation_config_to_dict, build_allocations, default_allocation_config
from tradebot.backtest import BacktestConfig, WalkForwardResult, run_backtest, run_walk_forward
from tradebot.dashboard import build_dashboard_state
from tradebot.data import generate_synthetic_spcx
from tradebot.data_sources import DataSourceFactory
from tradebot.execution import OrderIntent, PaperAccount, PaperExecutionAdapter, RiskLimits
from tradebot.metrics import calculate_metrics
from tradebot.research import AssetProfile
from tradebot.serialization import serialize_asset_result_detail, serialize_backtest_result, serialize_equity_curve
from tradebot.storage import BacktestResultStore
from tradebot.strategy import StrategyConfig


ROOT = Path(__file__).resolve().parents[1]
WEB_ROOT = ROOT / "web"
ENV_PATH = ROOT / ".env"
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
    market_data_max_age_sec=30.0,
    daily_loss_limit_quote=500.0,
)

# Per-symbol T-position tracking for grid spacing and layer checks.
PAPER_LAST_BUY_PRICE: dict[str, float] = {}
PAPER_T_LAYERS: dict[str, int] = {}
PAPER_POSITION_COST: dict[str, float] = {}  # total cost basis per symbol
PAPER_DAILY_LOSS: float = 0.0               # accumulated realized loss today (UTC day)


def load_env_file(path: Path = ENV_PATH) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or key in os.environ:
            continue
        os.environ[key] = value.strip().strip('"').strip("'")


def auth_settings() -> dict:
    return {
        "username": os.environ.get("TRADE_ADMIN_USERNAME", "admin"),
        "password": os.environ.get("TRADE_ADMIN_PASSWORD", ""),
        "token": os.environ.get("TRADE_API_TOKEN", ""),
    }


def auth_is_configured() -> bool:
    settings = auth_settings()
    return bool(settings["password"] and settings["token"])


def build_auth_login_response(raw_body: bytes):
    if not auth_is_configured():
        return 503, {"error": "admin auth is not configured"}
    try:
        payload = json.loads(raw_body.decode("utf-8"))
        username = str(payload.get("username", ""))
        password = str(payload.get("password", ""))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        return 400, {"error": str(exc)}

    settings = auth_settings()
    if (
        secrets.compare_digest(username, settings["username"])
        and secrets.compare_digest(password, settings["password"])
    ):
        return 200, {"token": settings["token"], "username": settings["username"]}
    return 401, {"error": "invalid username or password"}


def authorization_token_valid(header_value: str) -> bool:
    if not auth_is_configured():
        return False
    prefix = "Bearer "
    if not str(header_value or "").startswith(prefix):
        return False
    token = str(header_value)[len(prefix):].strip()
    return secrets.compare_digest(token, auth_settings()["token"])


def api_request_requires_auth(method: str, path: str) -> bool:
    parsed_path = urlparse(path).path
    if parsed_path in {"/api/auth/login", "/api/health"}:
        return False
    if not parsed_path.startswith("/api/"):
        return False
    return True


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
    start_time_s: Optional[int] = None,
    end_time_s: Optional[int] = None,
):
    try:
        normalized_source = DataSourceFactory.normalize_source(source)
        daily_bars, intraday_bars = DataSourceFactory.get_source(normalized_source).get_default_candles(
            symbol=symbol,
            resolution=resolution,
            daily_path=daily_path,
            intraday_path=intraday_path,
        )
    except ValueError as exc:
        return 400, {"error": str(exc)}
    except Exception as exc:
        return 502, {"error": str(exc), "source": source}

    # 1D resolution → return daily bars (up to 60, ~3 months)
    use_daily = resolution.lower() in ("1d", "1day", "daily")
    bars = daily_bars[-60:] if use_daily else intraday_bars

    if start_time_s is not None or end_time_s is not None:
        start_ms = (start_time_s * 1000) if start_time_s is not None else 0
        end_ms = (end_time_s * 1000) if end_time_s is not None else float("inf")
        bars = [c for c in bars if start_ms <= c.open_time <= end_ms]
    elif not use_daily:
        bars = bars[-500:]   # show up to 500 bars by default (≈3 weeks @15m, ≈21 days @1h)

    candles = []
    vwap = []
    cumulative_quote = 0.0
    cumulative_volume = 0.0
    for candle in bars:
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
    if quote_amount <= 0:
        return 400, {"error": "quoteAmount must be positive"}

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
        spread_pct=payload.get("spreadPct"),
        market_data_age_sec=payload.get("marketDataAgeSec"),
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
                remaining_cost = max(0.0, total_cost - sold_cost)
                PAPER_POSITION_COST[symbol] = remaining_cost
                current_layers = PAPER_T_LAYERS.get(symbol, 0)
                sold_quote = fill.filled_qty * price
                released_layers = int(sold_quote // max(cfg.min_order_quote, 1.0))
                if released_layers > 0:
                    remaining_layers = max(0, current_layers - released_layers)
                    if remaining_layers > 0:
                        PAPER_T_LAYERS[symbol] = min(remaining_layers, cfg.max_layers)
                    else:
                        PAPER_T_LAYERS.pop(symbol, None)
                        PAPER_LAST_BUY_PRICE.pop(symbol, None)

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


def build_walk_forward_response(raw_body: bytes):
    try:
        payload = json.loads(raw_body.decode("utf-8"))
        symbol = str(payload.get("symbol", "SPCXBUSDT")).upper()
        start_date = str(payload.get("startDate", "2025-01-01"))
        end_date = str(payload.get("endDate", ""))
        in_sample_bars = int(payload.get("inSampleBars", 40))
        oos_bars = int(payload.get("oosBars", 20))
        step_bars = int(payload.get("stepBars", 10))
        slippage = float(payload.get("slippageBps", 30))
        spread = float(payload.get("spreadBps", 20))
        per_symbol_quote = float(payload.get("perSymbolQuote", 600.0))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        return 400, {"error": str(exc)}

    status, validation = validate_walk_forward_window(in_sample_bars, oos_bars, step_bars)
    if status != 200:
        return status, validation

    from datetime import datetime, timezone as tz
    try:
        start_ms = int(datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=tz.utc).timestamp() * 1000)
        if end_date:
            end_ms = int(datetime.strptime(end_date, "%Y-%m-%d").replace(tzinfo=tz.utc).timestamp() * 1000)
        else:
            end_ms = int(datetime.now(tz.utc).timestamp() * 1000)
    except ValueError as exc:
        return 400, {"error": f"invalid date: {exc}"}

    from tradebot.data_sources import BinanceHistoricalDataSource
    try:
        src = BinanceHistoricalDataSource()
        daily = src.get_daily_range(symbol, start_ms, end_ms)
    except Exception as exc:
        return 502, {"error": str(exc), "symbol": symbol}

    if len(daily) < in_sample_bars + oos_bars:
        return 400, {"error": f"not enough daily bars: got {len(daily)}, need {in_sample_bars + oos_bars}"}

    bt_config = BacktestConfig(
        starting_quote=per_symbol_quote,
        core_allocation_pct=0.70,
        slippage_bps=slippage,
        synthetic_spread_bps=spread,
    )
    wf = run_walk_forward(daily, bt_config, StrategyConfig(), in_sample_bars, oos_bars, step_bars, symbol=symbol)

    folds_json = [
        {
            "foldIndex": f.fold_index,
            "inSampleStart": f.in_sample_start,
            "inSampleEnd": f.in_sample_end,
            "oosStart": f.oos_start,
            "oosEnd": f.oos_end,
            "oosReturnPct": f.oos_return_pct,
            "oosMaxDrawdownPct": f.oos_max_drawdown_pct,
            "oosWinRate": f.oos_win_rate,
        }
        for f in wf.folds
    ]
    return 200, {
        "walkForward": {
            "symbol": wf.symbol,
            "totalFolds": wf.total_folds,
            "meanOosReturnPct": wf.mean_oos_return_pct,
            "medianOosReturnPct": wf.median_oos_return_pct,
            "positiveFoldRate": wf.positive_fold_rate,
            "meanOosMaxDrawdownPct": wf.mean_oos_max_drawdown_pct,
            "folds": folds_json,
            "params": {
                "inSampleBars": in_sample_bars,
                "oosBars": oos_bars,
                "stepBars": step_bars,
                "startDate": start_date,
                "endDate": end_date,
            },
        }
    }


def validate_walk_forward_window(in_sample_bars: int, oos_bars: int, step_bars: int):
    if in_sample_bars <= 0 or oos_bars <= 0 or step_bars <= 0:
        return 400, {"error": "walk-forward window parameters must be positive integers"}
    if in_sample_bars > 2_000 or oos_bars > 2_000 or step_bars > 2_000:
        return 400, {"error": "walk-forward window parameters exceed the 2000 bar limit"}
    return 200, {}


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
    total_quote = float(payload.get("totalQuote", 0) or 0)
    per_symbol_quote = float(payload.get("perSymbolQuote", 0) or 0)
    idle_buffer_pct = float(payload.get("idleBufferPct", 0.0) or 0.0)
    if per_symbol_quote > 0:
        per_asset_quote = per_symbol_quote
    elif total_quote > 0:
        per_asset_quote = total_quote * (1.0 - idle_buffer_pct) / max(len(selected), 1)
    else:
        per_asset_quote = 15_000 / max(len(selected), 1)
    data_source = DataSourceFactory.get_source(source)
    asset_results = []
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
    primary_equity_curve = (
        serialize_equity_curve(primary_result.equity_curve, primary_result.equity_curve_ts)
        if primary_result is not None else []
    )
    primary_trades = (
        [{"side": t.side, "openTime": t.open_time, "price": t.price,
          "qty": t.qty, "quote": t.quote, "fee": t.fee, "reason": t.reason}
         for t in primary_result.trades]
        if primary_result is not None else []
    )
    return 200, {
        "backtest": {
            "resultId": persisted["resultId"],
            "source": persisted["source"],
            "configSnapshot": persisted["configSnapshot"],
            "executionAssumptions": persisted["executionAssumptions"],
            "summary": summary,
            "assets": assets,
            "primaryEquityCurve": primary_equity_curve,
            "primaryTrades": primary_trades,
            "assetDetails": asset_details,
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

    def _write_json(self, status: int, body: dict) -> None:
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _authorized_or_respond(self) -> bool:
        if not api_request_requires_auth(self.command, self.path):
            return True
        if authorization_token_valid(self.headers.get("Authorization", "")):
            return True
        status = 401 if auth_is_configured() else 503
        message = "unauthorized" if auth_is_configured() else "admin auth is not configured"
        self._write_json(status, {"error": message})
        return False

    def do_GET(self):
        if not self._authorized_or_respond():
            return
        parsed = urlparse(self.path)
        if parsed.path == "/api/health":
            self._write_json(200, {"ok": True})
            return
        if parsed.path == "/api/state":
            self._write_json(200, build_dashboard_state(allocation_config=ALLOCATION_CONFIG))
            return
        if parsed.path == "/api/state/static":
            self._write_json(200, build_static_state())
            return
        if parsed.path == "/api/state/live":
            self._write_json(200, build_live_state())
            return
        if parsed.path == "/api/klines":
            params = parse_qs(parsed.query)
            raw_start = params.get("startTime", [None])[0]
            raw_end = params.get("endTime", [None])[0]
            status, body = build_klines_response(
                params.get("symbol", ["NVDA"])[0],
                params.get("resolution", ["1m"])[0],
                params.get("source", ["Synthetic"])[0],
                params.get("dailyPath", [None])[0],
                params.get("intradayPath", [None])[0],
                start_time_s=int(raw_start) if raw_start else None,
                end_time_s=int(raw_end) if raw_end else None,
            )
            self._write_json(status, body)
            return
        if parsed.path == "/api/data-sources":
            status, body = build_data_sources_response()
            self._write_json(status, body)
            return
        if parsed.path == "/api/paper/orders":
            status, body = build_paper_orders_response()
            self._write_json(status, body)
            return
        if parsed.path == "/api/backtest/results":
            status, body = build_backtest_results_response()
            self._write_json(status, body)
            return
        if parsed.path.startswith("/api/backtest/results/"):
            result_id = parsed.path.rsplit("/", 1)[-1]
            status, body = build_backtest_result_detail_response(result_id)
            self._write_json(status, body)
            return
        return super().do_GET()

    def do_POST(self):
        if not self._authorized_or_respond():
            return
        if self.path == "/api/auth/login":
            length = int(self.headers.get("Content-Length", "0"))
            status, body = build_auth_login_response(self.rfile.read(length))
            self._write_json(status, body)
            return
        if self.path == "/api/allocation":
            length = int(self.headers.get("Content-Length", "0"))
            status, body = build_config_response(self.rfile.read(length))
            self._write_json(status, body)
            return
        if self.path == "/api/backtest/run":
            length = int(self.headers.get("Content-Length", "0"))
            status, body = build_backtest_response(self.rfile.read(length))
            self._write_json(status, body)
            return
        if self.path == "/api/paper/orders":
            length = int(self.headers.get("Content-Length", "0"))
            status, body = build_paper_order_response(self.rfile.read(length))
            self._write_json(status, body)
            return
        if self.path == "/api/paper/reset":
            length = int(self.headers.get("Content-Length", "0"))
            status, body = build_paper_reset_response(self.rfile.read(length))
            self._write_json(status, body)
            return
        if self.path == "/api/backtest/walkforward":
            length = int(self.headers.get("Content-Length", "0"))
            status, body = build_walk_forward_response(self.rfile.read(length))
            self._write_json(status, body)
            return
        self.send_error(404)


def run_server(host: str = "127.0.0.1", port: int = 8765) -> None:
    load_env_file()
    server = ThreadingHTTPServer((host, port), DashboardHandler)
    print(f"Trade dashboard running at http://{host}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    run_server()

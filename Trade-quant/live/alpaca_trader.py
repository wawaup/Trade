"""
Alpaca Paper/Live 量化策略执行脚本

策略：RS_Beta + MFI_14 + BIAS_20 + HV_ratio（W_BIAS20 权重矩阵）
      QQQ MA50 状态机（牛/熊/震荡三态）
      过滤：Combo_Score > 1.0 AND Vol_Shock > 1.2x
      仓位：Top-5 等权，每 5 个交易日调仓

运行方式（美东盘后 16:30 运行，次日开盘生效）：
  python alpaca_trader.py              # 正常运行，5 日间隔检查
  python alpaca_trader.py --force      # 强制立刻调仓（忽略 5 日间隔）
  python alpaca_trader.py --dry-run    # 仅预览目标持仓，不实际下单

输出：
  trader.log   每次运行的完整日志
  state.json   持久化状态（高水位、上次调仓日、Kill Switch）
"""

import os
import sys
import json
import logging
import argparse
import csv
import smtplib
import ssl
import time
import traceback
import email.utils
from pathlib import Path
from datetime import date, datetime
from email.message import EmailMessage
from typing import Optional
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import yfinance as yf
from dotenv import load_dotenv

from alpaca.data.enums import Adjustment, DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestQuoteRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetOrdersRequest, LimitOrderRequest, MarketOrderRequest, StopOrderRequest
from alpaca.trading.enums import OrderSide, QueryOrderStatus, TimeInForce

# ── 路径 & 模块导入 ────────────────────────────────────────────────────────────
LIVE_DIR     = Path(__file__).parent
RESEARCH_DIR = LIVE_DIR.parent / "research"
DATA_DIR     = LIVE_DIR.parent / "data"
UNIVERSE_PATH = DATA_DIR / "universe.json"
STATE_FILE        = LIVE_DIR / "state.json"
LOG_FILE          = LIVE_DIR / "trader.log"
AUDIT_DIR         = LIVE_DIR / "audit"
HALT_PENDING_FILE = LIVE_DIR / "halt_pending.json"

sys.path.insert(0, str(RESEARCH_DIR))
try:
    from factor_scanner import load_universe, compute_factors, build_liquidity_mask
    from factor_combo_backtest import zscore_factors, CORE_FACTORS, REGIME_WEIGHTS
    from build_universe import build_universe as build_universe_dict, save_universe as save_universe_dict
except ImportError as e:
    print(f"❌ 导入 research 模块失败：{e}")
    print("   请在 Trade-quant/live/ 目录内运行本脚本")
    sys.exit(1)

# ── 配置 ──────────────────────────────────────────────────────────────────────
load_dotenv(LIVE_DIR / ".env")
API_KEY    = os.getenv("ALPACA_API_KEY", "")
SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "")
PAPER      = os.getenv("ALPACA_PAPER", "true").lower() != "false"

REBALANCE_DAYS = 5      # 每 N 交易日调仓一次（与回测一致）
TOP_N          = 5      # 持仓上限（Top-5 MaxDD 最优）
MIN_SCORE      = 1.0    # Combo Score 入场门槛
VOL_MIN        = 1.2    # Vol_Shock 放量倍数
KILL_DD        = -0.30  # Kill Switch 触发阈值（从账户高水位回撤 30%）
DATA_DAYS      = 350    # 拉取天数（RS_Beta 需 ≥ 60 天 Beta 稳定期，留 350 天余量）
MIN_DATA_ROWS  = 150    # 单票最少有效日线数量
DATA_SOURCE    = os.getenv("MARKET_DATA_SOURCE", "alpaca").lower()
ALPACA_FEED    = os.getenv("ALPACA_DATA_FEED", "sip").lower()
DATA_BATCH_SIZE = 50
MIN_VALID_SYMBOLS = int(os.getenv("MIN_VALID_SYMBOLS", "120"))
ORDER_TIF      = os.getenv("ORDER_TIF", "opg").lower()
ALLOW_DUPLICATE_SIGNAL = os.getenv("ALLOW_DUPLICATE_SIGNAL", "false").lower() == "true"
ENABLE_STOP_ORDERS = os.getenv("ENABLE_STOP_ORDERS", "true").lower() == "true"
STOP_LOSS_PCT  = float(os.getenv("STOP_LOSS_PCT", "0.25"))
EMAIL_ENABLED  = os.getenv("EMAIL_ENABLED", "false").lower() == "true"
EMAIL_SMTP_HOST = os.getenv("EMAIL_SMTP_HOST", "")
EMAIL_SMTP_PORT = int(os.getenv("EMAIL_SMTP_PORT", "587"))
EMAIL_USERNAME = os.getenv("EMAIL_USERNAME", "")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD", "")
EMAIL_FROM     = os.getenv("EMAIL_FROM", EMAIL_USERNAME)
EMAIL_TO       = os.getenv("EMAIL_TO", "")

EARNINGS_BLACKOUT_DAYS    = int(os.getenv("EARNINGS_BLACKOUT_DAYS", "2"))
KILL_SWITCH_LIMIT_SLIPPAGE = float(os.getenv("KILL_SWITCH_LIMIT_SLIPPAGE", "0.03"))
HALT_RETRY_UNTIL_HOUR_ET  = int(os.getenv("HALT_RETRY_UNTIL_HOUR_ET", "12"))

# 仓位规模模拟：留空/0 = 用 Alpaca 账户真实净值计算仓位；
# 设置后仅用此金额代替账户净值计算买入数量，账户净值/回撤/Kill Switch 判断仍基于真实账户（百分比口径不受影响）
SIM_CAPITAL_USD = float(os.getenv("SIM_CAPITAL_USD", "0"))

# 已确认不可再直接从行情源获取的旧代码。IIVI 已并入/更名为 COHR，池子中已保留 COHR。
STALE_SYMBOLS = {"IIVI"}

# ── 日志 ──────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("trader")


# ── 状态持久化 ─────────────────────────────────────────────────────────────────
def _load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"high_watermark": None, "last_rebalance": None, "kill_switch": False}


def _save_state(s: dict):
    STATE_FILE.write_text(json.dumps(s, default=str, indent=2), encoding="utf-8")


def _slug_date(value: str) -> str:
    return value.replace("-", "")


def _order_time_in_force() -> TimeInForce:
    mapping = {
        "opg": TimeInForce.OPG,
        "day": TimeInForce.DAY,
    }
    return mapping.get(ORDER_TIF, TimeInForce.OPG)


def build_market_order(symbol: str, qty: int, side: OrderSide, signal_date: str, action: str) -> MarketOrderRequest:
    client_order_id = f"tq-{_slug_date(signal_date)}-{action.lower()}-{symbol.lower()}"
    return MarketOrderRequest(
        symbol=symbol,
        qty=qty,
        side=side,
        time_in_force=_order_time_in_force(),
        client_order_id=client_order_id,
    )


def build_stop_order(symbol: str, qty: int, reference_price: float, signal_date: str) -> StopOrderRequest:
    stop_price = round(reference_price * (1 - STOP_LOSS_PCT), 2)
    client_order_id = f"tq-stop-{symbol.lower()}"
    return StopOrderRequest(
        symbol=symbol,
        qty=qty,
        side=OrderSide.SELL,
        time_in_force=TimeInForce.GTC,
        stop_price=stop_price,
        client_order_id=client_order_id,
    )


def ensure_live_confirmation():
    if not PAPER and os.getenv("LIVE_CONFIRM", "") != "YES":
        raise RuntimeError("实盘模式需要设置 LIVE_CONFIRM=YES，防止误切真实交易。")


def is_duplicate_signal(state: dict, signal_date: str, dry_run: bool, allow_duplicate: bool) -> bool:
    return (not dry_run) and (not allow_duplicate) and state.get("last_order_signal_date") == signal_date


def validate_panel(close: pd.DataFrame):
    missing = [sym for sym in ("QQQ", "SPY") if sym not in close.columns]
    if missing:
        raise RuntimeError(f"关键基准缺失: {missing}")
    if close.empty:
        raise RuntimeError("行情面板为空，拒绝继续执行")
    valid_stocks = [c for c in close.columns if c not in ("QQQ", "SPY")]
    if len(valid_stocks) < MIN_VALID_SYMBOLS:
        raise RuntimeError(f"有效股票数过低: {len(valid_stocks)} < {MIN_VALID_SYMBOLS}")
    latest = close.index[-1].date()
    today = date.today()
    if latest > today:
        raise RuntimeError(f"最新行情日期异常: {latest} > {today}")


def load_trading_universe() -> tuple[list[str], list[str]]:
    if not UNIVERSE_PATH.exists():
        log.warning(f"未找到股票池文件，自动构建: {UNIVERSE_PATH}")
        universe = build_universe_dict()
        save_universe_dict(universe)
    else:
        universe = json.loads(UNIVERSE_PATH.read_text(encoding="utf-8"))
    return universe["symbols"], universe["benchmarks"]


class AuditWriter:
    def __init__(self, audit_dir: Path):
        self.audit_dir = audit_dir
        self.audit_dir.mkdir(parents=True, exist_ok=True)

    def _append(self, filename: str, row: dict):
        path = self.audit_dir / filename
        exists = path.exists()
        fieldnames = list(row.keys())
        with path.open("a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            if not exists:
                writer.writeheader()
            writer.writerow(row)

    def append_run(self, row: dict):
        self._append("paper_runs.csv", row)

    def append_order(self, row: dict):
        self._append("orders.csv", row)

    def append_fill(self, row: dict):
        self._append("fills.csv", row)

    def append_signal_rows(self, run_id: str, signal_date: str, scores: pd.Series, latest_prices: dict):
        ranked = scores.sort_values(ascending=False)
        if ranked.empty:
            self._append("signals.csv", {
                "run_id": run_id,
                "signal_date": signal_date,
                "rank": "",
                "symbol": "NO_CANDIDATE",
                "combo_score": "",
                "signal_close": "",
            })
            return
        for rank, (symbol, score) in enumerate(ranked.items(), start=1):
            self._append("signals.csv", {
                "run_id": run_id,
                "signal_date": signal_date,
                "rank": rank,
                "symbol": symbol,
                "combo_score": round(float(score), 6),
                "signal_close": latest_prices.get(symbol, ""),
            })


def _read_log_tail(max_lines: int = 100) -> str:
    if not LOG_FILE.exists():
        return ""
    lines = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(lines[-max_lines:])


def _audit_attachments() -> list:
    names = ["paper_runs.csv", "signals.csv", "orders.csv", "fills.csv"]
    return [AUDIT_DIR / name for name in names if (AUDIT_DIR / name).exists()]


def is_emergency_status(status: str) -> bool:
    emergency = {
        "api_connection_failed",
        "data_validation_failed",
        "error",
        "market_data_failed",
        "missing_api_key",
        "order_submit_failed",
        "service_stale",
        "kill_switch_locked",
        "kill_switch_triggered",
    }
    return status in emergency


def build_email_subject(status: str, run_id: str, paper: bool) -> str:
    mode = "Paper" if paper else "LIVE"
    labels = {
        "ok": "普通日报-运行成功",
        "skipped_rebalance_interval": "普通日报-非调仓日",
        "duplicate_signal_skipped": "普通日报-重复信号跳过",
        "api_connection_failed": "紧急报警-API连接失败",
        "market_data_failed": "紧急报警-行情数据异常",
        "data_validation_failed": "紧急报警-数据校验失败",
        "order_submit_failed": "紧急报警-下单失败",
        "service_stale": "紧急报警-服务失效",
        "missing_api_key": "紧急报警-配置缺失",
        "kill_switch_locked": "紧急报警-熔断锁定",
        "kill_switch_triggered": "紧急报警-熔断触发",
        "error": "紧急报警-脚本异常",
    }
    label = labels.get(status, "紧急报警-未知状态" if is_emergency_status(status) else "普通日报-其他状态")
    return f"Trade Quant {mode} {label} - {status} - {run_id}"


def send_email(subject: str, body: str, attachments: Optional[list] = None):
    if not EMAIL_ENABLED:
        return
    if not (EMAIL_SMTP_HOST and EMAIL_USERNAME and EMAIL_PASSWORD and EMAIL_FROM and EMAIL_TO):
        log.warning("邮件配置不完整，跳过发送")
        return
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO
    msg["Date"] = email.utils.formatdate(localtime=True)
    msg["Message-ID"] = email.utils.make_msgid(domain="quant.system")
    msg.set_content(body)
    for path in attachments or []:
        msg.add_attachment(
            path.read_bytes(),
            maintype="text",
            subtype="csv" if path.suffix == ".csv" else "plain",
            filename=path.name,
        )
    context = ssl.create_default_context()
    with smtplib.SMTP(EMAIL_SMTP_HOST, EMAIL_SMTP_PORT, timeout=30) as server:
        server.starttls(context=context)
        server.login(EMAIL_USERNAME, EMAIL_PASSWORD)
        server.send_message(msg)


def record_recent_fills(client: TradingClient, audit: AuditWriter, run_id: str, signal_date: str) -> int:
    try:
        orders = client.get_orders(GetOrdersRequest(status=QueryOrderStatus.ALL, limit=100))
    except Exception as e:
        log.warning(f"成交回报查询失败: {e}")
        return 0

    n = 0
    for order in orders:
        client_order_id = str(getattr(order, "client_order_id", "") or "")
        if not client_order_id.startswith("tq-"):
            continue
        filled_qty = float(getattr(order, "filled_qty", 0) or 0)
        if filled_qty <= 0:
            continue
        audit.append_fill({
            "run_id": run_id,
            "signal_date": signal_date,
            "symbol": getattr(order, "symbol", ""),
            "side": getattr(getattr(order, "side", ""), "value", getattr(order, "side", "")),
            "filled_qty": filled_qty,
            "filled_avg_price": getattr(order, "filled_avg_price", ""),
            "status": getattr(getattr(order, "status", ""), "value", getattr(order, "status", "")),
            "client_order_id": client_order_id,
            "submitted_at": getattr(order, "submitted_at", ""),
            "filled_at": getattr(order, "filled_at", ""),
        })
        n += 1
    return n


def ensure_stop_orders_for_positions(client: TradingClient, positions: list, signal_date: str, dry_run: bool) -> int:
    if not ENABLE_STOP_ORDERS:
        return 0
    submitted = 0
    for p in positions:
        try:
            sym = p.symbol
            qty = int(float(p.qty))
            if qty <= 0:
                continue
            ref_price = float(getattr(p, "avg_entry_price", 0) or 0)
            if ref_price <= 0:
                continue
            stop_req = build_stop_order(sym, qty, ref_price, signal_date)
            log.info(f"  STOP  {sym:8s} × {qty:4d} @ ${stop_req.stop_price:.2f}（已有持仓保护）")
            if not dry_run:
                client.submit_order(stop_req)
                submitted += 1
        except Exception as e:
            log.warning(f"  {getattr(p, 'symbol', '?')} 止损单处理失败: {e}")
    return submitted


def _now_hour_et() -> int:
    return datetime.now(ZoneInfo("America/New_York")).hour


def _is_after_hours() -> bool:
    """当前是否处于盘后交易窗口（16:00–20:00 ET）。"""
    h = _now_hour_et()
    return 16 <= h < 20


def get_upcoming_earnings(symbols: list[str], days_ahead: int = 2) -> set[str]:
    """返回在未来 days_ahead 个交易日内发布财报的股票集合（财报避雷针）。
    yfinance 不同版本的 ticker.calendar 返回值结构不一：dict / DataFrame / None。
    任何解析错误均静默跳过，原则：宁可错过一次避雷，不能因 API 异常挂断发单主流程。
    """
    if days_ahead <= 0:
        return set()
    blackout: set[str] = set()
    today = pd.Timestamp.today().normalize()
    cutoff = today + pd.offsets.BDay(days_ahead)
    for sym in [s for s in symbols if s not in ("QQQ", "SPY")]:
        try:
            cal = yf.Ticker(sym).calendar
            if cal is None:
                continue

            # yfinance ≥0.2 返回 dict；部分旧版或特殊股票返回 DataFrame
            if isinstance(cal, dict):
                raw_dates = cal.get("Earnings Date", [])
            elif hasattr(cal, "columns"):           # DataFrame
                col = next((c for c in cal.columns if "Earnings" in str(c) and "Date" in str(c)), None)
                raw_dates = cal[col].dropna().tolist() if col else []
            else:
                continue

            if not isinstance(raw_dates, (list, pd.Series)):
                raw_dates = [raw_dates]

            for ed in raw_dates:
                if ed is None:
                    continue
                try:
                    ed_ts = pd.Timestamp(ed).normalize()
                except Exception:
                    continue
                if pd.isna(ed_ts):
                    continue
                if today <= ed_ts <= cutoff:
                    blackout.add(sym)
                    log.info(f"  📅 财报避雷：{sym} 预计 {ed_ts.date()} 发布财报（{days_ahead}日内），强制回避")
                    break
        except Exception as e:
            log.warning(f"  {sym} 财报日历查询失败（跳过，默认放行）: {e}")
    return blackout


def _append_halt_pending(sym: str, qty: int, signal_date: str, run_id: str):
    """将被 LULD 熔断拒绝的订单写入重试队列文件。"""
    data: dict = {"pending_date": signal_date, "orders": []}
    if HALT_PENDING_FILE.exists():
        try:
            data = json.loads(HALT_PENDING_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    if data.get("pending_date") != signal_date:
        data = {"pending_date": signal_date, "orders": []}
    if not any(o["symbol"] == sym for o in data["orders"]):
        data["orders"].append({"symbol": sym, "qty": qty, "signal_date": signal_date, "run_id": run_id})
    HALT_PENDING_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    log.warning(f"  已写入 halt_pending.json：{sym} × {qty}")


def _kill_switch_liquidate(client: TradingClient, positions: list, dry_run: bool):
    """Kill Switch 触发时的清仓逻辑：盘后优先用限价单，否则用市价单（次日 9:30 执行）。"""
    log.critical("  取消所有挂单...")
    if not dry_run:
        try:
            client.cancel_orders()
        except Exception as e:
            log.error(f"  cancel_orders 失败: {e}")

    if _is_after_hours() and KILL_SWITCH_LIMIT_SLIPPAGE > 0:
        log.critical(
            f"  当前处于盘后（ET {_now_hour_et()}:xx），"
            f"使用盘后限价单逃生（让价 -{KILL_SWITCH_LIMIT_SLIPPAGE:.0%}）"
        )
        submitted = 0
        for p in positions:
            sym = p.symbol
            qty = int(float(getattr(p, "qty", 0) or 0))
            if qty <= 0:
                continue
            ref = float(getattr(p, "current_price", 0) or 0)
            if ref <= 0:
                ref = float(getattr(p, "avg_entry_price", 0) or 0)
            if ref <= 0:
                log.warning(f"    {sym} 无参考价格，跳过（需手动平仓）")
                continue
            limit_price = round(ref * (1 - KILL_SWITCH_LIMIT_SLIPPAGE), 2)
            req = LimitOrderRequest(
                symbol=sym,
                qty=qty,
                side=OrderSide.SELL,
                time_in_force=TimeInForce.DAY,
                limit_price=limit_price,
                extended_hours=True,
                client_order_id=f"tq-ks-{sym.lower()}",
            )
            log.critical(f"    SELL {sym} × {qty} @ ${limit_price:.2f} [盘后限价]")
            if not dry_run:
                try:
                    client.submit_order(req)
                    submitted += 1
                except Exception as e:
                    log.error(f"    ❌ {sym} 盘后限价单失败，改为 close_position: {e}")
                    try:
                        client.close_position(sym)
                    except Exception as e2:
                        log.error(f"    ❌ {sym} close_position 也失败: {e2}")
        log.critical(f"  Kill Switch 盘后限价单：已提交 {submitted}/{len(positions)} 笔")
    else:
        log.critical(
            f"  当前非盘后时段（ET {_now_hour_et()}:xx），"
            "提交市价清仓单（次日 9:30 集合竞价执行）"
        )
        if not dry_run:
            try:
                client.close_all_positions(cancel_orders=True)
            except Exception as e:
                log.error(f"  close_all_positions 失败: {e}")


def retry_halted_orders(dry_run: bool):
    """重试因 LULD 熔断被拒的买入单，每 5 分钟一次，直到 HALT_RETRY_UNTIL_HOUR_ET 时（ET）。"""
    if not HALT_PENDING_FILE.exists():
        log.info("未找到 halt_pending.json，无需重试，退出")
        return
    try:
        data = json.loads(HALT_PENDING_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        log.error(f"读取 halt_pending.json 失败: {e}")
        return
    orders = data.get("orders", [])
    if not orders:
        log.info("重试队列为空，退出")
        HALT_PENDING_FILE.unlink(missing_ok=True)
        return

    log.info(f"LULD 重试模式：发现 {len(orders)} 笔挂单 → {[o['symbol'] for o in orders]}")
    client = TradingClient(API_KEY, SECRET_KEY, paper=PAPER)
    data_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)

    remaining = list(orders)
    attempt = 0
    while remaining:
        et_hour = _now_hour_et()
        if et_hour >= HALT_RETRY_UNTIL_HOUR_ET:
            log.warning(f"  已到 {HALT_RETRY_UNTIL_HOUR_ET}:00 ET，放弃剩余 {len(remaining)} 笔重试")
            break
        attempt += 1
        log.info(f"  第 {attempt} 次重试（{len(remaining)} 笔）...")
        still_pending = []
        for o in remaining:
            sym = o["symbol"]
            qty = o["qty"]
            try:
                quote_req = StockLatestQuoteRequest(symbol_or_symbols=[sym])
                quotes = data_client.get_stock_latest_quote(quote_req)
                bid = float(quotes[sym].bid_price) if sym in quotes else 0.0
                if bid <= 0:
                    log.warning(f"    {sym} 报价为 0，跳过本轮")
                    still_pending.append(o)
                    continue
                limit_price = round(bid * 0.999, 2)  # bid - 0.1%，确保成交
                limit_req = LimitOrderRequest(
                    symbol=sym,
                    qty=qty,
                    side=OrderSide.BUY,
                    time_in_force=TimeInForce.DAY,
                    limit_price=limit_price,
                    client_order_id=f"tq-retry-{sym.lower()}",
                )
                log.info(f"    RETRY BUY {sym} × {qty} @ ${limit_price:.2f}")
                if not dry_run:
                    client.submit_order(limit_req)
                log.info(f"    ✅ {sym} 重试订单已提交")
            except Exception as e:
                err_msg = str(e).lower()
                if any(kw in err_msg for kw in ("halt", "not_tradable", "suspended", "asset_not_tradable")):
                    log.warning(f"    ⚠️ {sym} 仍停牌/熔断，5 分钟后继续重试")
                    still_pending.append(o)
                else:
                    log.error(f"    ❌ {sym} 重试失败（非熔断原因）: {e}")
        remaining = still_pending
        if remaining:
            log.info(f"  {len(remaining)} 笔仍在等待，5 分钟后重试...")
            time.sleep(300)

    if not remaining:
        log.info("  所有挂单已成功处理，清除 halt_pending.json")
        HALT_PENDING_FILE.unlink(missing_ok=True)
    else:
        log.warning(f"  {len(remaining)} 笔最终放弃（保留文件供复盘）：{[o['symbol'] for o in remaining]}")


def _money(value) -> str:
    try:
        return f"${float(value):,.2f}"
    except (TypeError, ValueError):
        return "$0.00"


def _position_line(p) -> str:
    symbol = getattr(p, "symbol", "")
    qty = getattr(p, "qty", "")
    market_value = _money(getattr(p, "market_value", 0))
    avg_entry_price = _money(getattr(p, "avg_entry_price", 0))
    unrealized_pl = _money(getattr(p, "unrealized_pl", 0))
    return (
        f"- {symbol} qty={qty} market_value={market_value} "
        f"avg_entry={avg_entry_price} unrealized_pl={unrealized_pl}"
    )


def _candidate_lines(candidates: pd.Series, latest_prices: dict, limit: int = 10) -> list:
    if candidates is None or candidates.empty:
        return ["- 今日无候选股"]
    lines = []
    for symbol, score in candidates.sort_values(ascending=False).head(limit).items():
        price = latest_prices.get(symbol, "")
        price_text = f"{float(price):.2f}" if price != "" else "N/A"
        lines.append(f"- {symbol}  score={float(score):.4f}  close={price_text}")
    return lines


def _order_plan_lines(order_plan: list) -> list:
    if not order_plan:
        return ["- 无新增操作计划"]
    lines = []
    for row in order_plan:
        action = row.get("action", "")
        symbol = row.get("symbol", "")
        qty = row.get("qty", "")
        tif = row.get("time_in_force", "")
        status = row.get("status", "")
        message = row.get("message", "")
        lines.append(f"- {action} {symbol} qty={qty} tif={tif} status={status} {message}".rstrip())
    return lines


def build_daily_email_body(context: dict) -> str:
    attachments = context.get("attachments", [])
    positions = context.get("positions", [])
    qqq_close = context.get("qqq_close")
    qqq_ma50 = context.get("qqq_ma50")
    qqq_text = "QQQ 数据暂缺"
    if qqq_close is not None and qqq_ma50 is not None:
        qqq_text = f"QQQ={float(qqq_close):.2f}  MA50={float(qqq_ma50):.2f}"

    lines = [
        "## 账户概览",
        f"- run_id: {context.get('run_id', '')}",
        f"- mode: {context.get('mode', '')}",
        f"- dry_run: {context.get('dry_run', '')}",
        f"- signal_date: {context.get('signal_date', '')}",
        f"- 账户净值: {_money(context.get('equity', 0))}",
        f"- 可用资金: {_money(context.get('buying_power', 0))}",
        f"- 高水位: {_money(context.get('high_watermark', 0))}",
        f"- 当前回撤: {float(context.get('drawdown', 0)):.2%}",
        "",
        "## 市场状态",
        f"- QQQ 状态: {context.get('regime', '')}",
        f"- {qqq_text}",
        "",
        "## 今日候选股",
        *_candidate_lines(context.get("candidates", pd.Series(dtype=float)), context.get("latest_prices", {})),
        "",
        "## 目标持仓",
        f"- {context.get('target_syms', [])}",
        "",
        "## 当前持仓",
        *( [_position_line(p) for p in positions] if positions else ["- 当前无持仓"] ),
        "",
        "## 操作记录",
        *_order_plan_lines(context.get("order_plan", [])),
        "",
        "## 成交/滑点",
        f"- fills_recorded: {context.get('fills_recorded', 0)}",
        "- 滑点统计: 暂未自动计算，详见 fills.csv 与 signals.csv",
        "",
        "## 风控状态",
        f"- kill_switch: {context.get('kill_switch', False)}",
        f"- stop_orders_submitted: {context.get('stop_orders_submitted', 0)}",
        "",
        "## 附件说明",
        f"- {', '.join(attachments) if attachments else '无附件'}",
        "",
        "## 最近日志",
        context.get("log_tail", ""),
    ]
    return "\n".join(lines)


# ── 市场数据拉取 ───────────────────────────────────────────────────────────────
def _normalize_ohlcv(
    close: pd.DataFrame,
    high: pd.DataFrame,
    low: pd.DataFrame,
    vol: pd.DataFrame,
) -> tuple[pd.DataFrame, ...]:
    for df in (close, high, low, vol):
        df.columns = [str(c).upper() for c in df.columns]
        if hasattr(df.index, "tz") and df.index.tz is not None:
            df.index = df.index.tz_localize(None)
    return close, high, low, vol


def _extract_ohlcv(raw: pd.DataFrame, symbols: list[str]) -> tuple[pd.DataFrame, ...]:
    """把 yfinance 的单票/多票返回值统一成 Close/High/Low/Volume 四个面板。"""
    if raw is None or raw.empty:
        empty = pd.DataFrame()
        return empty, empty.copy(), empty.copy(), empty.copy()

    if isinstance(raw.columns, pd.MultiIndex):
        if "Close" in raw.columns.get_level_values(0):
            close = raw["Close"].copy()
            high  = raw["High"].copy()
            low   = raw["Low"].copy()
            vol   = raw["Volume"].copy()
        else:
            close = raw.xs("Close", axis=1, level=-1).copy()
            high  = raw.xs("High", axis=1, level=-1).copy()
            low   = raw.xs("Low", axis=1, level=-1).copy()
            vol   = raw.xs("Volume", axis=1, level=-1).copy()
    else:
        sym = symbols[0]
        close = raw[["Close"]].rename(columns={"Close": sym})
        high  = raw[["High"]].rename(columns={"High": sym})
        low   = raw[["Low"]].rename(columns={"Low": sym})
        vol   = raw[["Volume"]].rename(columns={"Volume": sym})

    return _normalize_ohlcv(close, high, low, vol)


def _download_symbols(symbols: list[str], days: int, threads: bool) -> tuple[pd.DataFrame, ...]:
    raw = yf.download(
        symbols,
        period=f"{days}d",
        auto_adjust=True,
        progress=False,
        threads=threads,
    )
    return _extract_ohlcv(raw, symbols)


def _alpaca_feed() -> DataFeed:
    feeds = {
        "iex": DataFeed.IEX,
        "sip": DataFeed.SIP,
    }
    return feeds.get(ALPACA_FEED, DataFeed.SIP)


def _fetch_alpaca_chunk(
    client: StockHistoricalDataClient,
    symbols: list[str],
    days: int,
) -> tuple[pd.DataFrame, ...]:
    end = pd.Timestamp.utcnow().to_pydatetime()
    start = (pd.Timestamp.utcnow() - pd.Timedelta(days=days * 2)).to_pydatetime()
    req = StockBarsRequest(
        symbol_or_symbols=symbols,
        timeframe=TimeFrame.Day,
        start=start,
        end=end,
        adjustment=Adjustment.ALL,
        feed=_alpaca_feed(),
    )
    bars = client.get_stock_bars(req)
    df = bars.df
    if df is None or df.empty:
        empty = pd.DataFrame()
        return empty, empty.copy(), empty.copy(), empty.copy()

    if isinstance(df.index, pd.MultiIndex):
        symbol_level = "symbol" if "symbol" in df.index.names else 0
        time_level = "timestamp" if "timestamp" in df.index.names else 1
        close = df["close"].unstack(symbol_level)
        high = df["high"].unstack(symbol_level)
        low = df["low"].unstack(symbol_level)
        vol = df["volume"].unstack(symbol_level)
        close.index = close.index.get_level_values(time_level) if isinstance(close.index, pd.MultiIndex) else close.index
    else:
        sym = symbols[0]
        close = df[["close"]].rename(columns={"close": sym})
        high = df[["high"]].rename(columns={"high": sym})
        low = df[["low"]].rename(columns={"low": sym})
        vol = df[["volume"]].rename(columns={"volume": sym})

    return _normalize_ohlcv(close.sort_index(), high.sort_index(), low.sort_index(), vol.sort_index())


def _concat_panels(panels: list[tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]]) -> tuple[pd.DataFrame, ...]:
    non_empty = [panel for panel in panels if not panel[0].empty]
    if not non_empty:
        empty = pd.DataFrame()
        return empty, empty.copy(), empty.copy(), empty.copy()
    close = pd.concat([p[0] for p in non_empty], axis=1)
    high = pd.concat([p[1] for p in non_empty], axis=1)
    low = pd.concat([p[2] for p in non_empty], axis=1)
    vol = pd.concat([p[3] for p in non_empty], axis=1)
    return _normalize_ohlcv(close.sort_index(), high.sort_index(), low.sort_index(), vol.sort_index())


def _fetch_alpaca_panel(symbols: list[str], days: int) -> tuple[pd.DataFrame, ...]:
    client = StockHistoricalDataClient(API_KEY, SECRET_KEY)
    panels = []
    for i in range(0, len(symbols), DATA_BATCH_SIZE):
        chunk = symbols[i : i + DATA_BATCH_SIZE]
        try:
            panels.append(_fetch_alpaca_chunk(client, chunk, days))
        except Exception as e:
            log.warning(f"  Alpaca 批量拉取失败 {chunk[0]}..{chunk[-1]}: {e}，改为单票重试")
            for sym in chunk:
                try:
                    panels.append(_fetch_alpaca_chunk(client, [sym], days))
                except Exception as single_e:
                    log.warning(f"    {sym} Alpaca 单票拉取失败: {single_e}")
    return _concat_panels(panels)


def _merge_symbol_panel(
    close: pd.DataFrame,
    high: pd.DataFrame,
    low: pd.DataFrame,
    vol: pd.DataFrame,
    sym: str,
    retry_panel: tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame],
) -> tuple[pd.DataFrame, ...]:
    retry_close, retry_high, retry_low, retry_vol = retry_panel
    if sym not in retry_close.columns or retry_close[sym].count() < MIN_DATA_ROWS:
        return close, high, low, vol

    all_index = close.index.union(retry_close.index).sort_values()
    close = close.reindex(all_index)
    high = high.reindex(all_index)
    low = low.reindex(all_index)
    vol = vol.reindex(all_index)

    close[sym] = retry_close[sym].reindex(all_index)
    high[sym] = retry_high[sym].reindex(all_index)
    low[sym] = retry_low[sym].reindex(all_index)
    vol[sym] = retry_vol[sym].reindex(all_index)
    return close, high, low, vol


def fetch_panel(symbols: list, days: int) -> tuple:
    """
    拉取全股票池 + QQQ + SPY 最近 days 天日线。
    返回 (close, high, low, vol) — 与 factor_scanner 的 load_panel 接口兼容。
    """
    requested_stale = sorted(set(symbols) & STALE_SYMBOLS)
    if requested_stale:
        log.warning(f"  跳过已失效代码: {requested_stale}")

    all_syms = sorted((set(symbols) - STALE_SYMBOLS) | {"QQQ", "SPY"})
    if DATA_SOURCE == "yfinance":
        log.info(f"[yfinance] 拉取 {len(all_syms)} 只 × {days} 天日线...")
        close, high, low, vol = _download_symbols(all_syms, days, threads=True)
    else:
        log.info(f"[Alpaca:{ALPACA_FEED}] 拉取 {len(all_syms)} 只 × {days} 天日线...")
        close, high, low, vol = _fetch_alpaca_panel(all_syms, days)

    weak = [
        sym for sym in all_syms
        if sym not in close.columns or close[sym].count() < MIN_DATA_ROWS
    ]
    if weak and DATA_SOURCE == "yfinance":
        log.warning(f"  批量下载中 {len(weak)} 只缺失/不足，单票重试...")
        recovered = []
        for sym in weak:
            try:
                retry_panel = _download_symbols([sym], days, threads=False)
                before = sym in close.columns and close[sym].count() >= MIN_DATA_ROWS
                close, high, low, vol = _merge_symbol_panel(close, high, low, vol, sym, retry_panel)
                after = sym in close.columns and close[sym].count() >= MIN_DATA_ROWS
                if not before and after:
                    recovered.append(sym)
            except Exception as e:
                log.warning(f"    {sym} 单票重试失败: {e}")
        if recovered:
            log.info(f"  单票重试恢复: {recovered}")
    elif weak:
        log.warning(f"  Alpaca 数据不足/缺失: {len(weak)} 只")

    # 过滤数据量不足（< 150 行）的标的
    valid = [c for c in close.columns if close[c].count() >= MIN_DATA_ROWS]
    n_skip = len(close.columns) - len(valid)
    if n_skip:
        log.warning(f"  [{n_skip} 只数据不足，跳过]")
    log.info(f"  有效: {len(valid)} 只  "
             f"{close.index[0].date()} ~ {close.index[-1].date()}")
    return close[valid], high[valid], low[valid], vol[valid]


# ── 信号计算 ──────────────────────────────────────────────────────────────────
def compute_today_signals(
    close: pd.DataFrame,
    high:  pd.DataFrame,
    low:   pd.DataFrame,
    vol:   pd.DataFrame,
) -> tuple:
    """
    用最新收盘价计算今日 Combo Score，返回 (top_syms, regime_str, scores_series)。
    逻辑与 factor_combo_backtest.py 完全一致，确保 Paper 信号可与回测复现对比。
    """
    qqq_close = close["QQQ"]

    # 提取成分股（排除基准）
    stocks = [s for s in close.columns if s not in ("QQQ", "SPY")]
    c, h, l, v = close[stocks], high[stocks], low[stocks], vol[stocks]

    # ── 因子计算（复用 research 代码）───────────────────────────────────────
    log.info("计算因子（RS_Beta / MFI_14 / BIAS_20 / HV_ratio）...")
    all_f = compute_factors(c, h, l, v, qqq_close)
    core  = {f: all_f[f] for f in CORE_FACTORS if f in all_f}

    # ── 流动性掩码（20日均换手 >= $5M，价格 >= $2）─────────────────────────
    liquid = build_liquidity_mask(c, v)
    log.info(f"  今日流动性达标: {int(liquid.iloc[-1].sum())} 只")

    # ── 截面 Z-Score 标准化 ──────────────────────────────────────────────────
    log.info("截面 Z-Score 标准化...")
    z_panels = zscore_factors(core, liquid)

    # ── QQQ MA50 状态机 ──────────────────────────────────────────────────────
    qqq_last = float(qqq_close.iloc[-1])
    qqq_ma50 = float(qqq_close.rolling(50).mean().iloc[-1])
    if qqq_last > qqq_ma50:
        regime, rstr = 1, "牛市"
    elif qqq_last < qqq_ma50:
        regime, rstr = -1, "熊市"
    else:
        regime, rstr = 0, "震荡"
    log.info(f"QQQ 状态: {rstr}  (QQQ={qqq_last:.2f}  MA50={qqq_ma50:.2f})")

    # ── 今日 Combo Score（当日权重加权求和）────────────────────────────────
    weights     = REGIME_WEIGHTS[regime]
    combo_today = pd.Series(0.0, index=c.columns)
    for fname, w in weights.items():
        if w != 0 and fname in z_panels:
            combo_today = combo_today.add(
                z_panels[fname].iloc[-1] * w, fill_value=0
            )

    # ── Vol_Shock（当日成交量 / 20日均量）────────────────────────────────────
    vol_shock = (v.iloc[-1] / v.rolling(20).mean().iloc[-1]).fillna(0)

    # ── 三重过滤：流动性 & Combo & Vol ──────────────────────────────────────
    liquid_now = liquid.iloc[-1].reindex(combo_today.index, fill_value=False)
    mask = (
        liquid_now
        & (combo_today > MIN_SCORE)
        & (vol_shock.reindex(combo_today.index, fill_value=0) > VOL_MIN)
    )
    candidates = combo_today[mask].dropna()
    top_syms   = candidates.nlargest(TOP_N).index.tolist()

    log.info(f"候选股（Combo>{MIN_SCORE}, Vol>{VOL_MIN}x）: {len(candidates)} 只")
    if top_syms:
        log.info(f"Top-{TOP_N}: {top_syms}")
        log.info(f"Scores: {candidates.nlargest(TOP_N).round(3).to_dict()}")
    else:
        log.warning("⚠️  双过滤后无候选股（市场极端状态？）")

    return top_syms, rstr, candidates


# ── 调仓执行 ──────────────────────────────────────────────────────────────────
def rebalance(
    client:        TradingClient,
    target_syms:   list,
    close:         pd.DataFrame,
    equity:        float,
    dry_run:       bool,
    signal_date:   str,
    run_id:        str,
    audit:         Optional[AuditWriter] = None,
    order_plan:    Optional[list] = None,
    earnings_allow: Optional[set] = None,
) -> int:
    """
    对比 Alpaca 当前持仓与目标 Top-N，生成并提交差异订单。
    返回实际提交的订单数量。

    调仓原则：
      - 不在 Top-N 的仓位 → 全额平仓（市价）
      - Top-N 中新增的标的 → 按等权买入（equity / TOP_N / price）
      - Top-N 中已持有的标的 → 保持不动（不做权重再平衡）

    earnings_allow：手动豁免名单（--earnings-allow），命中财报避雷的标的若在此名单中则不强制出场/不移出买入计划。
    仅本次运行生效，需由人工确认财报预期正面后手动传入。
    """
    if not target_syms:
        log.warning("⚠️  目标持仓为空，跳过本次调仓，保持现有持仓不动")
        return 0

    positions   = client.get_all_positions()
    current_map = {p.symbol: p for p in positions}
    target_set  = set(target_syms)
    n_orders    = 0

    # 财报避雷：未来 EARNINGS_BLACKOUT_DAYS 天内有财报的股票强制回避
    if EARNINGS_BLACKOUT_DAYS > 0:
        check_syms = list((target_set | set(current_map.keys())) - {"QQQ", "SPY"})
        earnings_blackout = get_upcoming_earnings(check_syms, EARNINGS_BLACKOUT_DAYS)
        if earnings_allow:
            overridden = earnings_blackout & earnings_allow
            if overridden:
                log.warning(f"  ⚠️ 手动豁免财报避雷：{sorted(overridden)}（人工确认不强制出场，风险自负）")
                earnings_blackout -= earnings_allow
        if earnings_blackout:
            log.warning(f"  📅 财报避雷命中：{sorted(earnings_blackout)} 移出买入计划并强制出场")
            target_set -= earnings_blackout
    else:
        earnings_blackout: set[str] = set()

    val_each = equity / len(target_set) if target_set else 0.0  # 等权仓位金额（财报避雷过滤后重新计算）

    # 1. 平掉不在目标列表的旧持仓（含财报避雷强制出场）
    to_exit = [sym for sym in current_map if sym not in target_set or sym in earnings_blackout]
    for sym in to_exit:
        log.info(f"  SELL  {sym:8s}（全仓平仓）")
        if audit:
            row = {
                "run_id": run_id,
                "signal_date": signal_date,
                "action": "SELL_CLOSE",
                "symbol": sym,
                "qty": "",
                "order_type": "close_position",
                "time_in_force": "",
                "client_order_id": "",
                "status": "dry_run" if dry_run else "planned",
                "message": "不在目标持仓",
            }
            audit.append_order(row)
            if order_plan is not None:
                order_plan.append(row)
        if not dry_run:
            try:
                client.close_position(sym)
                n_orders += 1
            except Exception as e:
                log.error(f"    ❌ 平仓失败 {sym}: {e}")

    # 2. 买入目标列表中尚未持有的标的（财报避雷已从 target_set 中剔除）
    to_enter = [sym for sym in target_syms if sym in target_set and sym not in current_map]
    for sym in to_enter:
        if sym not in close.columns:
            log.warning(f"  ⚠️  {sym} 无价格数据，跳过")
            continue
        price = float(close[sym].dropna().iloc[-1])
        if price <= 0:
            log.warning(f"  ⚠️  {sym} 价格异常（{price}），跳过")
            continue
        qty = max(1, int(val_each / price))
        log.info(f"  BUY   {sym:8s} × {qty:4d} @ ~${price:8.2f}  (≈${val_each:,.0f})")
        order_req = build_market_order(sym, qty, OrderSide.BUY, signal_date, "enter")
        if audit:
            row = {
                "run_id": run_id,
                "signal_date": signal_date,
                "action": "BUY",
                "symbol": sym,
                "qty": qty,
                "order_type": "market",
                "time_in_force": order_req.time_in_force.value,
                "client_order_id": order_req.client_order_id,
                "status": "dry_run" if dry_run else "planned",
                "message": f"reference_price={price:.4f}",
            }
            audit.append_order(row)
            if order_plan is not None:
                order_plan.append(row)
        if not dry_run:
            try:
                order = client.submit_order(order_req)
                log.info(f"    ✅ 订单 id={order.id}  status={order.status}")
                n_orders += 1
            except Exception as e:
                err_msg = str(e).lower()
                if any(kw in err_msg for kw in ("halt", "not_tradable", "suspended", "asset_not_tradable")):
                    log.warning(f"    ⚠️ {sym} 停牌/LULD 熔断，写入重试队列")
                    _append_halt_pending(sym, qty, signal_date, run_id)
                else:
                    log.error(f"    ❌ 买入失败 {sym}: {e}")

    if not to_exit and not to_enter:
        log.info("  持仓无需变动（目标与当前完全一致）")
    elif dry_run:
        log.info("  [DRY RUN] 上述订单均未提交")

    return n_orders


# ── 主流程 ────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Alpaca 量化策略执行脚本")
    parser.add_argument("--dry-run", action="store_true",
                        help="仅预览调仓计划，不实际下单")
    parser.add_argument("--force",   action="store_true",
                        help="忽略 5 日间隔，强制立刻调仓")
    parser.add_argument("--allow-duplicate", action="store_true",
                        help="允许同一 signal_date 重复提交订单（默认禁止）")
    parser.add_argument("--retry-halted", action="store_true",
                        help="重试因 LULD 熔断被拒的挂单（由单独 cron 在 9:45 AM ET 触发）")
    parser.add_argument("--earnings-allow", type=str, default="",
                        help="逗号分隔股票代码，本次运行手动豁免财报避雷强制出场（如 MU,AAPL）。"
                             "仅本次生效，需人工确认财报预期正面后使用，风险自负。")
    args = parser.parse_args()
    earnings_allow = {s.strip().upper() for s in args.earnings_allow.split(",") if s.strip()}

    if args.retry_halted:
        retry_halted_orders(dry_run=args.dry_run)
        return
    run_id = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    audit = AuditWriter(AUDIT_DIR)
    status = "started"
    email_lines = []
    run_summary = {
        "signal_date": "",
        "equity": "",
        "buying_power": "",
        "high_watermark": "",
        "drawdown": "",
        "regime": "",
        "qqq_close": "",
        "qqq_ma50": "",
        "target_symbols": "",
        "orders_submitted": "",
        "fills_recorded": "",
        "stop_orders_submitted": "",
    }

    try:
        ensure_live_confirmation()

        log.info("=" * 64)
        log.info(f"策略执行开始  "
                 f"模式={'Paper' if PAPER else '⚠️ LIVE'}  "
                 f"{'[DRY RUN] ' if args.dry_run else ''}"
                 f"{'[FORCE] ' if args.force else ''}")

        if not API_KEY or "YOUR_API_KEY" in API_KEY:
            log.error("❌ 未配置 API Keys，请编辑 .env 文件")
            status = "missing_api_key"
            return

    # ── 加载状态 ─────────────────────────────────────────────────────────────
        state = _load_state()
        if state.get("kill_switch"):
            log.critical("🚨 Kill Switch 已激活，程序拒绝执行。"
                         "手动将 state.json 中 kill_switch 改为 false 后方可恢复。")
            status = "kill_switch_locked"
            return

    # ── 连接 Alpaca ──────────────────────────────────────────────────────────
        log.info("连接 Alpaca...")
        try:
            client  = TradingClient(API_KEY, SECRET_KEY, paper=PAPER)
            account = client.get_account()
        except Exception:
            status = "api_connection_failed"
            raise
        equity  = float(account.equity)
        buying_power = float(account.buying_power)
        run_summary["equity"] = f"{equity:.2f}"
        run_summary["buying_power"] = f"{buying_power:.2f}"
        log.info(f"账户净值: ${equity:>12,.2f}  "
                 f"可用资金: ${buying_power:>12,.2f}")

    # ── Kill Switch 检查 ─────────────────────────────────────────────────────
        hw = float(state["high_watermark"]) if state["high_watermark"] else equity
        if equity > hw:
            hw = equity
        state["high_watermark"] = hw

        dd = (equity - hw) / hw
        run_summary["high_watermark"] = f"{hw:.2f}"
        run_summary["drawdown"] = f"{dd:.6f}"
        log.info(f"高水位: ${hw:>12,.2f}  当前回撤: {dd:.1%}")

        if dd <= KILL_DD:
            log.critical(f"🚨 Kill Switch 触发！账户从高水位回撤 {dd:.1%}（阈值 {KILL_DD:.0%}）")
            ks_positions = client.get_all_positions()
            _kill_switch_liquidate(client, ks_positions, args.dry_run)
            state["kill_switch"] = True
            _save_state(state)
            status = "kill_switch_triggered"
            log.critical("   kill_switch=True，程序退出。检查策略后手动解除。")
            return

    # ── 调仓日检查（每 5 交易日）────────────────────────────────────────────
        today   = date.today()
        last_rb = state.get("last_rebalance")
        if last_rb and not args.force:
            last_date = date.fromisoformat(str(last_rb))
            bd_elapsed = len(pd.bdate_range(last_date, today)) - 1
            log.info(f"上次调仓: {last_date}  已过 {bd_elapsed} 个交易日")
            if bd_elapsed < REBALANCE_DAYS:
                remain = REBALANCE_DAYS - bd_elapsed
                log.info(f"  非调仓日（还差 {remain} 日），跳过。用 --force 强制执行。")
                _save_state(state)
                status = "skipped_rebalance_interval"
                return

        log.info("→ 调仓日确认，拉取数据并计算信号...")

    # ── 数据拉取 & 信号计算 ──────────────────────────────────────────────────
        symbols, _ = load_trading_universe()
        try:
            close, high, low, vol = fetch_panel(symbols, DATA_DAYS)
        except Exception:
            status = "market_data_failed"
            raise
        try:
            validate_panel(close)
        except Exception:
            status = "data_validation_failed"
            raise
        signal_date = str(close.index[-1].date())
        run_summary["signal_date"] = signal_date
        if is_duplicate_signal(state, signal_date, args.dry_run, args.allow_duplicate or ALLOW_DUPLICATE_SIGNAL):
            log.warning(f"⚠️  signal_date={signal_date} 已提交过订单，跳过以避免重复下单")
            status = "duplicate_signal_skipped"
            return
        existing_positions = client.get_all_positions()
        stop_orders_submitted = ensure_stop_orders_for_positions(client, existing_positions, signal_date, args.dry_run)
        fills_recorded = record_recent_fills(client, audit, run_id, signal_date)
        target_syms, regime_str, candidates = compute_today_signals(close, high, low, vol)
        latest_prices = close.iloc[-1].to_dict()
        audit.append_signal_rows(run_id, signal_date, candidates, latest_prices)
        qqq_close = float(close["QQQ"].iloc[-1]) if "QQQ" in close.columns else None
        qqq_ma50 = float(close["QQQ"].rolling(50).mean().iloc[-1]) if "QQQ" in close.columns else None
        run_summary["regime"] = regime_str
        run_summary["qqq_close"] = f"{qqq_close:.4f}" if qqq_close is not None else ""
        run_summary["qqq_ma50"] = f"{qqq_ma50:.4f}" if qqq_ma50 is not None else ""
        run_summary["target_symbols"] = ",".join(target_syms)
        run_summary["fills_recorded"] = str(fills_recorded)
        run_summary["stop_orders_submitted"] = str(stop_orders_submitted)

        # ── 执行调仓 ─────────────────────────────────────────────────────────────
        sizing_capital = SIM_CAPITAL_USD if SIM_CAPITAL_USD > 0 else equity
        if SIM_CAPITAL_USD > 0:
            log.info(f"  💰 仓位规模模拟：按 ${SIM_CAPITAL_USD:,.0f} 计算买入数量（账户真实净值 ${equity:,.2f} 仅用于回撤/Kill Switch 判断）")
        log.info(f"\n目标持仓 ({regime_str}, Top-{len(target_syms)}): {target_syms}")
        order_plan = []
        if earnings_allow:
            log.info(f"  ⚠️ 本次手动豁免财报避雷名单：{sorted(earnings_allow)}")
        try:
            n = rebalance(client, target_syms, close, sizing_capital, args.dry_run, signal_date, run_id, audit, order_plan,
                           earnings_allow=earnings_allow)
        except Exception:
            status = "order_submit_failed"
            raise
        run_summary["orders_submitted"] = str(n)

    # ── 更新状态 ─────────────────────────────────────────────────────────────
        if not args.dry_run:
            state["last_rebalance"] = str(today)
            state["last_order_signal_date"] = signal_date
        _save_state(state)
        status = "ok"

        email_lines = [build_daily_email_body({
            "run_id": run_id,
            "mode": "Paper" if PAPER else "LIVE",
            "dry_run": args.dry_run,
            "signal_date": signal_date,
            "equity": equity,
            "buying_power": buying_power,
            "high_watermark": hw,
            "drawdown": dd,
            "regime": regime_str,
            "qqq_close": qqq_close,
            "qqq_ma50": qqq_ma50,
            "candidates": candidates,
            "latest_prices": latest_prices,
            "target_syms": target_syms,
            "positions": existing_positions,
            "order_plan": order_plan,
            "fills_recorded": fills_recorded,
            "stop_orders_submitted": stop_orders_submitted,
            "kill_switch": state.get("kill_switch", False),
            "attachments": [p.name for p in _audit_attachments()],
            "log_tail": _read_log_tail(80),
        })]
        log.info(f"\n本次执行完成，提交 {n} 笔订单")
        log.info("=" * 64)
    except Exception:
        if status in ("started", "ok"):
            status = "error"
        err = traceback.format_exc()
        log.error(err)
        email_lines = [f"run_id: {run_id}", "status: error", err]
        raise
    finally:
        audit.append_run({
            "run_id": run_id,
            "run_at_utc": datetime.utcnow().isoformat(timespec="seconds"),
            "status": status,
            "mode": "Paper" if PAPER else "LIVE",
            "dry_run": args.dry_run,
            **run_summary,
        })
        try:
            send_email(
                build_email_subject(status, run_id, PAPER),
                "\n".join(email_lines) if email_lines else f"run_id: {run_id}\nstatus: {status}",
                _audit_attachments(),
            )
        except Exception as email_e:
            log.warning(f"邮件发送失败: {email_e}")


if __name__ == "__main__":
    main()

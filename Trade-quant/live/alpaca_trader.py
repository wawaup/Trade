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
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from pathlib import Path
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from typing import Optional
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import yfinance as yf
import pandas_market_calendars as mcal
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
ORDER_TIF      = os.getenv("ORDER_TIF", "day").lower()   # OPG 需 Elite Smart Router，普通账户用 DAY
ALLOW_DUPLICATE_SIGNAL = os.getenv("ALLOW_DUPLICATE_SIGNAL", "false").lower() == "true"
ENABLE_STOP_ORDERS = os.getenv("ENABLE_STOP_ORDERS", "true").lower() == "true"
STOP_LOSS_PCT  = float(os.getenv("STOP_LOSS_PCT", "0.25"))
PRICE_SANITY_PCT = float(os.getenv("PRICE_SANITY_PCT", "0.5"))  # 最新价相对前一交易日收盘价的最大允许偏离
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


# ── 交易日历（节假日感知）───────────────────────────────────────────────────────
_NYSE = mcal.get_calendar("NYSE")


def trading_days_elapsed(start: date, end: date) -> int:
    """start（不含）到 end（含）之间的真实交易日数量，感知节假日。"""
    sched = _NYSE.schedule(start_date=start, end_date=end)
    if sched.empty:
        return 0
    days = sched.index.date.tolist()
    return len([d for d in days if d > start])


def is_trading_day(d: date) -> bool:
    return not _NYSE.schedule(start_date=d, end_date=d).empty


def next_trading_day(d: date) -> date:
    sched = _NYSE.schedule(start_date=d + timedelta(days=1), end_date=d + timedelta(days=14))
    return sched.index[0].date()


# ── 状态持久化 ─────────────────────────────────────────────────────────────────
STATE_BACKUP_FILE = LIVE_DIR / "state.json.bak"
LOCK_FILE = LIVE_DIR / ".trader.lock"


def _load_state() -> dict:
    if not STATE_FILE.exists():
        return {"high_watermark": None, "last_rebalance": None, "kill_switch": False}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        raise RuntimeError(
            f"state.json 解析失败，拒绝静默重置为空状态（防止误触发重复调仓）："
            f"{e}。请检查 {STATE_FILE}，必要时从 {STATE_BACKUP_FILE} 手动恢复。"
        ) from e


def _save_state(s: dict):
    if STATE_FILE.exists():
        try:
            STATE_BACKUP_FILE.write_text(STATE_FILE.read_text(encoding="utf-8"), encoding="utf-8")
        except Exception as e:
            log.warning(f"state.json 备份失败（不影响本次写入）: {e}")
    tmp_path = STATE_FILE.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(s, default=str, indent=2), encoding="utf-8")
    os.replace(tmp_path, STATE_FILE)


class ProcessLock:
    """基于 PID 文件的单机进程锁：防止 plan/sell/buy/retry-halted 并发读写 state.json。"""

    def __init__(self, path: Path):
        self.path = path
        self.acquired = False

    def acquire(self) -> bool:
        try:
            fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            self.acquired = True
            return True
        except FileExistsError:
            try:
                old_pid = int(self.path.read_text().strip())
                os.kill(old_pid, 0)
                return False  # 旧进程仍存活，锁有效
            except (ValueError, ProcessLookupError, PermissionError):
                pass
            except OSError:
                return False
            # 旧进程已不存在，锁文件是残留的，清理后重试一次
            self.path.unlink(missing_ok=True)
            return self.acquire()

    def release(self):
        if self.acquired:
            self.path.unlink(missing_ok=True)
            self.acquired = False


def _slug_date(value: str) -> str:
    return value.replace("-", "")


def _order_time_in_force() -> TimeInForce:
    # OPG 仅在美东 7:00pm–9:28am 窗口有效；盘后运行请用 DAY
    mapping = {
        "opg": TimeInForce.OPG,
        "day": TimeInForce.DAY,
    }
    return mapping.get(ORDER_TIF, TimeInForce.DAY)


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
    client_order_id = f"tq-stop-{symbol.lower()}-{_slug_date(signal_date)}"
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


def validate_panel(close: pd.DataFrame, expect_today: bool = False):
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
    if expect_today and latest != today:
        raise RuntimeError(f"最新行情日期滞后: 最新bar={latest}，今天={today}，疑似未收敛/陈旧数据，拒绝使用")


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


_SENSITIVE_LOG_KEYWORDS = ("API_KEY", "SECRET", "PASSWORD", "TOKEN")


def _read_log_tail(max_lines: int = 100) -> str:
    if not LOG_FILE.exists():
        return ""
    lines = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
    tail = lines[-max_lines:]
    redacted = [
        "[已脱敏：疑似包含密钥/口令关键字]"
        if any(kw in line.upper() for kw in _SENSITIVE_LOG_KEYWORDS) else line
        for line in tail
    ]
    return "\n".join(redacted)


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
        "buy_completed_with_issues",
        "last_run_failed",
    }
    return status in emergency


def build_email_subject(status: str, run_id: str, paper: bool) -> str:
    mode = "Paper" if paper else "LIVE"
    labels = {
        "ok": "普通日报-运行成功",
        "skipped_rebalance_interval": "普通日报-非调仓日",
        "duplicate_signal_skipped": "普通日报-重复信号跳过",
        "cycle_already_pending": "普通日报-上一周期未完成",
        "plan_saved": "普通日报-调仓计划已生成",
        "plan_saved_earnings_degraded": "警报-调仓计划已生成（财报避雷未完全生效）",
        "sell_submitted": "普通日报-卖出已提交",
        "buy_completed": "普通日报-调仓周期完成",
        "buy_completed_with_issues": "紧急报警-卖单未确认成交",
        "no_pending_plan": "普通日报-无待执行计划",
        "not_a_trading_day": "普通日报-非交易日跳过",
        "api_connection_failed": "紧急报警-API连接失败",
        "market_data_failed": "紧急报警-行情数据异常",
        "data_validation_failed": "紧急报警-数据校验失败",
        "order_submit_failed": "紧急报警-下单失败",
        "service_stale": "紧急报警-服务失效",
        "missing_api_key": "紧急报警-配置缺失",
        "kill_switch_locked": "紧急报警-熔断锁定",
        "kill_switch_triggered": "紧急报警-熔断触发",
        "last_run_failed": "紧急报警-上次运行状态异常",
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

    # 查询当前所有活跃挂单，找出已有的 GTC stop 单（按 tq-stop-{sym} 前缀匹配）
    existing_stops: dict[str, object] = {}
    try:
        open_orders = client.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN))
        for o in open_orders:
            cid = str(getattr(o, "client_order_id", "") or "")
            sym_o = getattr(o, "symbol", "")
            if cid.startswith(f"tq-stop-{sym_o.lower()}") and sym_o:
                existing_stops[sym_o] = o
    except Exception as e:
        log.warning(f"  查询活跃挂单失败，跳过止损单去重检查: {e}")

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
            desired_stop = round(ref_price * (1 - STOP_LOSS_PCT), 2)

            existing = existing_stops.get(sym)
            if existing is not None:
                existing_price = float(getattr(existing, "stop_price", 0) or 0)
                if abs(existing_price - desired_stop) < 0.01:
                    log.info(f"  STOP  {sym:8s} 已有止损单 @ ${existing_price:.2f}，跳过")
                    continue
                # 止损价已变，撤旧单再补新单
                log.info(f"  STOP  {sym:8s} 止损价更新 ${existing_price:.2f} → ${desired_stop:.2f}，撤旧补新")
                if not dry_run:
                    try:
                        client.cancel_order_by_id(existing.id)
                    except Exception as ce:
                        log.warning(f"    撤销旧止损单失败 {sym}: {ce}")

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


EARNINGS_LOOKUP_TIMEOUT_SEC = float(os.getenv("EARNINGS_LOOKUP_TIMEOUT_SEC", "10"))


def _fetch_earnings_calendar(sym: str):
    """单只股票的 yf.Ticker(...).calendar 调用，交给线程池以便加超时保护。"""
    return yf.Ticker(sym).calendar


def get_upcoming_earnings(symbols: list[str], days_ahead: int = 2) -> tuple[set[str], bool]:
    """返回 (blackout, degraded)：
    blackout 为在未来 days_ahead 个交易日内发布财报的股票集合（财报避雷针）；
    degraded 表示本次查询是否有过半标的失败/超时（财报避雷本次可能未完全生效）。
    yfinance 不同版本的 ticker.calendar 返回值结构不一：dict / DataFrame / None。
    单只股票的解析错误/超时均静默跳过，原则：宁可错过一次避雷，不能因 API 异常挂断发单主流程；
    但整体失败率过高时需要在邮件里显式提示，而不是完全静默。
    """
    if days_ahead <= 0:
        return set(), False
    blackout: set[str] = set()
    today = pd.Timestamp.today().normalize()
    cutoff = today + pd.offsets.BDay(days_ahead)
    check_syms = [s for s in symbols if s not in ("QQQ", "SPY")]
    failed = 0
    # 注意：不用 `with ThreadPoolExecutor(...) as pool` —— yf 的网络调用一旦发起无法从
    # 外部中断，若用 with 语句，退出时会阻塞等待所有（含已超时的慢）线程跑完，超时保护形同虚设。
    # 这里改为 shutdown(wait=False)：主流程按超时及时返回，慢线程留给后台自行跑完后回收。
    pool = ThreadPoolExecutor(max_workers=8)
    try:
        futures = {pool.submit(_fetch_earnings_calendar, sym): sym for sym in check_syms}
        for fut, sym in futures.items():
            try:
                cal = fut.result(timeout=EARNINGS_LOOKUP_TIMEOUT_SEC)
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
            except FutureTimeoutError:
                failed += 1
                log.warning(f"  {sym} 财报日历查询超时（>{EARNINGS_LOOKUP_TIMEOUT_SEC:.0f}s，跳过，默认放行）")
            except Exception as e:
                failed += 1
                log.warning(f"  {sym} 财报日历查询失败（跳过，默认放行）: {e}")
    finally:
        pool.shutdown(wait=False)
    degraded = bool(check_syms) and (failed / len(check_syms)) > 0.5
    if degraded:
        log.warning(f"  ⚠️ 财报避雷本次可能未完全生效：{failed}/{len(check_syms)} 只股票查询失败/超时")
    return blackout, degraded


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
        today_slug = _slug_date(str(date.today()))
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
                client_order_id=f"tq-ks-{today_slug}-{sym.lower()}",
            )
            log.critical(f"    SELL {sym} × {qty} @ ${limit_price:.2f} [盘后限价]")
            if not dry_run:
                try:
                    client.submit_order(req)
                    submitted += 1
                except Exception as e:
                    err_msg = str(e).lower()
                    if any(kw in err_msg for kw in ("already exists", "duplicate")):
                        log.info(f"    ✅ {sym} 逃生单此前已提交（client_order_id 重复，视为成功）")
                        submitted += 1
                        continue
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
                cid = f"tq-retry-{_slug_date(o.get('signal_date', data.get('pending_date', '')))}-{sym.lower()}"
                limit_req = LimitOrderRequest(
                    symbol=sym,
                    qty=qty,
                    side=OrderSide.BUY,
                    time_in_force=TimeInForce.DAY,
                    limit_price=limit_price,
                    client_order_id=cid,
                )
                log.info(f"    RETRY BUY {sym} × {qty} @ ${limit_price:.2f}")
                if not dry_run:
                    client.submit_order(limit_req)
                log.info(f"    ✅ {sym} 重试订单已提交")
            except Exception as e:
                err_msg = str(e).lower()
                if any(kw in err_msg for kw in ("already exists", "duplicate")):
                    log.info(f"    ✅ {sym} 重试订单此前已提交（client_order_id 重复，视为成功）")
                elif any(kw in err_msg for kw in ("halt", "not_tradable", "suspended", "asset_not_tradable")):
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


def build_skip_email_body(context: dict) -> str:
    """
    非调仓日 / 周期未完成等 skip 场景的可读日报。取代此前几乎空白的 `run_id/status` 邮件。
    """
    positions = context.get("positions", [])
    weekday_cn = "一二三四五六日"[date.today().weekday()]

    lines = [
        f"## 今日结论：{context.get('conclusion', '无操作')}",
        f"- 日期: {date.today()}（周{weekday_cn}）",
        f"- run_id: {context.get('run_id', '')}",
        f"- status: {context.get('status', '')}",
    ]
    if context.get("last_rebalance"):
        lines.append(f"- 上次调仓完成日: {context['last_rebalance']}")
    if context.get("elapsed") is not None and context.get("rebalance_days") is not None:
        elapsed, need = context["elapsed"], context["rebalance_days"]
        remain = max(0, need - elapsed)
        lines.append(f"- 已过交易日: {elapsed} / {need}（还差 {remain} 个交易日，节假日已计入）")
        if context.get("next_calc_date"):
            lines.append(f"- 预计下次调仓计算日: 约 {context['next_calc_date']}")

    lines += [
        "",
        "## 账户概览",
        f"- 账户净值: {_money(context.get('equity', 0))} | "
        f"可用资金: {_money(context.get('buying_power', 0))} | "
        f"当前回撤: {float(context.get('drawdown', 0)):.2%}",
        "",
        "## 当前持仓",
        *( [_position_line(p) for p in positions] if positions else ["- 当前无持仓"] ),
        "",
        "## 当前周期状态",
        f"- cycle_plan: {context.get('cycle_plan_summary', '无')}",
        f"- pending_sell: {context.get('pending_sell_summary', '无')}",
    ]

    if context.get("warning"):
        lines += ["", "## ⚠️ 需要人工介入", context["warning"]]

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

    if close.empty:
        raise RuntimeError(
            f"[{DATA_SOURCE}] 行情数据全部拉取失败（0 只有效），"
            f"请检查数据源订阅权限（如 ALPACA_DATA_FEED={ALPACA_FEED} 是否在当前账户套餐内）"
        )

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


# ── 三阶段调仓状态机：plan（T日收盘后）→ sell（T+1尾盘前）→ buy（T+2开盘前）────────
CYCLE_PLAN_KEY   = "cycle_plan"    # phase=plan 写入，phase=sell 消费后清空
PENDING_SELL_KEY = "pending_sell"  # phase=sell 写入，phase=buy 消费后清空


def compute_rebalance_plan(
    client: TradingClient,
    target_syms: list,
    close: pd.DataFrame,
    equity: float,
    buying_power: float,
    signal_date: str,
    earnings_allow: Optional[set] = None,
) -> dict:
    """
    纯计算：对比当前持仓与目标 Top-N，生成清仓/减仓/买入计划，不提交任何订单。
    等权目标金额 = equity / len(target_syms)，5% 缓冲防开盘跳空超支。
    """
    positions   = client.get_all_positions()
    current_map = {p.symbol: p for p in positions}
    target_set  = set(target_syms)

    earnings_blackout: set = set()
    earnings_degraded = False
    if EARNINGS_BLACKOUT_DAYS > 0:
        check_syms = list((target_set | set(current_map.keys())) - {"QQQ", "SPY"})
        earnings_blackout, earnings_degraded = get_upcoming_earnings(check_syms, EARNINGS_BLACKOUT_DAYS)
        if earnings_allow:
            overridden = earnings_blackout & earnings_allow
            if overridden:
                log.warning(f"  ⚠️ 手动豁免财报避雷：{sorted(overridden)}（人工确认不强制出场，风险自负）")
                earnings_blackout -= earnings_allow
        if earnings_blackout:
            log.warning(f"  📅 财报避雷命中：{sorted(earnings_blackout)} 移出买入计划并强制出场")
            target_set -= earnings_blackout

    target_val = equity / len(target_set) if target_set else 0.0
    close_all, trim, buy = [], [], []

    for sym in current_map:
        if sym not in target_set or sym in earnings_blackout:
            mv  = float(getattr(current_map[sym], "market_value", 0) or 0)
            qty = int(float(getattr(current_map[sym], "qty", 0) or 0))
            if qty > 0:
                close_all.append({"symbol": sym, "qty": qty, "market_value": round(mv, 2)})

    for sym in target_syms:
        if sym not in target_set:
            continue
        if sym not in close.columns:
            log.warning(f"  ⚠️  {sym} 无价格数据，跳过")
            continue
        sym_closes = close[sym].dropna()
        price = float(sym_closes.iloc[-1])
        if price <= 0:
            log.warning(f"  ⚠️  {sym} 价格异常（{price}），跳过")
            continue
        if len(sym_closes) >= 2:
            prev_price = float(sym_closes.iloc[-2])
            if prev_price > 0:
                dev = abs(price - prev_price) / prev_price
                if dev > PRICE_SANITY_PCT:
                    log.warning(
                        f"  ⚠️  {sym} 最新价 {price:.4f} 相对前一交易日 {prev_price:.4f} "
                        f"偏离 {dev:.0%}（阈值 {PRICE_SANITY_PCT:.0%}），疑似数据异常，跳过本次调仓"
                    )
                    continue
        target_qty  = max(1, int(target_val / (price * 1.05)))
        current_qty = int(float(getattr(current_map[sym], "qty", 0) or 0)) if sym in current_map else 0
        delta       = target_qty - current_qty
        curr_val    = float(getattr(current_map[sym], "market_value", 0) or 0) if sym in current_map else 0.0
        drift       = (curr_val - target_val) / target_val if target_val > 0 else 0.0

        if delta < 0:
            trim.append({"symbol": sym, "qty": abs(delta), "price": round(price, 4), "drift": round(drift, 6)})
        elif delta > 0:
            buy.append({"symbol": sym, "qty": delta, "price": round(price, 4),
                        "is_new": current_qty == 0, "drift": round(drift, 6)})
        else:
            log.info(f"  HOLD  {sym:8s} qty={current_qty}  val≈${curr_val:,.0f}  偏差={drift:+.1%}")

    est_sell_value = sum(c["market_value"] for c in close_all) + sum(t["qty"] * t["price"] for t in trim)
    est_buy_total  = sum(b["qty"] * b["price"] for b in buy)
    est_available  = buying_power + est_sell_value
    log.info(
        f"  资金预估: buying_power=${buying_power:,.0f}  "
        f"卖出释放≈${est_sell_value:,.0f}  买入需求≈${est_buy_total:,.0f}  "
        f"可用≈${est_available:,.0f}"
    )
    if est_buy_total > est_available * 1.03:
        log.warning(
            f"  ⚠️ 买入需求 ${est_buy_total:,.0f} 超过预估可用资金 ${est_available:,.0f}"
            f"（差额 ${est_buy_total - est_available:,.0f}），次日 buy 阶段会按实际可用资金自动缩减"
        )

    return {
        "plan_date": signal_date,
        "signal_date": signal_date,
        "target_syms": target_syms,
        "target_val": round(target_val, 2),
        "close_all": close_all,
        "trim": trim,
        "buy": buy,
        "est_sell_value": round(est_sell_value, 2),
        "est_buy_total": round(est_buy_total, 2),
        "est_available": round(est_available, 2),
        "earnings_degraded": earnings_degraded,
    }


def execute_sell_phase(client: TradingClient, state: dict, run_id: str, dry_run: bool,
                        audit: Optional[AuditWriter] = None) -> dict:
    """
    phase=sell（T+1 尾盘前运行）：读取 cycle_plan，对 close_all/trim 提交限价卖单
    （吃买一价，确保收盘前迅速成交），写入 pending_sell，清空 cycle_plan。
    """
    plan = state.get(CYCLE_PLAN_KEY)
    summary = {"had_plan": bool(plan), "orders": [], "buy_carry": [], "note": ""}
    if not plan:
        log.info("  cycle_plan 为空，今日无待卖出计划。")
        return summary

    today     = date.today()
    plan_date = date.fromisoformat(plan["plan_date"])
    expected  = next_trading_day(plan_date)
    if today != expected:
        summary["note"] = f"补跑：计划日为 {plan_date}，理应 {expected} 执行，实际 {today} 执行"
        log.warning(f"  ⚠️ {summary['note']}")

    sell_targets = [(c["symbol"], c["qty"], "close") for c in plan.get("close_all", [])] + \
                   [(t["symbol"], t["qty"], "trim") for t in plan.get("trim", [])]

    data_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)
    orders_out = []
    for sym, qty, kind in sell_targets:
        bid = 0.0
        try:
            quote_req = StockLatestQuoteRequest(symbol_or_symbols=[sym])
            quotes = data_client.get_stock_latest_quote(quote_req)
            bid = float(quotes[sym].bid_price) if sym in quotes else 0.0
        except Exception as e:
            log.warning(f"  {sym} 报价查询失败: {e}")

        if bid <= 0:
            log.warning(f"  ⚠️ {sym} 买一价异常（{bid}），改用市价单兜底")
            order_req = build_market_order(sym, qty, OrderSide.SELL, plan["signal_date"], kind)
            limit_txt = "market"
        else:
            client_order_id = f"tq-{_slug_date(plan['signal_date'])}-{kind}-{sym.lower()}"
            order_req = LimitOrderRequest(
                symbol=sym, qty=qty, side=OrderSide.SELL,
                time_in_force=TimeInForce.DAY, limit_price=bid,
                client_order_id=client_order_id,
            )
            limit_txt = f"${bid:.2f}"
        log.info(f"  SELL  {sym:8s} × {qty:4d}（{kind}）@ {limit_txt}")
        row = {
            "run_id": run_id, "signal_date": plan["signal_date"],
            "action": f"SELL_{kind.upper()}", "symbol": sym, "qty": qty,
            "order_type": "limit" if bid > 0 else "market",
            "time_in_force": order_req.time_in_force.value,
            "client_order_id": order_req.client_order_id,
            "status": "dry_run" if dry_run else "planned",
            "message": f"尾盘前主动让价卖出 bid={bid:.4f}",
        }
        if audit:
            audit.append_order(row)
        if not dry_run:
            try:
                client.submit_order(order_req)
            except Exception as e:
                err_msg = str(e).lower()
                if any(kw in err_msg for kw in ("already exists", "duplicate")):
                    log.info(f"    ✅ {sym} 卖单此前已提交（client_order_id 重复，视为成功）")
                else:
                    log.error(f"    ❌ 卖出失败 {sym}: {e}")
                    continue
        orders_out.append({"symbol": sym, "qty": qty, "client_order_id": order_req.client_order_id, "kind": kind})

    summary["orders"]     = orders_out
    summary["buy_carry"]  = plan.get("buy", [])
    summary["all_failed"] = bool(sell_targets) and not orders_out

    if not dry_run:
        if summary["all_failed"]:
            log.error("  ❌ 卖出阶段全部提交失败，保留 cycle_plan 以便下次重试，不推进周期")
        else:
            state[PENDING_SELL_KEY] = {
                "sell_date": str(today),
                "signal_date": plan["signal_date"],
                "orders": orders_out,
                "buy_carry": plan.get("buy", []),
            }
            state.pop(CYCLE_PLAN_KEY, None)
    return summary


def execute_buy_phase(client: TradingClient, state: dict, run_id: str, dry_run: bool,
                       audit: Optional[AuditWriter] = None) -> dict:
    """
    phase=buy（T+2 开盘前运行）：逐笔核实 pending_sell 中卖单的实际成交情况，
    再用账户当下真实可用资金提交买单（资金不足则按比例缩减，保留最小 1 股）。
    """
    pending = state.get(PENDING_SELL_KEY)
    summary = {"had_pending": bool(pending), "fill_issues": [], "orders": [], "scaled": False, "note": ""}
    if not pending:
        log.info("  pending_sell 为空，今日无待买入计划。")
        return summary

    today     = date.today()
    sell_date = date.fromisoformat(pending["sell_date"])
    expected  = next_trading_day(sell_date)
    if today != expected:
        summary["note"] = f"补跑：卖出日为 {sell_date}，理应 {expected} 执行，实际 {today} 执行"
        log.warning(f"  ⚠️ {summary['note']}")

    # ── 逐笔核实卖单成交（不能假设 DAY 单一定成交）────────────────────────────
    bad_symbols = set()
    for o in pending.get("orders", []):
        cid = o["client_order_id"]
        try:
            order = client.get_order_by_client_id(cid)
            filled_qty = float(getattr(order, "filled_qty", 0) or 0)
            status_val = getattr(getattr(order, "status", ""), "value", str(getattr(order, "status", "")))
            if filled_qty < o["qty"]:
                issue = f"{o['symbol']} 卖单未完全成交（filled={filled_qty}/{o['qty']}, status={status_val}）"
                log.warning(f"  ⚠️ {issue}")
                summary["fill_issues"].append(issue)
                bad_symbols.add(o["symbol"])
        except Exception as e:
            issue = f"{o['symbol']} 卖单成交状态查询失败: {e}"
            log.warning(f"  ⚠️ {issue}")
            summary["fill_issues"].append(issue)
            bad_symbols.add(o["symbol"])

    summary["had_fill_issues"] = bool(bad_symbols)

    # ── 用真实可用资金提交买单 ───────────────────────────────────────────────
    account = client.get_account()
    real_buying_power = float(account.buying_power)
    buy_carry = pending.get("buy_carry", [])
    if bad_symbols:
        skipped_syms = [b["symbol"] for b in buy_carry if b["symbol"] in bad_symbols]
        if skipped_syms:
            log.warning(f"  ⚠️ 以下标的卖单未确认成交，本轮暂停对应买入决策: {skipped_syms}")
        buy_carry = [b for b in buy_carry if b["symbol"] not in bad_symbols]
    est_buy_total = sum(b["qty"] * b["price"] for b in buy_carry)

    scale = 1.0
    if est_buy_total > 0 and est_buy_total > real_buying_power:
        scale = max(0.0, (real_buying_power * 0.97) / est_buy_total)
        summary["scaled"] = True
        log.warning(
            f"  ⚠️ 买入需求 ${est_buy_total:,.0f} 超过实际可用资金 ${real_buying_power:,.0f}，"
            f"按比例缩减至 {scale:.1%}"
        )

    orders_out = []
    for b in buy_carry:
        qty = max(1, int(b["qty"] * scale)) if scale < 1.0 else b["qty"]
        if qty <= 0:
            continue
        sym, price, is_new, drift = b["symbol"], b["price"], b.get("is_new", True), b.get("drift", 0.0)
        action_tag   = "enter" if is_new else "add"
        order_req    = build_market_order(sym, qty, OrderSide.BUY, pending["signal_date"], action_tag)
        action_label = "新建" if is_new else f"加仓 drift={drift:+.1%}"
        log.info(f"  BUY   {sym:8s} × {qty:4d} @ ~${price:8.2f}（{action_label}）")
        row = {
            "run_id": run_id, "signal_date": pending["signal_date"],
            "action": "BUY", "symbol": sym, "qty": qty,
            "order_type": "market", "time_in_force": order_req.time_in_force.value,
            "client_order_id": order_req.client_order_id,
            "status": "dry_run" if dry_run else "planned",
            "message": f"phase=buy drift={drift:+.1%} ref_price={price:.4f} scale={scale:.2f}",
        }
        if audit:
            audit.append_order(row)
        if not dry_run:
            try:
                client.submit_order(order_req)
            except Exception as e:
                err_msg = str(e).lower()
                if any(kw in err_msg for kw in ("already exists", "duplicate")):
                    log.info(f"    ✅ {sym} 买单此前已提交（client_order_id 重复，视为成功）")
                elif any(kw in err_msg for kw in ("halt", "not_tradable", "suspended", "asset_not_tradable")):
                    log.warning(f"    ⚠️ {sym} 停牌/LULD 熔断，写入重试队列")
                    _append_halt_pending(sym, qty, pending["signal_date"], run_id)
                else:
                    log.error(f"    ❌ 买入失败 {sym}: {e}")
                continue
        orders_out.append({"symbol": sym, "qty": qty})

    summary["orders"] = orders_out

    # ── 买入提交后立即补挂止损单，避免新仓位在下次 plan 阶段前无保护 ────────────
    stop_orders_submitted = 0
    if orders_out or dry_run:
        try:
            fresh_positions = client.get_all_positions()
            stop_orders_submitted = ensure_stop_orders_for_positions(
                client, fresh_positions, pending["signal_date"], dry_run
            )
        except Exception as e:
            log.warning(f"  ⚠️ 买入后补挂止损单失败: {e}")
    summary["stop_orders_submitted"] = stop_orders_submitted

    if not dry_run:
        state["last_rebalance"] = str(today)
        state["last_order_signal_date"] = pending["signal_date"]
        state.pop(PENDING_SELL_KEY, None)
    return summary


# ── 调仓执行 ──────────────────────────────────────────────────────────────────
def rebalance(
    client:        TradingClient,
    target_syms:   list,
    close:         pd.DataFrame,
    equity:        float,
    buying_power:  float,
    dry_run:       bool,
    signal_date:   str,
    run_id:        str,
    audit:         Optional[AuditWriter] = None,
    order_plan:    Optional[list] = None,
    earnings_allow: Optional[set] = None,
) -> int:
    """
    对比 Alpaca 当前持仓与目标 Top-N，同一次运行内同时提交卖单和买单，返回订单数量。
    仅供 --phase both（人工 dry-run/一次性测试）使用；生产 cron 走
    compute_rebalance_plan() → execute_sell_phase() → execute_buy_phase() 三段式，
    卖出与买入分属不同交易日，避免资金衔接和成交确认问题。

    调仓原则（严格等权，每次调仓后消除漂移）：
      - 不在 Top-N 的仓位 → 全额平仓（market）
      - Top-N 中所有标的 → 按 equity/N/1.05 计算目标数量（5% 缓冲防开盘跳空超支）
      - 超重则 TRIM，欠重/新增则 BUY

    earnings_allow：手动豁免名单（--earnings-allow）。
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
        earnings_blackout, earnings_degraded = get_upcoming_earnings(check_syms, EARNINGS_BLACKOUT_DAYS)
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

    # ── 等权目标金额 ─────────────────────────────────────────────────────────────
    target_val = equity / len(target_set) if target_set else 0.0

    # ── 预计算全部操作计划（先算完再下单，方便资金预估） ─────────────────────────
    close_all_list   = []           # [(sym, market_value)]  全仓清出
    trim_list        = []           # [(sym, qty, price)]    减仓至目标
    buy_list         = []           # [(sym, qty, price, is_new)]  加仓/新建

    # 1. 退出仓位
    for sym in current_map:
        if sym not in target_set or sym in earnings_blackout:
            mv = float(getattr(current_map[sym], "market_value", 0) or 0)
            close_all_list.append((sym, mv))

    # 2. 目标仓位中的每只：计算 delta
    for sym in target_syms:
        if sym not in target_set:
            continue
        if sym not in close.columns:
            log.warning(f"  ⚠️  {sym} 无价格数据，跳过")
            continue
        price = float(close[sym].dropna().iloc[-1])
        if price <= 0:
            log.warning(f"  ⚠️  {sym} 价格异常（{price}），跳过")
            continue
        target_qty  = max(1, int(target_val / (price * 1.05)))  # 5% 缓冲防开盘跳空超支
        current_qty = int(float(getattr(current_map[sym], "qty", 0) or 0)) if sym in current_map else 0
        delta       = target_qty - current_qty

        curr_val = float(getattr(current_map[sym], "market_value", 0) or 0) if sym in current_map else 0.0
        drift    = (curr_val - target_val) / target_val if target_val > 0 else 0.0

        if delta < 0:
            trim_list.append((sym, abs(delta), price, drift))
        elif delta > 0:
            buy_list.append((sym, delta, price, current_qty == 0, drift))
        else:
            log.info(f"  HOLD  {sym:8s} qty={current_qty}  val≈${curr_val:,.0f}  偏差={drift:+.1%}")

    # ── 资金预估 & 警告 ──────────────────────────────────────────────────────────
    est_sell_value = (
        sum(mv for _, mv in close_all_list)
        + sum(qty * price for _, qty, price, _ in trim_list)
    )
    est_buy_total  = sum(qty * price for _, qty, price, *_ in buy_list)
    est_available  = buying_power + est_sell_value
    log.info(
        f"  资金预估: buying_power=${buying_power:,.0f}  "
        f"卖出释放≈${est_sell_value:,.0f}  买入需求≈${est_buy_total:,.0f}  "
        f"可用≈${est_available:,.0f}"
    )
    if est_buy_total > est_available * 1.03:
        log.warning(
            f"  ⚠️ 买入需求 ${est_buy_total:,.0f} 超过预估可用资金 ${est_available:,.0f}"
            f"（差额 ${est_buy_total - est_available:,.0f}）"
        )
        if _order_time_in_force() == TimeInForce.DAY:
            log.warning(
                "      DAY 模式：卖单与买单同步提交，若卖单未先成交则买单可能因资金不足被拒"
            )

    # ── 1. SELL 阶段：清仓 + trim ────────────────────────────────────────────────
    for sym, mv in close_all_list:
        # 取实际持仓 qty；close_position() 不支持 TIF，改用 MarketOrderRequest 确保 DAY 生效
        cur_qty = int(float(getattr(current_map[sym], "qty", 0) or 0)) if sym in current_map else 0
        if cur_qty <= 0:
            log.warning(f"  ⚠️ {sym} 持仓 qty=0，跳过清仓")
            continue
        log.info(f"  SELL  {sym:8s} 全仓 ×{cur_qty}（不在目标，val≈${mv:,.0f}）")
        order_req = build_market_order(sym, cur_qty, OrderSide.SELL, signal_date, "close")
        row = {
            "run_id": run_id, "signal_date": signal_date,
            "action": "SELL_CLOSE", "symbol": sym, "qty": cur_qty,
            "order_type": "market", "time_in_force": order_req.time_in_force.value,
            "client_order_id": order_req.client_order_id,
            "status": "dry_run" if dry_run else "planned",
            "message": f"不在目标持仓 mv={mv:.0f}",
        }
        if audit:
            audit.append_order(row)
            if order_plan is not None:
                order_plan.append(row)
        if not dry_run:
            try:
                client.submit_order(order_req)
                n_orders += 1
            except Exception as e:
                log.error(f"    ❌ 平仓失败 {sym}: {e}")

    for sym, qty, price, drift in trim_list:
        log.info(f"  TRIM  {sym:8s} × {qty:4d} @ ~${price:8.2f}  偏差={drift:+.1%}（等权减仓）")
        order_req = build_market_order(sym, qty, OrderSide.SELL, signal_date, "trim")
        row = {
            "run_id": run_id, "signal_date": signal_date,
            "action": "SELL_TRIM", "symbol": sym, "qty": qty,
            "order_type": "market", "time_in_force": order_req.time_in_force.value,
            "client_order_id": order_req.client_order_id,
            "status": "dry_run" if dry_run else "planned",
            "message": f"drift={drift:+.1%} ref_price={price:.4f}",
        }
        if audit:
            audit.append_order(row)
            if order_plan is not None:
                order_plan.append(row)
        if not dry_run:
            try:
                client.submit_order(order_req)
                n_orders += 1
            except Exception as e:
                log.error(f"    ❌ TRIM 失败 {sym}: {e}")

    # ── 2. BUY 阶段：新建 + add ──────────────────────────────────────────────────
    for sym, qty, price, is_new, drift in buy_list:
        action_label = "新建" if is_new else f"加仓 drift={drift:+.1%}"
        log.info(f"  BUY   {sym:8s} × {qty:4d} @ ~${price:8.2f}  目标≈${target_val:,.0f}（{action_label}）")
        action_tag = "enter" if is_new else "add"
        order_req = build_market_order(sym, qty, OrderSide.BUY, signal_date, action_tag)
        row = {
            "run_id": run_id, "signal_date": signal_date,
            "action": "BUY", "symbol": sym, "qty": qty,
            "order_type": "market", "time_in_force": order_req.time_in_force.value,
            "client_order_id": order_req.client_order_id,
            "status": "dry_run" if dry_run else "planned",
            "message": f"target_val={target_val:.0f} drift={drift:+.1%} ref_price={price:.4f}",
        }
        if audit:
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
                    raise

    if not close_all_list and not trim_list and not buy_list:
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
    parser.add_argument("--phase", type=str, default="both",
                        choices=["both", "plan", "sell", "buy"],
                        help=(
                            "三阶段执行模式（默认 both = 人工一次性卖+买，仅供 dry-run/手动测试）:\n"
                            "  plan — T 日收盘后运行（~16:05 ET）：计算目标持仓与买卖计划，写入 state.json，不下单\n"
                            "  sell — T+1 尾盘前运行（~15:50 ET）：读取计划，主动让价到买一价提交限价卖单\n"
                            "  buy  — T+2 开盘前运行（~09:15 ET）：核实卖单成交后，用实际可用资金提交买单\n"
                            "三段之间用真实交易日（节假日感知）衔接，不是固定周几。"
                        ))
    args = parser.parse_args()
    earnings_allow = {s.strip().upper() for s in args.earnings_allow.split(",") if s.strip()}

    lock = ProcessLock(LOCK_FILE)
    if not lock.acquire():
        log.warning(
            f"⚠️ 检测到另一个实例正在运行（锁文件 {LOCK_FILE} 已被占用），本次跳过，"
            "避免并发读写 state.json / halt_pending.json"
        )
        return
    try:
        _main_impl(args, earnings_allow)
    finally:
        lock.release()


def _main_impl(args, earnings_allow):
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

    # ── 连接 Alpaca ──────────────────────────────────────────────────────────
        log.info("连接 Alpaca...")
        try:
            client  = TradingClient(API_KEY, SECRET_KEY, paper=PAPER)
            account = client.get_account()
        except Exception:
            status = "api_connection_failed"
            raise

    # ── 加载状态 ─────────────────────────────────────────────────────────────
        state = _load_state()
        if state.get("kill_switch"):
            log.critical("🚨 Kill Switch 已激活，程序拒绝执行。"
                         "手动将 state.json 中 kill_switch 改为 false 后方可恢复。")
            status = "kill_switch_locked"
            try:
                remaining = client.get_all_positions()
            except Exception as e:
                log.error(f"  锁定期间查询残留持仓失败: {e}")
                remaining = []
            if remaining:
                log.critical(f"  🚨 锁定期间仍有 {len(remaining)} 笔残留持仓，强制市价清仓兜底（不解除锁定）")
                if not args.dry_run:
                    try:
                        client.close_all_positions(cancel_orders=True)
                    except Exception as e:
                        log.error(f"  锁定期间强制清仓失败: {e}")
            return
        equity  = float(account.equity)
        buying_power = float(account.buying_power)
        run_summary["equity"] = f"{equity:.2f}"
        run_summary["buying_power"] = f"{buying_power:.2f}"
        log.info(f"账户净值: ${equity:>12,.2f}  "
                 f"可用资金: ${buying_power:>12,.2f}")
        existing_positions = client.get_all_positions()

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

    # ── 交易日历硬检查：cron 按周几触发（1-5），不认节假日，这里兜底 ──────────────
        if args.phase in ("plan", "sell", "buy") and not is_trading_day(date.today()):
            log.info(f"今天 {date.today()} 非美股交易日（节假日），phase={args.phase} 跳过。")
            status = "not_a_trading_day"
            _save_state(state)
            email_lines = [build_skip_email_body({
                "run_id": run_id, "status": status,
                "conclusion": f"今天非交易日（节假日），phase={args.phase} 未执行任何操作",
                "equity": equity, "buying_power": buying_power, "drawdown": dd,
                "positions": existing_positions,
                "last_rebalance": state.get("last_rebalance"),
                "cycle_plan_summary": "有" if state.get(CYCLE_PLAN_KEY) else "无",
                "pending_sell_summary": "有" if state.get(PENDING_SELL_KEY) else "无",
            })]
            return

    # ── phase=sell / phase=buy：独立于 plan/both 的调仓周期主流程 ─────────────
        if args.phase == "sell":
            summary = execute_sell_phase(client, state, run_id, args.dry_run, audit)
            _save_state(state)
            if not summary["had_plan"]:
                status = "no_pending_plan"
                email_lines = [build_skip_email_body({
                    "run_id": run_id, "status": status,
                    "conclusion": "无待卖出计划（cycle_plan 为空）",
                    "equity": equity, "buying_power": buying_power, "drawdown": dd,
                    "positions": existing_positions,
                    "last_rebalance": state.get("last_rebalance"),
                    "cycle_plan_summary": "无", "pending_sell_summary": "无",
                })]
            elif summary.get("all_failed"):
                status = "order_submit_failed"
                email_lines = ["\n".join([
                    "## 今日结论：尾盘卖出全部提交失败",
                    f"- run_id: {run_id}",
                    "- ⚠️ 本轮 cycle_plan 中的卖单全部提交失败，已保留 cycle_plan 供下次 sell 重试，未推进周期",
                    "- 请人工检查 API 连接/账户状态/标的是否可交易",
                ])]
            else:
                status = "sell_submitted"
                order_lines = [f"- {o['symbol']} {o['kind']} × {o['qty']}（client_order_id={o['client_order_id']}）"
                               for o in summary["orders"]] or ["- 无实际提交（全部下单失败，请查日志）"]
                buy_carry_lines = [f"- {b['symbol']} × {b['qty']} @ ~${b['price']:.2f}"
                                    for b in summary["buy_carry"]] or ["- 无后续买入计划"]
                email_lines = ["\n".join([
                    "## 今日结论：尾盘前卖出已提交",
                    f"- run_id: {run_id}",
                    f"- 提示: {summary['note']}" if summary["note"] else "",
                    "",
                    "## 已提交卖单",
                    *order_lines,
                    "",
                    "## 次日开盘前将执行的买入计划预览",
                    *buy_carry_lines,
                ])]
            log.info(f"[phase=sell] 完成，status={status}")
            return

        if args.phase == "buy":
            summary = execute_buy_phase(client, state, run_id, args.dry_run, audit)
            _save_state(state)
            if not summary["had_pending"]:
                status = "no_pending_plan"
                email_lines = [build_skip_email_body({
                    "run_id": run_id, "status": status,
                    "conclusion": "无待买入计划（pending_sell 为空）",
                    "equity": equity, "buying_power": buying_power, "drawdown": dd,
                    "positions": existing_positions,
                    "last_rebalance": state.get("last_rebalance"),
                    "cycle_plan_summary": "有" if state.get(CYCLE_PLAN_KEY) else "无",
                    "pending_sell_summary": "无",
                })]
            else:
                status = "buy_completed_with_issues" if summary.get("had_fill_issues") else "buy_completed"
                order_lines = [f"- {o['symbol']} × {o['qty']}" for o in summary["orders"]] or ["- 无实际提交"]
                issue_lines = summary["fill_issues"] or ["- 无异常，全部卖单如期成交"]
                email_lines = ["\n".join([
                    "## 今日结论：调仓周期完成" + ("（存在卖单未确认成交，需人工复核）" if summary.get("had_fill_issues") else ""),
                    f"- run_id: {run_id}",
                    f"- 提示: {summary['note']}" if summary["note"] else "",
                    f"- 买入资金是否缩减: {'是' if summary['scaled'] else '否'}",
                    f"- 买入后新挂止损单: {summary.get('stop_orders_submitted', 0)} 笔",
                    "",
                    "## 卖单成交核实",
                    *issue_lines,
                    "",
                    "## 已提交买单（未确认成交标的本轮已跳过买入）",
                    *order_lines,
                ])]
            log.info(f"[phase=buy] 完成，status={status}")
            return

    # ── phase=plan：上一周期未完成则不重新计算 ────────────────────────────────
        if args.phase == "plan" and (state.get(CYCLE_PLAN_KEY) or state.get(PENDING_SELL_KEY)):
            log.warning("⚠️ 上一周期尚未完成 buy，本次不重新计算调仓计划")
            status = "cycle_already_pending"
            _save_state(state)
            email_lines = [build_skip_email_body({
                "run_id": run_id, "status": status,
                "conclusion": "上一周期未完成，跳过本次计划生成",
                "equity": equity, "buying_power": buying_power, "drawdown": dd,
                "positions": existing_positions,
                "last_rebalance": state.get("last_rebalance"),
                "cycle_plan_summary": json.dumps(state.get(CYCLE_PLAN_KEY), ensure_ascii=False) if state.get(CYCLE_PLAN_KEY) else "无",
                "pending_sell_summary": json.dumps(state.get(PENDING_SELL_KEY), ensure_ascii=False) if state.get(PENDING_SELL_KEY) else "无",
                "warning": "cycle_plan 或 pending_sell 卡住未清空，请人工检查 state.json 并确认上一轮 buy 是否已手动完成。",
            })]
            return

    # ── 调仓日检查（每 5 交易日）────────────────────────────────────────────
        today   = date.today()
        last_rb = state.get("last_rebalance")
        if last_rb and not args.force:
            last_date = date.fromisoformat(str(last_rb))
            bd_elapsed = trading_days_elapsed(last_date, today)
            log.info(f"上次调仓: {last_date}  已过 {bd_elapsed} 个交易日（节假日感知）")
            if bd_elapsed < REBALANCE_DAYS:
                remain = REBALANCE_DAYS - bd_elapsed
                log.info(f"  非调仓日（还差 {remain} 日），跳过。用 --force 强制执行。")
                _save_state(state)
                status = "skipped_rebalance_interval"
                next_calc = today
                for _ in range(remain):
                    next_calc = next_trading_day(next_calc)
                email_lines = [build_skip_email_body({
                    "run_id": run_id, "status": status,
                    "conclusion": "非调仓日，无操作",
                    "equity": equity, "buying_power": buying_power, "drawdown": dd,
                    "positions": existing_positions,
                    "last_rebalance": last_rb,
                    "elapsed": bd_elapsed, "rebalance_days": REBALANCE_DAYS,
                    "next_calc_date": str(next_calc),
                    "cycle_plan_summary": "有" if state.get(CYCLE_PLAN_KEY) else "无",
                    "pending_sell_summary": "有" if state.get(PENDING_SELL_KEY) else "无",
                })]
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
            validate_panel(close, expect_today=is_trading_day(date.today()))
        except Exception:
            status = "data_validation_failed"
            raise
        signal_date = str(close.index[-1].date())
        run_summary["signal_date"] = signal_date
        if is_duplicate_signal(state, signal_date, args.dry_run, args.allow_duplicate or ALLOW_DUPLICATE_SIGNAL):
            log.warning(f"⚠️  signal_date={signal_date} 已提交过订单，跳过以避免重复下单")
            status = "duplicate_signal_skipped"
            return
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
        if earnings_allow:
            log.info(f"  ⚠️ 本次手动豁免财报避雷名单：{sorted(earnings_allow)}")

        if args.phase == "plan":
            plan = compute_rebalance_plan(
                client, target_syms, close, sizing_capital, buying_power,
                signal_date, earnings_allow=earnings_allow,
            )
            if not args.dry_run:
                state[CYCLE_PLAN_KEY] = plan
            _save_state(state)
            status = "plan_saved" if not plan.get("earnings_degraded") else "plan_saved_earnings_degraded"
            close_n, trim_n, buy_n = len(plan["close_all"]), len(plan["trim"]), len(plan["buy"])
            expected_sell = next_trading_day(date.fromisoformat(signal_date))
            email_lines = ["\n".join([
                "## 今日结论：调仓计划已生成（未下单）"
                + ("（⚠️ 财报避雷本次可能未完全生效）" if plan.get("earnings_degraded") else ""),
                f"- run_id: {run_id}",
                f"- signal_date: {signal_date}",
                f"- 清仓 {close_n} 只 / 减仓 {trim_n} 只 / 新建或加仓 {buy_n} 只",
                f"- 预计 {expected_sell} 尾盘前卖出，随后开盘前买入",
                f"- 资金预估: 卖出释放≈{_money(plan['est_sell_value'])}  "
                f"买入需求≈{_money(plan['est_buy_total'])}  可用≈{_money(plan['est_available'])}",
                "⚠️ 财报避雷本次可能未完全生效（超过一半标的财报日历查询失败/超时），请人工核实相关持仓财报日期"
                if plan.get("earnings_degraded") else "",
                "",
                "## 目标持仓",
                f"- {target_syms}",
            ])]
            run_summary["orders_submitted"] = "0"
            log.info(f"\n计划已生成：清仓{close_n}/减仓{trim_n}/买入{buy_n}")
            log.info("=" * 64)
            return

        order_plan = []
        try:
            n = rebalance(
                client, target_syms, close, sizing_capital, buying_power,
                args.dry_run, signal_date, run_id, audit, order_plan,
                earnings_allow=earnings_allow,
            )
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
        email_lines = [f"run_id: {run_id}", f"status: {status}", err]
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

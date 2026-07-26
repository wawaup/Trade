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
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError, as_completed
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
# 路径允许通过环境变量覆盖，主要用途：测试隔离（conftest.py 在 collect 阶段就把
# 全部路径重定向到 tmp 目录，避免 pytest 污染生产 state.json / trader.log / audit）。
# 生产环境不设置这些变量即用默认路径，行为完全不变。
STATE_FILE        = Path(os.getenv("TRADE_QUANT_STATE_FILE", str(LIVE_DIR / "state.json")))
LOG_FILE          = Path(os.getenv("TRADE_QUANT_LOG_FILE",   str(LIVE_DIR / "trader.log")))
AUDIT_DIR         = Path(os.getenv("TRADE_QUANT_AUDIT_DIR",  str(LIVE_DIR / "audit")))
HALT_PENDING_FILE = Path(os.getenv("TRADE_QUANT_HALT_PENDING_FILE", str(LIVE_DIR / "halt_pending.json")))

sys.path.insert(0, str(RESEARCH_DIR))
try:
    from factor_scanner import load_universe, compute_factors, build_liquidity_mask
    from factor_combo_backtest import zscore_factors, CORE_FACTORS, REGIME_WEIGHTS
    from build_universe import build_universe as build_universe_dict, save_universe as save_universe_dict
    from strategy_params import REBALANCE_DAYS, TOP_N, MIN_SCORE, VOL_MIN, KILL_DD
except ImportError as e:
    print(f"❌ 导入 research 模块失败：{e}")
    print("   请在 Trade-quant/live/ 目录内运行本脚本")
    sys.exit(1)

# ── 配置 ──────────────────────────────────────────────────────────────────────
load_dotenv(LIVE_DIR / ".env")
API_KEY    = os.getenv("ALPACA_API_KEY", "")
SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "")
PAPER      = os.getenv("ALPACA_PAPER", "true").lower() != "false"

# REBALANCE_DAYS / TOP_N / MIN_SCORE / VOL_MIN / KILL_DD 从 research/strategy_params.py
# 导入（单一来源，见该文件顶部说明），保证回测与实盘口径一致。
DATA_DAYS      = 350    # 拉取天数（RS_Beta 需 ≥ 60 天 Beta 稳定期，留 350 天余量）
MIN_DATA_ROWS  = 150    # 单票最少有效日线数量
DATA_SOURCE    = os.getenv("MARKET_DATA_SOURCE", "alpaca").lower()
ALPACA_FEED    = os.getenv("ALPACA_DATA_FEED", "sip").lower()
DATA_BATCH_SIZE = 50
MIN_VALID_SYMBOLS = int(os.getenv("MIN_VALID_SYMBOLS", "120"))
ORDER_TIF      = os.getenv("ORDER_TIF", "day").lower()   # OPG 需 Elite Smart Router，普通账户用 DAY

# 执行模式（2026-07-12 引入）：
#   three_phase (默认，向后兼容)：plan(T 16:05) → sell(T+1 15:50) → buy(T+2 09:15)
#   moc_single (新，推荐)：plan(T 16:05) → execute(T+1 15:35, MOC/MOC + LOO 兜底)
#                          → reconcile(T+2 09:45, 核实 LOO 兜底成交并补挂止损)
# 详见 FIX_PLAN §3 方案 A+ 与 RESEARCH_LOG §13。paper 验证一个完整调仓周期后可切默认。
EXEC_MODE      = os.getenv("EXEC_MODE", "three_phase").lower()
# MOC 提交截止：Alpaca 硬截止 15:50 ET，我们在 15:35 提交给 15 分钟余量应对 API 延迟。
MOC_CUTOFF_MIN_BEFORE_CLOSE = int(os.getenv("MOC_CUTOFF_MIN_BEFORE_CLOSE", "10"))
# LOO 兜底价保护：MOC 买单被拒后转 LOO 时，限价 = T+1 收盘 mid × (1 + 该保护)，
# 防止次日开盘跳空过深仍追进。0.03 = 允许 3% 跳空内成交。
LOO_FALLBACK_MAX_CHASE_PCT  = float(os.getenv("LOO_FALLBACK_MAX_CHASE_PCT", "0.03"))
ALLOW_DUPLICATE_SIGNAL = os.getenv("ALLOW_DUPLICATE_SIGNAL", "false").lower() == "true"
ENABLE_STOP_ORDERS = os.getenv("ENABLE_STOP_ORDERS", "true").lower() == "true"
STOP_LOSS_PCT  = float(os.getenv("STOP_LOSS_PCT", "0.25"))
PRICE_SANITY_PCT = float(os.getenv("PRICE_SANITY_PCT", "0.5"))  # 最新价相对前一交易日收盘价的最大允许偏离

# client_order_id 重复提交时，不同版本 Alpaca 报错文案不完全一致，统一在此维护关键字列表，
# 避免各下单路径各自维护一份、后续遗漏更新。
IDEMPOTENT_DUPLICATE_KEYWORDS = ("already exists", "duplicate", "must be unique")
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

# LULD 熔断重试（仅买入/加仓单）：有限次数重试上限，超过后放弃本轮买入（不产生持仓 = 不产生风险敞口）。
# 与卖出侧的"绝不自动放弃"策略刻意不同——买单错过只是错过机会，卖单错过是持续裸露风险。
HALT_RETRY_MAX_ATTEMPTS   = int(os.getenv("HALT_RETRY_MAX_ATTEMPTS", "6"))
HALT_RETRY_INTERVAL_SEC   = int(os.getenv("HALT_RETRY_INTERVAL_SEC", "300"))
# 追价上限：重试限价相对原计划参考价的最大允许涨幅，超过则本轮放弃该买单（不发邮件，视为正常追涨失败）。
# 与其它下单路径里"price * 1.05"式的仓位规模缓冲是两个独立概念，只是恰好数值相同，故单独设常量、不复用。
HALT_RETRY_MAX_CHASE_PCT  = float(os.getenv("HALT_RETRY_MAX_CHASE_PCT", "0.05"))

# 仓位规模模拟：留空/0 = 用 Alpaca 账户真实净值计算仓位；
# 设置后仅用此金额代替账户净值计算买入数量，账户净值/回撤/Kill Switch 判断仍基于真实账户（百分比口径不受影响）
SIM_CAPITAL_USD = float(os.getenv("SIM_CAPITAL_USD", "0"))

# 单标的仓位集中度上限：等权分配（可投资金额 / 候选数）超过此比例时按此比例封顶，
# 避免当日选股候选数过少（甚至只有1只）时单票吃满绝大部分资金——候选不足时宁可空仓，不加仓填满
MAX_POSITION_PCT = float(os.getenv("MAX_POSITION_PCT", "0.50"))

# 最小现金缓冲：仓位计算只用 (1 - 此比例) 的资金去分配，其余始终留作现金，
# 而不是把 100% 净值/模拟资金全部平分给候选标的
MIN_CASH_BUFFER_PCT = float(os.getenv("MIN_CASH_BUFFER_PCT", "0.05"))

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
STATE_BACKUP_FILE = Path(os.getenv("TRADE_QUANT_STATE_BACKUP_FILE",
                                   str(LIVE_DIR / "state.json.bak")))
LOCK_FILE = Path(os.getenv("TRADE_QUANT_LOCK_FILE", str(LIVE_DIR / ".trader.lock")))


# state.json 顶层键的默认值 —— 缺任何一个都会让下游代码 KeyError 崩溃或
# 让风控失效（丢失 high_watermark 会悄悄抬高 Kill Switch 基准）。
# 新增顶层键时，在此登记默认值即可自动获得 schema 校验与缺键报警。
_STATE_DEFAULTS: dict = {
    "high_watermark": None,   # 账户净值高水位，Kill Switch 回撤计算的分母
    "last_rebalance": None,   # 上次 buy 阶段完成日期（ISO 字符串），控制调仓间隔
    "kill_switch":    False,  # 触发后永久锁定，需人工清零
}

# 缺失后必须告警但可保守修复的关键键（子集）——用于区分"正常首次运行"和"文件损坏"：
# 首次运行时整个 state.json 不存在，_load_state 直接返回默认；
# 文件存在却缺这些键，则说明被测试污染 / 被人工误编辑 / 上游写入不完整。
_STATE_CRITICAL_KEYS = ("high_watermark", "last_rebalance", "kill_switch")


def _load_state() -> dict:
    if not STATE_FILE.exists():
        return dict(_STATE_DEFAULTS)
    try:
        raw = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        raise RuntimeError(
            f"state.json 解析失败，拒绝静默重置为空状态（防止误触发重复调仓）："
            f"{e}。请检查 {STATE_FILE}，必要时从 {STATE_BACKUP_FILE} 手动恢复。"
        ) from e
    if not isinstance(raw, dict):
        raise RuntimeError(
            f"state.json 顶层不是 dict（实际类型 {type(raw).__name__}），拒绝加载。"
            f" 请从 {STATE_BACKUP_FILE} 恢复。"
        )
    missing = [k for k in _STATE_CRITICAL_KEYS if k not in raw]
    if missing:
        # 文件存在但关键键缺失 —— 极大概率是被测试/人工编辑损坏，绝不能静默补默认，
        # 否则 kill_switch 会被重置为 False、high_watermark 会被重置为 None（下游取 equity），
        # 悄悄抬高熔断基准。硬失败让 cron 邮件报警走出来。
        raise RuntimeError(
            f"state.json 缺少关键键 {missing}——极可能被测试或人工编辑损坏。"
            f"拒绝加载（避免风控静默失效）。请从 {STATE_BACKUP_FILE} 恢复，"
            f"或人工核对后补齐这些键。"
        )
    # 非关键键（如 pending_buy/pending_sell/cycle_plan/audit 元信息）缺失是正常的，
    # 用默认值填齐即可；已有键保持不变。
    merged = dict(_STATE_DEFAULTS)
    merged.update(raw)
    return merged


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


# ── MOC / MOO / LOO 订单构造器（EXEC_MODE=moc_single 使用）───────────────────
# 三段式历史设计要求 T+2 开盘用市价单成交；moc_single 改为 T+1 收盘 MOC 二腿
# （SELL+BUY 同一 closing auction 成交，消除跨日风险）。BUY 被拒时降级 LOO
# 由 T+2 开盘拍卖兜底。参考 FIX_PLAN §3 方案 A+ 与 RESEARCH_LOG §13.
#
# Alpaca TimeInForce 映射：
#   CLS = Market-On-Close  (MOC)   收盘拍卖成交
#   OPG = Market/Limit-On-Open (MOO/LOO)  开盘拍卖成交
def build_moc_market_order(symbol: str, qty: int, side: OrderSide,
                           signal_date: str, action: str) -> MarketOrderRequest:
    """T+1 收盘拍卖成交的 MOC 单。BUY / SELL 通用。"""
    client_order_id = f"tq-moc-{_slug_date(signal_date)}-{action.lower()}-{symbol.lower()}"
    return MarketOrderRequest(
        symbol=symbol,
        qty=qty,
        side=side,
        time_in_force=TimeInForce.CLS,
        client_order_id=client_order_id,
    )


def build_loo_limit_order(symbol: str, qty: int, side: OrderSide,
                          limit_price: float, signal_date: str, action: str) -> LimitOrderRequest:
    """MOC 买单被拒时的兜底：T+2 开盘拍卖 LOO 限价单，防止追高过深。

    limit_price 在 execute 阶段设为 T+1 收盘 mid × 1.03 上限保护——不希望次日
    开盘跳空 5% 还追进去。client_order_id 用 -loo- 前缀区分于 MOC 主路径。
    """
    client_order_id = f"tq-loo-{_slug_date(signal_date)}-{action.lower()}-{symbol.lower()}"
    return LimitOrderRequest(
        symbol=symbol,
        qty=qty,
        side=side,
        time_in_force=TimeInForce.OPG,
        limit_price=round(limit_price, 2),
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


def _rebuild_stop_order_exact(symbol: str, qty: int, stop_price: float, signal_date: str) -> StopOrderRequest:
    """按已知的精确 stop_price 直接重建止损单，不经过 reference_price 反推。

    用于卖单提交失败后"恢复"刚被取消的旧止损单——必须保证止损价与被取消前完全一致，
    不能用 build_stop_order() 反推 reference_price 再重算，避免因四舍五入产生哪怕 1 分钱的偏差。
    client_order_id 沿用同一命名规则（同 symbol/日期会与原止损单一致，代理侧按 client_order_id
    去重是幂等的，符合 IDEMPOTENT_DUPLICATE_KEYWORDS 的既有处理逻辑）。
    """
    client_order_id = f"tq-stop-{symbol.lower()}-{_slug_date(signal_date)}"
    return StopOrderRequest(
        symbol=symbol,
        qty=qty,
        side=OrderSide.SELL,
        time_in_force=TimeInForce.GTC,
        stop_price=round(stop_price, 2),
        client_order_id=client_order_id,
    )


_INACTIVE_ORDER_STATUSES = {
    "filled", "canceled", "cancelled", "expired", "replaced",
    "rejected", "done_for_day", "suspended", "stopped",
}


def _order_is_active(order) -> bool:
    """判断订单是否仍处于未完成/挂单状态（而非已取消/已成交/已过期等终态）。

    用于 client_order_id 重复（duplicate）时的二次核实——Broker 报 duplicate 只说明
    这个 ID 曾经被用过，不代表对应订单现在仍然存活；同一个 cid 也可能对应一笔早已
    被取消的旧订单，此时绝不能把"duplicate"误当成"止损仍在保护仓位"。"""
    raw_status = getattr(order, "status", "")
    status = getattr(raw_status, "value", str(raw_status)).lower()
    return status not in _INACTIVE_ORDER_STATUSES


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
        "buy_submitted_with_issues",
        "sell_submitted_with_issues",
        "sell_naked_stop_restore_failed",
        "pending_sell_stale_blocked",
        "cycle_already_pending",
        "halt_retry_stuck_uncertain",
        "last_run_failed",
    }
    return status in emergency


# 这些状态下每个交易日/自然日都会被 cron 高频命中（如节假日），发日报纯属噪音，直接静默跳过。
NO_EMAIL_STATUSES = {"not_a_trading_day"}


def should_escalate_to_error(status: str) -> bool:
    """免打扰状态若在其自身处理逻辑内部再抛异常（如 _save_state 失败），
    不能让 finally 里的 NO_EMAIL_STATUSES 检查把这次真实故障也一起静默掉。"""
    return status in ("started", "ok") or status in NO_EMAIL_STATUSES


def build_email_subject(status: str, run_id: str, paper: bool) -> str:
    mode = "Paper" if paper else "LIVE"
    labels = {
        "ok": "普通日报-运行成功",
        "skipped_rebalance_interval": "普通日报-非调仓日",
        "duplicate_signal_skipped": "普通日报-重复信号跳过",
        "cycle_already_pending": "紧急报警-上一周期未完成",
        "plan_saved": "普通日报-调仓计划已生成",
        "plan_saved_earnings_degraded": "警报-调仓计划已生成（财报避雷未完全生效）",
        "sell_submitted": "普通日报-卖出已提交",
        "sell_submitted_with_issues": "紧急报警-部分卖单提交失败",
        "sell_naked_stop_restore_failed": "紧急报警-止损单撤销后恢复失败-仓位无保护",
        "pending_sell_stale_blocked": "紧急报警-上一轮卖单未核实即被跳过",
        "halt_retry_stuck_uncertain": "紧急报警-LULD重试后买单状态不确定",
        "buy_completed": "普通日报-调仓周期完成",
        "buy_completed_with_issues": "紧急报警-卖单未确认成交",
        "buy_submitted_pending_confirmation": "普通日报-买单已提交待成交核实",
        "buy_submitted_with_issues": "紧急报警-买单提交存在失败",
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


def cancel_stop_orders_for_symbols(client: TradingClient, symbols: list) -> int:
    """卖出前必须先撤销这些标的已有的 GTC 止损单，否则其持有的 qty 会占满
    qty_available，导致卖单被券商拒绝（'insufficient qty available'）。"""
    if not symbols or not ENABLE_STOP_ORDERS:
        return 0
    wanted = {s.lower() for s in symbols}
    cancelled = 0
    try:
        open_orders = client.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN))
    except Exception as e:
        log.warning(f"  查询活跃挂单失败，无法撤销止损单（卖单可能因 qty 被占用而失败）: {e}")
        return 0
    for o in open_orders:
        cid = str(getattr(o, "client_order_id", "") or "")
        sym_o = getattr(o, "symbol", "")
        if sym_o.lower() in wanted and cid.startswith(f"tq-stop-{sym_o.lower()}"):
            try:
                client.cancel_order_by_id(o.id)
                cancelled += 1
                log.info(f"  STOP  {sym_o:8s} 卖出前撤销已有止损单 {cid}")
            except Exception as e:
                log.warning(f"  {sym_o} 撤销止损单失败（卖单可能因 qty 被占用而失败）: {e}")
    return cancelled


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


def _today_et() -> date:
    return datetime.now(ZoneInfo("America/New_York")).date()


def earnings_window_bounds(start_date: date, days_ahead: int) -> tuple[pd.Timestamp, pd.Timestamp]:
    """返回包含执行日及其后 N 个 NYSE 交易日的财报避雷窗口。"""
    cutoff_date = start_date
    for _ in range(max(days_ahead, 0)):
        cutoff_date = next_trading_day(cutoff_date)
    return pd.Timestamp(start_date).normalize(), pd.Timestamp(cutoff_date).normalize()


def get_upcoming_earnings(symbols: list[str], days_ahead: int = 2,
                          details: Optional[dict] = None) -> tuple[set[str], bool]:
    """返回 (blackout, degraded)：
    blackout 为在未来 days_ahead 个交易日内发布财报的股票集合（财报避雷针）；
    degraded 表示本次查询是否有过半标的失败/超时（财报避雷本次可能未完全生效）。
    yfinance 不同版本的 ticker.calendar 返回值结构不一：dict / DataFrame / None。
    单只股票的解析错误/超时均静默跳过，原则：宁可错过一次避雷，不能因 API 异常挂断发单主流程；
    但整体失败率过高时需要在邮件里显式提示，而不是完全静默。
    """
    if days_ahead <= 0:
        if details is not None:
            details["failed_symbols"] = []
        return set(), False
    blackout: set[str] = set()
    today, cutoff = earnings_window_bounds(_today_et(), days_ahead)
    check_syms = [s for s in symbols if s not in ("QQQ", "SPY")]
    failed = 0
    failed_symbols: set[str] = set()
    # 注意：不用 `with ThreadPoolExecutor(...) as pool` —— yf 的网络调用一旦发起无法从
    # 外部中断，若用 with 语句，退出时会阻塞等待所有（含已超时的慢）线程跑完，超时保护形同虚设。
    # 这里改为 shutdown(wait=False)：本函数按全局截止时间及时返回，慢线程留给后台线程池自行
    # 跑完后回收；注意这只是让*本函数*不阻塞——Python 退出时 concurrent.futures 仍会通过
    # atexit 钩子等待所有工作线程结束，真正兜底进程不被挂起的是 run_trader.sh 的 `timeout 600`。
    pool = ThreadPoolExecutor(max_workers=8)
    futures = {}
    try:
        futures = {pool.submit(_fetch_earnings_calendar, sym): sym for sym in check_syms}
        pending = set(futures.keys())
        try:
            # 用 as_completed 设置一个"全局"截止时间，而不是对每个 future 依次等待
            # EARNINGS_LOOKUP_TIMEOUT_SEC——后者在 future 数量超过 max_workers 时，
            # 总等待时间会线性叠加（(N - max_workers) * timeout），完全背离超时保护的初衷。
            for fut in as_completed(futures, timeout=EARNINGS_LOOKUP_TIMEOUT_SEC):
                pending.discard(fut)
                sym = futures[fut]
                try:
                    cal = fut.result()
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
                    failed += 1
                    failed_symbols.add(sym)
                    log.warning(f"  {sym} 财报日历查询失败（跳过，默认放行）: {e}")
        except FutureTimeoutError:
            pass
        finally:
            for fut in pending:
                sym = futures[fut]
                failed += 1
                failed_symbols.add(sym)
                log.warning(f"  {sym} 财报日历查询超时（全局 {EARNINGS_LOOKUP_TIMEOUT_SEC:.0f}s 截止未完成，跳过，默认放行）")
    finally:
        pool.shutdown(wait=False)
    degraded = bool(check_syms) and (failed / len(check_syms)) > 0.5
    if degraded:
        log.warning(f"  ⚠️ 财报避雷本次可能未完全生效：{failed}/{len(check_syms)} 只股票查询失败/超时")
    if details is not None:
        details["failed_symbols"] = sorted(failed_symbols)
    return blackout, degraded


def lookup_upcoming_earnings(symbols: list[str], days_ahead: int) -> tuple[set[str], bool, set[str]]:
    details: dict = {}
    blackout, degraded = get_upcoming_earnings(symbols, days_ahead, details=details)
    return blackout, degraded, set(details.get("failed_symbols", []))


def _append_halt_pending(sym: str, qty: int, ref_price: float, signal_date: str, run_id: str):
    """将被 LULD 熔断拒绝的订单写入重试队列文件。

    ref_price 记录本次买入计划参考价（下单时的目标价），用于后续重试时计算追价上限
    （HALT_RETRY_MAX_CHASE_PCT）——防止无限追价买在过高的位置。
    """
    data: dict = {"pending_date": signal_date, "orders": []}
    if HALT_PENDING_FILE.exists():
        try:
            data = json.loads(HALT_PENDING_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    if data.get("pending_date") != signal_date:
        data = {"pending_date": signal_date, "orders": []}
    if not any(o["symbol"] == sym for o in data["orders"]):
        data["orders"].append({
            "symbol": sym, "qty": qty, "ref_price": ref_price,
            "signal_date": signal_date, "run_id": run_id, "attempts": 0,
        })
    HALT_PENDING_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    log.warning(f"  已写入 halt_pending.json：{sym} × {qty}（参考价 ${ref_price:.2f}）")


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
                    if any(kw in err_msg for kw in IDEMPOTENT_DUPLICATE_KEYWORDS):
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


def _mark_pending_buy_halt_retry(state: Optional[dict], sym: str, client_order_id: str):
    if not state or not state.get(PENDING_BUY_KEY):
        return
    orders = state[PENDING_BUY_KEY].get("orders", [])
    for item in orders:
        if (
            item.get("symbol") == sym
            and item.get("status") in {"halted", "query_error"}
            and not item.get("halt_retry")
        ):
            item["client_order_id"] = client_order_id
            item["status"] = "submitted"
            item["halt_retry"] = True


def _mark_pending_buy_halt_abandoned(state: Optional[dict], sym: str) -> bool:
    """Broker 已明确确认该标的无任何挂单/持仓（404），可以安全放弃本轮买入意图。

    只删除仍处于 halted/query_error 状态（即从未被判定为已提交成功）的条目——
    绝不删除任何已经 submitted/filled 的记录，避免误删真实持仓的核实链路。
    返回 True 表示 pending_buy 里已无其它未解决订单（可视情况完成整个调仓周期）。
    """
    if not state or not state.get(PENDING_BUY_KEY):
        return False
    pending = state[PENDING_BUY_KEY]
    orders = pending.get("orders", [])
    remaining_orders = [
        item for item in orders
        if not (item.get("symbol") == sym and item.get("status") in {"halted", "query_error"})
    ]
    removed = len(remaining_orders) != len(orders)
    pending["orders"] = remaining_orders
    if removed:
        log.warning(
            f"  🗑️ {sym} LULD 重试已达上限且 Broker 确认订单不存在（404），"
            f"放弃本轮买入（未产生持仓 = 无风险敞口，不发送报警邮件）"
        )
    now_empty = not remaining_orders
    if now_empty:
        # 与 reconcile_pending_buy 完成周期时的语义保持一致：pending_buy 里已无任何
        # 未解决订单（既没有仍在等待的，也没有状态不确定的），才能弹出整个 key，
        # 否则 has_incomplete_cycle() 会一直认为周期未完成，永久阻塞下一次 plan。
        state.pop(PENDING_BUY_KEY, None)
        state["last_rebalance"] = str(date.today())
    return now_empty


def _mark_pending_buy_halt_uncertain(state: Optional[dict], sym: str, client_order_id: Optional[str] = None):
    """Broker 状态不确定（订单曾以模糊错误提交过，或查询本身失败/订单确实存在）：
    绝不能静默删除买入意图——保留 pending_buy（继续阻塞下一次 plan 周期），交由人工介入。

    若本轮重试确实尝试过提交（client_order_id 非空），必须把这个最新的 client_order_id
    写回 pending_buy——否则后续人工核实/下一次 reconcile 仍会查询重试前的旧 ID，而
    Broker 可能已经用这个新 ID 成功接单，导致真实持仓核实不到、止损单漏挂。"""
    if not state or not state.get(PENDING_BUY_KEY):
        return
    orders = state[PENDING_BUY_KEY].get("orders", [])
    for item in orders:
        if item.get("symbol") == sym and item.get("status") in {"halted", "query_error"}:
            item["status"] = "halt_retry_stuck_uncertain"
            if client_order_id:
                item["client_order_id"] = client_order_id
    log.error(f"  🚨 {sym} LULD 重试已达上限，但 Broker 订单状态不确定，保留 pending_buy 并需要人工介入")


def _confirm_order_absent(client: TradingClient, client_order_id: str) -> bool:
    """仅当明确判定为 404/not found 时才返回 True（确认订单不存在）。
    查询异常但不是明确的 404、或订单确实存在（任意状态），一律保守返回 False（不确定）——
    绝不能把"查询失败"和"确认不存在"混为一谈，否则可能误删一笔实际已提交成功的买单记录。"""
    try:
        client.get_order_by_client_id(client_order_id)
        return False
    except Exception as e:
        err_msg = str(e).lower()
        return "404" in err_msg or "not found" in err_msg


def retry_halted_orders(dry_run: bool, state: Optional[dict] = None, run_id: Optional[str] = None) -> dict:
    """重试因 LULD 熔断被拒的买入单：每 HALT_RETRY_INTERVAL_SEC 秒一次，
    最多 HALT_RETRY_MAX_ATTEMPTS 次（约 HALT_RETRY_MAX_ATTEMPTS*HALT_RETRY_INTERVAL_SEC/60 分钟），
    到 HALT_RETRY_UNTIL_HOUR_ET 时（ET）也会提前结束——两个上限谁先到都会停止本轮重试。

    买入侧“保守”策略：追价不超过 HALT_RETRY_MAX_CHASE_PCT（相对原计划参考价），超过则
    本轮不提交（继续等待，不算失败）；重试次数耗尽后，只有 Broker 明确确认订单不存在（404）
    才会放弃该笔买入意图（不产生持仓 = 无风险敞口，不报警）；只要 Broker 状态不确定
    （包括曾出现过模糊的提交异常），一律保留 pending_buy 并交由 --retry-halted 之外的
    上层调用发送报警邮件，绝不允许在不确定的情况下静默把买入意图删除。
    """
    summary = {
        "had_orders": False,
        "resolved_symbols": [],
        "chase_capped_symbols": [],
        "confirmed_absent_symbols": [],
        "uncertain_symbols": [],
        "attempts_used": 0,
    }
    if not HALT_PENDING_FILE.exists():
        log.info("未找到 halt_pending.json，无需重试，退出")
        return summary
    try:
        data = json.loads(HALT_PENDING_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        log.error(f"读取 halt_pending.json 失败: {e}")
        return summary
    orders = data.get("orders", [])
    if not orders:
        log.info("重试队列为空，退出")
        HALT_PENDING_FILE.unlink(missing_ok=True)
        return summary

    summary["had_orders"] = True
    if run_id is None:
        run_id = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    log.info(f"LULD 重试模式：发现 {len(orders)} 笔挂单 → {[o['symbol'] for o in orders]}")
    client = TradingClient(API_KEY, SECRET_KEY, paper=PAPER)
    data_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)

    # 每个 order dict 额外维护三个仅本函数内部使用的状态字段：
    #   _attempted_cid  最近一次实际尝试提交时用的 client_order_id（用于事后核对 Broker 状态）
    #   _uncertain      是否曾经出现过"不清楚订单是否已创建"的模糊异常（一旦为 True 永不清除）
    #   _chase_capped   本轮是否因追价超过上限而跳过提交（仅用于展示/统计，不影响是否继续重试）
    remaining = [dict(o, _attempted_cid=None, _uncertain=False, _chase_capped=False) for o in orders]
    attempt = 0
    while remaining:
        et_hour = _now_hour_et()
        if et_hour >= HALT_RETRY_UNTIL_HOUR_ET:
            log.warning(f"  已到 {HALT_RETRY_UNTIL_HOUR_ET}:00 ET，停止重试循环（{len(remaining)} 笔待终态判定）")
            break
        if attempt >= HALT_RETRY_MAX_ATTEMPTS:
            log.warning(f"  已达最大重试次数 {HALT_RETRY_MAX_ATTEMPTS}，停止重试循环（{len(remaining)} 笔待终态判定）")
            break
        attempt += 1
        log.info(f"  第 {attempt}/{HALT_RETRY_MAX_ATTEMPTS} 次重试（{len(remaining)} 笔）...")
        resolved_before = len(summary["resolved_symbols"])
        still_pending = []
        for o in remaining:
            sym = o["symbol"]
            qty = o["qty"]
            ref_price = float(o.get("ref_price", 0) or 0)
            o["_chase_capped"] = False
            try:
                quote_req = StockLatestQuoteRequest(symbol_or_symbols=[sym])
                quotes = data_client.get_stock_latest_quote(quote_req)
            except Exception as e:
                log.warning(f"    {sym} 报价查询失败，跳过本轮: {e}")
                still_pending.append(o)
                continue
            ask = float(getattr(quotes[sym], "ask_price", 0) or 0) if sym in quotes else 0.0
            if ask <= 0:
                log.warning(f"    {sym} 卖一价为 0，跳过本轮")
                still_pending.append(o)
                continue

            if ref_price > 0:
                chase_cap_price = round(ref_price * (1 + HALT_RETRY_MAX_CHASE_PCT), 2)
                if ask > chase_cap_price:
                    log.warning(
                        f"    ⚠️ {sym} 当前卖一价 ${ask:.2f} 已超过追价上限 ${chase_cap_price:.2f}"
                        f"（参考价 ${ref_price:.2f} + {HALT_RETRY_MAX_CHASE_PCT:.0%}），本轮不追价提交"
                    )
                    o["_chase_capped"] = True
                    still_pending.append(o)
                    continue

            # 用卖一价（ask）而非买一价（bid）：买单要吃卖方挂单才能成交，用 bid 会低于
            # 当前最优卖价，几乎不可能成交（此前版本用 bid*0.999 是买卖方向搞反的错误）。
            limit_price = round(ask * 1.001, 2)
            cid = f"tq-retry-{_slug_date(o.get('signal_date', data.get('pending_date', '')))}-{sym.lower()}"
            limit_req = LimitOrderRequest(
                symbol=sym,
                qty=qty,
                side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY,
                limit_price=limit_price,
                client_order_id=cid,
            )
            log.info(f"    RETRY BUY {sym} × {qty} @ ${limit_price:.2f}（ask=${ask:.2f}）")
            o["_attempted_cid"] = cid
            try:
                if not dry_run:
                    client.submit_order(limit_req)
                    _mark_pending_buy_halt_retry(state, sym, cid)
                log.info(f"    ✅ {sym} 重试订单已提交")
                summary["resolved_symbols"].append(sym)
            except Exception as e:
                err_msg = str(e).lower()
                if any(kw in err_msg for kw in IDEMPOTENT_DUPLICATE_KEYWORDS):
                    log.info(f"    ✅ {sym} 重试订单此前已提交（client_order_id 重复，视为成功）")
                    if not dry_run:
                        _mark_pending_buy_halt_retry(state, sym, cid)
                    summary["resolved_symbols"].append(sym)
                elif any(kw in err_msg for kw in ("halt", "not_tradable", "suspended", "asset_not_tradable")):
                    log.warning(f"    ⚠️ {sym} 仍停牌/熔断，{HALT_RETRY_INTERVAL_SEC}s 后继续重试")
                    still_pending.append(o)
                else:
                    # 非幂等、非熔断的模糊异常（如网络超时）：无法确认订单是否已被 Broker 创建，
                    # 之前版本这里既不重试也不记录，会让该标的从队列里"静默消失"且被当作已解决——
                    # 必须继续留在 still_pending 里，并标记为不确定，供耗尽后走人工报警分支。
                    log.error(f"    ❌ {sym} 重试提交异常（无法确认是否已创建订单）: {e}")
                    o["_uncertain"] = True
                    still_pending.append(o)
        remaining = still_pending
        if remaining:
            if not dry_run and state is not None and len(summary["resolved_symbols"]) > resolved_before:
                # 只有在"循环还会因为其它未解决标的继续等待"时才在此提前核实+补挂止损——
                # 重试单是限价单且价格已追到卖一价之上，本轮已解决的标的很可能已经成交，
                # 不该干等到整个循环结束（其它标的还要再等最多 30 分钟）才补上止损保护。
                # 但如果本轮之后 remaining 已空（即全部解决，函数即将正常返回），则不在
                # 这里做零延迟核实：刚提交的订单可能还没被 Broker 一侧完全处理好，此时
                # 查询容易误判为 query_error 把刚写好的 submitted 状态又冲掉（回归用例
                # test_halt_retry_updates_pending_buy_client_order_id 覆盖此场景），这种
                # 情况留给下一次 --retry-halted 或其它常规核对流程处理即可。
                try:
                    reconcile_pending_buy(client, state, run_id, dry_run=False)
                    _save_state(state)
                except Exception as e:
                    log.warning(f"  ⚠️ 本轮 LULD 重试成交后核实失败，将在下一轮/下次 --retry-halted 补核实: {e}")
            if attempt < HALT_RETRY_MAX_ATTEMPTS:
                # 若本轮已经是最后一次允许的重试（attempt 已达上限），下一次循环开头就会
                # 直接 break，没有必要在这里白等 HALT_RETRY_INTERVAL_SEC 秒——之前版本
                # 不看这个条件，会在放弃前多睡一次完全无意义的 5 分钟。
                log.info(f"  {len(remaining)} 笔仍在等待，{HALT_RETRY_INTERVAL_SEC}s 后重试...")
                if not dry_run:
                    time.sleep(HALT_RETRY_INTERVAL_SEC)
    summary["attempts_used"] = attempt

    if not remaining:
        log.info("  所有挂单已成功处理，清除 halt_pending.json")
        HALT_PENDING_FILE.unlink(missing_ok=True)
        return summary

    log.warning(f"  {len(remaining)} 笔重试耗尽，逐笔核对 Broker 订单状态后判定终态...")
    if dry_run:
        # dry-run 不做任何 state 变更/真实查询，仅报告仍待处理的标的，避免误导性地
        # 打印"已放弃"却没有真的验证过 Broker 状态。
        for o in remaining:
            (summary["uncertain_symbols"] if o["_uncertain"] else summary["chase_capped_symbols"]).append(o["symbol"])
        log.info(f"  [DRY RUN] {len(remaining)} 笔待终态判定，实际运行时将核对 Broker 状态后放弃/报警")
        return summary

    any_finalized_empty = False
    for o in remaining:
        sym = o["symbol"]
        cid = o["_attempted_cid"] or f"tq-retry-{_slug_date(o.get('signal_date', data.get('pending_date', '')))}-{sym.lower()}"
        if o["_uncertain"]:
            summary["uncertain_symbols"].append(sym)
            _mark_pending_buy_halt_uncertain(state, sym, o["_attempted_cid"])
            continue
        if o["_chase_capped"] and o["_attempted_cid"] is None:
            # 最后一轮仍是追价超上限而跳过提交——从未真正下过单，谈不上要向 Broker
            # 核实"是否存在"，直接归类为追价超限、保留 pending_buy 供下次人工/次日处理。
            summary["chase_capped_symbols"].append(sym)
            continue
        if _confirm_order_absent(client, cid):
            summary["confirmed_absent_symbols"].append(sym)
            if _mark_pending_buy_halt_abandoned(state, sym):
                any_finalized_empty = True
        else:
            # Broker 查询本身失败（非明确 404）或订单其实存在：都不能判定为"确认不存在"。
            summary["uncertain_symbols"].append(sym)
            _mark_pending_buy_halt_uncertain(state, sym, o["_attempted_cid"])

    if any_finalized_empty and state is not None and not state.get(PENDING_BUY_KEY):
        # 本轮所有 pending_buy 条目都已解决（放弃的已删除、之前已成交的早被 reconcile 移除），
        # 但可能还有其它标的这次买入确实成交了——按 reconcile_pending_buy 完成周期时的同等逻辑，
        # 用真实持仓补挂止损单，避免这些已建仓标的因为周期在这里收尾而漏挂保护。
        try:
            fresh_positions = client.get_all_positions()
            signal_date = data.get("pending_date") or str(date.today())
            stops = ensure_stop_orders_for_positions(client, fresh_positions, signal_date, dry_run)
            log.info(f"  周期在 LULD 重试终态判定后完成，补挂止损单 {stops} 笔")
        except Exception as e:
            log.warning(f"  ⚠️ 周期完成后补挂止损单失败: {e}")

    HALT_PENDING_FILE.unlink(missing_ok=True)
    if summary["confirmed_absent_symbols"]:
        log.warning(f"  已放弃（Broker 确认不存在，不报警）：{summary['confirmed_absent_symbols']}")
    if summary["uncertain_symbols"]:
        log.error(f"  🚨 状态不确定，保留 pending_buy 待人工介入：{summary['uncertain_symbols']}")
    return summary


def _money(value) -> str:
    try:
        return f"${float(value):,.2f}"
    except (TypeError, ValueError):
        return "$0.00"


def sell_earnings_email_lines(summary: dict) -> list[str]:
    lines = []
    forced = summary.get("earnings_forced_close") or []
    if forced:
        lines.append(f"- 📅 财报复核新增强制清仓: {forced}")
    failed = summary.get("earnings_recheck_failed_symbols") or []
    if failed:
        lines.append(f"- ⚠️ 财报日历查询失败并默认放行: {failed}")
    if summary.get("earnings_recheck_degraded"):
        lines.append("- ⚠️ 财报日历查询降级（本轮未能完整复核，请留意后续手动核查）")
    return lines


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


# ── 调仓状态机 ─────────────────────────────────────────────────────────────
# 两条路径共存，由 EXEC_MODE 切换（详见文件顶部）：
#   three_phase：plan(T 16:05) → sell(T+1 15:50) → buy(T+2 09:15)
#   moc_single ：plan(T 16:05) → execute(T+1 15:35 MOC 二腿) → reconcile(T+2 09:45)
CYCLE_PLAN_KEY      = "cycle_plan"       # phase=plan 写入，被 sell 或 execute 消费
PENDING_SELL_KEY    = "pending_sell"     # three_phase 专用：sell → buy 之间
PENDING_BUY_KEY     = "pending_buy"      # three_phase 专用：buy 提交后待成交核实
PENDING_EXECUTE_KEY = "pending_execute"  # moc_single 专用：execute 提交后待 reconcile


def has_incomplete_cycle(state: dict) -> bool:
    keys = (CYCLE_PLAN_KEY, PENDING_SELL_KEY, PENDING_BUY_KEY, PENDING_EXECUTE_KEY)
    return any(state.get(key) for key in keys)


def buy_phase_status(summary: dict) -> str:
    if summary.get("buy_pending"):
        if summary.get("submit_failed") or summary.get("had_fill_issues"):
            return "buy_submitted_with_issues"
        return "buy_submitted_pending_confirmation"
    return "buy_completed_with_issues" if summary.get("had_fill_issues") else "buy_completed"


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
    earnings_failed_symbols: set = set()
    if EARNINGS_BLACKOUT_DAYS > 0:
        check_syms = list((target_set | set(current_map.keys())) - {"QQQ", "SPY"})
        earnings_blackout, earnings_degraded, earnings_failed_symbols = lookup_upcoming_earnings(
            check_syms, EARNINGS_BLACKOUT_DAYS
        )
        if earnings_allow:
            overridden = earnings_blackout & earnings_allow
            if overridden:
                log.warning(f"  ⚠️ 手动豁免财报避雷：{sorted(overridden)}（人工确认不强制出场，风险自负）")
                earnings_blackout -= earnings_allow
        if earnings_blackout:
            log.warning(f"  📅 财报避雷命中：{sorted(earnings_blackout)} 移出买入计划并强制出场")
            target_set -= earnings_blackout

    target_snapshot: dict[str, dict[str, float]] = {}
    for sym in target_syms:
        if sym not in target_set:
            continue
        if sym not in close.columns:
            log.warning(f"  ⚠️  {sym} 无价格数据，跳过")
            continue
        sym_closes = close[sym].dropna()
        if sym_closes.empty:
            log.warning(f"  ⚠️  {sym} 无有效价格数据，跳过")
            continue
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
        target_snapshot[sym] = {"price": round(price, 4)}

    investable = equity * (1 - MIN_CASH_BUFFER_PCT)
    target_n = len(target_snapshot)
    target_val = min(investable / target_n, equity * MAX_POSITION_PCT) if target_n else 0.0
    close_all, trim, buy = [], [], []

    for sym in current_map:
        if sym not in target_set or sym in earnings_blackout:
            mv  = float(getattr(current_map[sym], "market_value", 0) or 0)
            qty = int(float(getattr(current_map[sym], "qty", 0) or 0))
            if qty > 0:
                close_all.append({"symbol": sym, "qty": qty, "market_value": round(mv, 2)})

    for sym in target_syms:
        if sym not in target_snapshot:
            continue
        price = target_snapshot[sym]["price"]
        current_qty = int(float(getattr(current_map[sym], "qty", 0) or 0)) if sym in current_map else 0
        target_qty  = int(target_val / (price * 1.05))
        if target_qty <= 0 and current_qty == 0:
            log.warning(
                f"  ⚠️  {sym} 单股价格 ${price:,.2f} 超过等权目标金额 ${target_val:,.2f}，"
                f"买入 1 股会超配，跳过本次建仓"
            )
            continue
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
        "earnings_failed_symbols": sorted(earnings_failed_symbols),
        "target_n": target_n,
        "target_snapshot": target_snapshot,
    }


def execute_sell_phase(client: TradingClient, state: dict, run_id: str, dry_run: bool,
                        audit: Optional[AuditWriter] = None,
                        earnings_allow: Optional[set] = None) -> dict:
    """
    phase=sell（T+1 尾盘前运行）：读取 cycle_plan，对 close_all/trim 提交限价卖单
    （吃买一价，确保收盘前迅速成交），写入 pending_sell，清空 cycle_plan。
    """
    plan = state.get(CYCLE_PLAN_KEY)
    summary = {"had_plan": bool(plan), "orders": [], "buy_carry": [], "note": "", "blocked_stale_pending_sell": False}
    if not plan:
        log.info("  cycle_plan 为空，今日无待卖出计划。")
        return summary

    if state.get(PENDING_SELL_KEY):
        # 上一轮 sell 阶段提交的卖单尚未被 buy 阶段核实/消费（大概率是 buy 阶段漏跑或崩溃），
        # 此时若继续提交本轮卖单，下面对 PENDING_SELL_KEY 的无条件覆盖会让上一轮已成交卖单的
        # 核实指针和 buy_carry 永久丢失。宁可本轮不提交、报警等待人工介入，也不要静默覆盖。
        summary["blocked_stale_pending_sell"] = True
        log.error("  ❌ 检测到未被 buy 阶段消费的 pending_sell，为避免覆盖丢失，本轮跳过卖出提交，"
                   "请人工检查 buy 阶段是否遗漏运行")
        return summary

    today     = date.today()
    plan_date = date.fromisoformat(plan["plan_date"])
    expected  = next_trading_day(plan_date)
    if today != expected:
        summary["note"] = f"补跑：计划日为 {plan_date}，理应 {expected} 执行，实际 {today} 执行"
        log.warning(f"  ⚠️ {summary['note']}")

    sell_targets = [(c["symbol"], c["qty"], "close") for c in plan.get("close_all", [])] + \
                   [(t["symbol"], t["qty"], "trim") for t in plan.get("trim", [])]

    # ── 财报避雷复核：plan 阶段判断的避雷名单可能因跨日延迟而过期，这里对"计划仍继续
    # 持有/减仓/加仓"的标的重新查一次财报日历。命中的标的一律升级为全额强制清仓
    # （与 compute_rebalance_plan 里"命中避雷即移出 target_set"的语义保持一致，
    # 不因为它原本只是 trim 就放过），并从 buy_carry 中剔除对应的加仓/新建计划。
    earnings_forced_syms: set = set()
    earnings_recheck_degraded = False
    earnings_recheck_failed_symbols: set = set()
    current_map: dict = {}
    if EARNINGS_BLACKOUT_DAYS > 0:
        positions = client.get_all_positions()
        current_map = {p.symbol: p for p in positions}
        already_closing = {sym for sym, _, kind in sell_targets if kind == "close"}
        watch_syms = list((set(current_map.keys()) - already_closing) - {"QQQ", "SPY"})
        if watch_syms:
            new_blackout, earnings_recheck_degraded, earnings_recheck_failed_symbols = lookup_upcoming_earnings(
                watch_syms, EARNINGS_BLACKOUT_DAYS
            )
            if earnings_allow:
                overridden = new_blackout & earnings_allow
                if overridden:
                    log.warning(f"  ⚠️ sell 阶段手动豁免财报避雷：{sorted(overridden)}")
                    new_blackout -= earnings_allow
            for sym in new_blackout:
                qty = int(float(getattr(current_map[sym], "qty", 0) or 0))
                if qty > 0:
                    earnings_forced_syms.add(sym)
            if earnings_forced_syms:
                log.warning(
                    f"  📅 sell 阶段财报复核命中：{sorted(earnings_forced_syms)}，"
                    f"改为强制清仓（覆盖原 HOLD/trim/加仓计划）"
                )

    if earnings_forced_syms:
        sell_targets = [(sym, qty, kind) for sym, qty, kind in sell_targets if sym not in earnings_forced_syms]
        sell_targets += [
            (sym, int(float(getattr(current_map[sym], "qty", 0) or 0)), "close")
            for sym in earnings_forced_syms
        ]

    # ── F1 修复：sell 提交前用实时持仓夹取 qty，杜绝裸空 ─────────────────────────
    # plan 阶段（T 16:05）生成的 sell qty 是 T 日收盘持仓；T+1 盘中 GTC 止损单可能已
    # 触发把仓位清零。此时若照原 qty 提交卖单，保证金账户会直接受理为开空——策略层
    # 完全不知情的裸空头。这里刷新持仓，把每笔 qty 夹取到 min(plan_qty, 实际可用)。
    # 为 0 则跳过并记 stop_triggered_syms，供审计与后续买入阶段负持仓核查参考。
    if not dry_run and sell_targets:
        try:
            live_positions = client.get_all_positions()
        except Exception as e:
            log.warning(f"  ⚠️ 拉取实时持仓失败，本轮 sell 无法核对可用量（回退到 plan qty）: {e}")
            live_positions = None
    else:
        live_positions = None

    stop_triggered_syms: list[str] = []
    if live_positions is not None:
        live_qty_map = {p.symbol: int(float(getattr(p, "qty_available", getattr(p, "qty", 0)) or 0))
                        for p in live_positions}
        adjusted: list[tuple] = []
        for sym, qty, kind in sell_targets:
            available = live_qty_map.get(sym, 0)
            if available <= 0:
                # 仓位在 T+1 盘中已被止损单/其他机制清零 —— 绝不能再提交卖单
                stop_triggered_syms.append(sym)
                log.warning(
                    f"  ⚠️ {sym} 实时可用持仓为 0（可能 T+1 盘中止损单已成交），"
                    f"跳过卖单提交，避免开出裸空头"
                )
                continue
            safe_qty = min(int(qty), available)
            if safe_qty < int(qty):
                log.warning(
                    f"  ⚠️ {sym} plan qty={qty} > 实时可用 {available}，"
                    f"夹取到 {safe_qty}（可能已部分被止损单占用/成交）"
                )
            adjusted.append((sym, safe_qty, kind))
        sell_targets = adjusted
    if stop_triggered_syms:
        summary["stop_triggered_syms"] = stop_triggered_syms

    buy_carry = [b for b in plan.get("buy", []) if b["symbol"] not in earnings_forced_syms]
    plan_target_snapshot = plan.get("target_snapshot")
    if plan_target_snapshot is not None:
        target_snapshot = {
            sym: data for sym, data in plan_target_snapshot.items()
            if sym not in earnings_forced_syms
        }
        reallocation_required = len(target_snapshot) != len(plan_target_snapshot)
    else:
        target_snapshot = None
        reallocation_required = False

    # 逐标的"临下单前才撤止损单"，而不是像之前那样对整批 sell_targets 一次性批量撤销。
    # 批量撤销会制造"裸露窗口"：假设一批要卖的标的里，前几个的止损单已经全部撤销，
    # 但排在后面的某个标的卖单提交失败——它的止损保护已经没了，却没有任何补救。
    # 这里先一次性查询出每个标的对应的止损单（含止损价），撤销延后到每个标的自己的
    # 提交前一刻才做；一旦该标的卖单提交失败，立即尝试用原止损价把止损单恢复回去。
    stop_by_symbol: dict = {}
    if not dry_run and ENABLE_STOP_ORDERS and sell_targets:
        wanted = {sym.lower() for sym, _, _ in sell_targets}
        try:
            open_orders = client.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN))
            for o in open_orders:
                cid = str(getattr(o, "client_order_id", "") or "")
                sym_o = getattr(o, "symbol", "")
                if sym_o.lower() in wanted and cid.startswith(f"tq-stop-{sym_o.lower()}"):
                    stop_by_symbol[sym_o] = o
        except Exception as e:
            log.warning(f"  查询活跃挂单失败，无法逐笔撤销止损单（卖单可能因 qty 被占用而失败）: {e}")

    data_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)
    orders_out = []
    submit_failed = []  # 非幂等原因导致提交失败的标的，需写回 cycle_plan 供下一轮重试，不能静默丢弃
    naked_positions = []  # 止损单已撤销但卖单提交失败、且恢复止损单也失败的标的：当前完全无保护，需最高优先级人工介入
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
            # client_order_id 用 plan_date（而非 signal_date）生成：重试计划的 plan_date
            # 会是重新写入当天的日期，与首次尝试的 plan_date 不同，避免同一 signal_date
            # 反复重试时 client_order_id 撞车、被券商当作重复提交而实际未真正下单。
            order_req = build_market_order(sym, qty, OrderSide.SELL, plan["plan_date"], kind)
            limit_txt = "market"
        else:
            limit_price = round(bid, 2)
            client_order_id = f"tq-{_slug_date(plan['plan_date'])}-{kind}-{sym.lower()}"
            order_req = LimitOrderRequest(
                symbol=sym, qty=qty, side=OrderSide.SELL,
                time_in_force=TimeInForce.DAY, limit_price=limit_price,
                client_order_id=client_order_id,
            )
            limit_txt = f"${limit_price:.2f}"
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
            # Alpaca 的 qty_available 预留机制：只要该标的还有一张活跃的 GTC 止损单占着这些股数，
            # 新的卖单就会被拒绝，所以撤销止损单必须紧贴在提交卖单之前做（而不是整批提前撤），
            # 尽量缩短"止损单已撤、卖单还没受理"这段无保护窗口。
            existing_stop = stop_by_symbol.get(sym)
            stop_cancelled = False
            if existing_stop is not None:
                try:
                    client.cancel_order_by_id(existing_stop.id)
                    stop_cancelled = True
                    log.info(f"  STOP  {sym:8s} 卖出前撤销已有止损单 {existing_stop.client_order_id}")
                except Exception as e:
                    log.warning(f"  {sym} 撤销止损单失败（卖单可能因 qty 被占用而失败）: {e}")
            try:
                client.submit_order(order_req)
            except Exception as e:
                err_msg = str(e).lower()
                if any(kw in err_msg for kw in IDEMPOTENT_DUPLICATE_KEYWORDS):
                    log.info(f"    ✅ {sym} 卖单此前已提交（client_order_id 重复，视为成功）")
                else:
                    log.error(f"    ❌ 卖出失败 {sym}: {e}")
                    submit_failed.append({"symbol": sym, "qty": qty, "kind": kind})
                    if stop_cancelled:
                        # 卖单没提交成功，但止损单已经被撤了——这个仓位现在完全裸露，
                        # 必须立即尝试用原止损价把止损单恢复回去，而不是等下一轮 sell 阶段。
                        stop_qty = int(float(getattr(existing_stop, "qty", 0) or qty))
                        restore_req = _rebuild_stop_order_exact(
                            sym, stop_qty, float(getattr(existing_stop, "stop_price", 0) or 0),
                            plan["plan_date"],
                        )
                        try:
                            client.submit_order(restore_req)
                            log.warning(f"    ⚠️ {sym} 卖单提交失败，已恢复原止损单 @ ${restore_req.stop_price:.2f}")
                        except Exception as re_err:
                            re_msg = str(re_err).lower()
                            if any(kw in re_msg for kw in IDEMPOTENT_DUPLICATE_KEYWORDS):
                                # duplicate 只说明这个 client_order_id 曾被用过，不代表对应订单
                                # 现在仍是活跃止损——同一个 cid 也可能属于一笔已被取消的旧订单
                                # （例如前一天已经走过一次"撤销→恢复→次日再撤销"）。必须查询该
                                # cid 的当前状态，确认仍处于未终态才能当作"仍受保护"，否则一律
                                # 按无保护处理并报警，绝不能凭 duplicate 就默认安全。
                                try:
                                    restored_order = client.get_order_by_client_id(restore_req.client_order_id)
                                except Exception as verify_err:
                                    log.critical(
                                        f"    🚨 {sym} 止损单恢复 client_order_id 重复，但核实订单状态失败，"
                                        f"无法确认是否仍受保护，按无保护处理: {verify_err}"
                                    )
                                    naked_positions.append({
                                        "symbol": sym, "qty": stop_qty,
                                        "error": f"duplicate cid，核实状态失败: {verify_err}",
                                    })
                                else:
                                    if _order_is_active(restored_order):
                                        log.info(f"    ✅ {sym} 止损单恢复：client_order_id 重复，已核实订单仍处于活跃状态")
                                    else:
                                        status = getattr(
                                            getattr(restored_order, "status", ""), "value",
                                            str(getattr(restored_order, "status", "")),
                                        )
                                        log.critical(
                                            f"    🚨 {sym} 止损单恢复 client_order_id 重复，但核实后订单已是终态"
                                            f"（status={status}），当前完全无保护"
                                        )
                                        naked_positions.append({
                                            "symbol": sym, "qty": stop_qty,
                                            "error": f"duplicate cid 但订单已是终态 status={status}",
                                        })
                            else:
                                log.critical(
                                    f"    🚨 {sym} 卖单提交失败且止损单恢复也失败，当前完全无保护: {re_err}"
                                )
                                naked_positions.append({"symbol": sym, "qty": stop_qty, "error": str(re_err)})
                    continue
        orders_out.append({"symbol": sym, "qty": qty, "client_order_id": order_req.client_order_id, "kind": kind})

    summary["orders"]        = orders_out
    summary["buy_carry"]     = buy_carry
    summary["all_failed"]    = bool(sell_targets) and not orders_out
    summary["naked_positions"] = naked_positions
    summary["submit_failed"] = submit_failed
    summary["earnings_forced_close"] = sorted(earnings_forced_syms)
    summary["earnings_recheck_degraded"] = earnings_recheck_degraded
    summary["earnings_recheck_failed_symbols"] = sorted(earnings_recheck_failed_symbols)

    if not dry_run:
        if summary["all_failed"]:
            log.error("  ❌ 卖出阶段全部提交失败，保留 cycle_plan 以便下次重试，不推进周期")
        else:
            plan_target_n = plan.get("target_n")
            pending_target_n = (
                len(target_snapshot)
                if target_snapshot is not None
                else max(plan_target_n - len(earnings_forced_syms), 0)
                if plan_target_n is not None
                else None
            )
            state[PENDING_SELL_KEY] = {
                "sell_date": str(today),
                "signal_date": plan["signal_date"],
                "orders": orders_out,
                "buy_carry": buy_carry,
                "target_n": pending_target_n,
                "earnings_forced_close": sorted(earnings_forced_syms),
                "target_snapshot": target_snapshot,
                "reallocation_required": reallocation_required,
            }
            if submit_failed:
                # 部分标的提交失败但另一部分成功：不能整体判定 all_failed，也不能让失败标的
                # 随 cycle_plan 清空而永久消失——写回一份只含失败标的的新 cycle_plan，
                # plan_date 用今天（避免 client_order_id 与本次已失败的提交撞车），
                # 下一轮 sell 阶段会自动重新尝试这些标的的卖出。
                retry_close = [{"symbol": f["symbol"], "qty": f["qty"]} for f in submit_failed if f["kind"] == "close"]
                retry_trim  = [{"symbol": f["symbol"], "qty": f["qty"]} for f in submit_failed if f["kind"] != "close"]
                state[CYCLE_PLAN_KEY] = {
                    "plan_date": str(today),
                    "signal_date": plan["signal_date"],
                    "close_all": retry_close,
                    "trim": retry_trim,
                    "buy": [],
                }
                log.warning(
                    f"  ⚠️ {len(submit_failed)} 笔卖单提交失败，已写回 cycle_plan 供下一轮重试："
                    f"close={[f['symbol'] for f in retry_close]} trim={[f['symbol'] for f in retry_trim]}"
                )
            else:
                state.pop(CYCLE_PLAN_KEY, None)
    return summary


# ══════════════════════════════════════════════════════════════════════════════
# MOC 单段执行路径（EXEC_MODE=moc_single）
# ══════════════════════════════════════════════════════════════════════════════

def execute_moc_phase(client: TradingClient, state: dict, run_id: str, dry_run: bool,
                      audit: Optional[AuditWriter] = None,
                      earnings_allow: Optional[set] = None) -> dict:
    """
    phase=execute（T+1 15:35 ET 运行）：读取 cycle_plan，一次性提交
    SELL_MOC + BUY_MOC 二腿，让 16:00 收盘拍卖同一价印成交。
    BUY 侧 MOC 被 broker 拒单时，同步降级为 T+2 开盘 LOO 兜底
    （限价 = T+1 收盘 mid × (1 + LOO_FALLBACK_MAX_CHASE_PCT)），
    防止裸卖不裸买的资金空置。

    关键差异 vs 原 execute_sell_phase + execute_buy_phase：
      - 卖买同一时刻决策与提交，无 T+1→T+2 跨夜资金/风险空窗
      - MOC 拍卖近乎保证成交（audit F1/H3/H4 高危攻击面消失）
      - 状态机由 3 段坍缩为 1 段，崩溃恢复语义简单：pending_execute 是
        单一唯一的 in-flight 快照，reconcile 阶段做终态收拢

    资金来源合规：Alpaca cash 账户支持"当日卖出金额进入 unsettled_proceeds
    可当日再用于买入"。提交顺序必须先卖后买，MOC 15:50 硬截止前完成。
    """
    plan = state.get(CYCLE_PLAN_KEY)
    summary = {
        "had_plan": bool(plan), "sell_orders": [], "buy_orders": [],
        "loo_fallback": [], "submit_failed": [], "note": "",
        "blocked_stale_pending": False,
        "earnings_forced_syms": [], "stop_triggered_syms": [],
    }
    if not plan:
        log.info("  cycle_plan 为空，今日无待执行调仓。")
        return summary

    if state.get(PENDING_EXECUTE_KEY):
        summary["blocked_stale_pending"] = True
        log.error("  ❌ 检测到未被 reconcile 消费的 pending_execute，"
                  "为避免覆盖丢单，本轮跳过执行，请人工检查 reconcile 是否遗漏运行")
        return summary
    if state.get(PENDING_SELL_KEY) or state.get(PENDING_BUY_KEY):
        summary["blocked_stale_pending"] = True
        log.error("  ❌ 检测到旧 three_phase 路径的残留 pending_sell/pending_buy，"
                  "无法在 moc_single 模式下继续，请人工核对 broker 侧持仓与挂单后清理")
        return summary

    today     = date.today()
    plan_date = date.fromisoformat(plan["plan_date"])
    expected  = next_trading_day(plan_date)
    if today != expected:
        summary["note"] = f"补跑：计划日 {plan_date}，理应 {expected} 执行，实际 {today}"
        log.warning(f"  ⚠️ {summary['note']}")

    # ── 财报避雷复核（与原 sell 阶段相同语义）────────────────────────────
    positions = client.get_all_positions()
    current_map = {p.symbol: p for p in positions}
    already_closing = {c["symbol"] for c in plan.get("close_all", [])}
    earnings_forced_syms: set = set()
    if EARNINGS_BLACKOUT_DAYS > 0:
        watch_syms = list((set(current_map.keys()) - already_closing) - {"QQQ", "SPY"})
        if watch_syms:
            new_blackout, _, _ = lookup_upcoming_earnings(watch_syms, EARNINGS_BLACKOUT_DAYS)
            if earnings_allow:
                new_blackout -= earnings_allow
            for sym in new_blackout:
                qty = int(float(getattr(current_map[sym], "qty", 0) or 0))
                if qty > 0:
                    earnings_forced_syms.add(sym)
            if earnings_forced_syms:
                log.warning(f"  📅 财报复核命中，强制清仓：{sorted(earnings_forced_syms)}")
    summary["earnings_forced_syms"] = sorted(earnings_forced_syms)

    # 汇总卖单列表：close_all + trim + 财报强制清仓（覆盖计划中的 trim/HOLD）
    sell_targets: list = []
    for c in plan.get("close_all", []):
        sell_targets.append((c["symbol"], int(c["qty"]), "close"))
    for t in plan.get("trim", []):
        if t["symbol"] not in earnings_forced_syms:
            sell_targets.append((t["symbol"], int(t["qty"]), "trim"))
    for sym in earnings_forced_syms:
        if sym in already_closing:
            continue
        qty = int(float(getattr(current_map[sym], "qty", 0) or 0))
        if qty > 0:
            sell_targets.append((sym, qty, "close"))

    # 买入列表：剔除财报命中标的
    buy_carry = [b for b in plan.get("buy", []) if b["symbol"] not in earnings_forced_syms]

    # ── F1 修复的等效逻辑：实时持仓夹取 qty ────────────────────────────────
    live_qty_map = {p.symbol: int(float(getattr(p, "qty_available",
                                                 getattr(p, "qty", 0)) or 0))
                    for p in positions}
    adjusted_sells = []
    for sym, qty, kind in sell_targets:
        available = live_qty_map.get(sym, 0)
        if available <= 0:
            summary["stop_triggered_syms"].append(sym)
            log.warning(f"  ⚠️ {sym} 实时可用持仓 0（可能止损已触发），跳过卖单")
            continue
        safe_qty = min(int(qty), available)
        if safe_qty < int(qty):
            log.warning(f"  ⚠️ {sym} plan qty={qty} 夹取到 {safe_qty}")
        adjusted_sells.append((sym, safe_qty, kind))
    sell_targets = adjusted_sells

    # ── 拉取 T+1 收盘前 mid 价（供 LOO 兜底限价 + 买入 sizing 参考）──────
    data_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)
    quote_map: dict = {}
    quote_syms = list({s for s, _, _ in sell_targets} | {b["symbol"] for b in buy_carry})
    if quote_syms and not dry_run:
        try:
            quotes = data_client.get_stock_latest_quote(
                StockLatestQuoteRequest(symbol_or_symbols=quote_syms)
            )
            for sym, q in quotes.items():
                bid = float(getattr(q, "bid_price", 0) or 0)
                ask = float(getattr(q, "ask_price", 0) or 0)
                if bid > 0 and ask > 0:
                    quote_map[sym] = (bid + ask) / 2.0
                elif ask > 0:
                    quote_map[sym] = ask
                elif bid > 0:
                    quote_map[sym] = bid
        except Exception as e:
            log.warning(f"  报价查询失败：{e}（LOO 兜底价将取买单参考价）")

    # ── 撤销即将卖出标的的活跃 GTC 止损单（同 F/H4 语义）────────────────
    stop_by_symbol: dict = {}
    if not dry_run and ENABLE_STOP_ORDERS and sell_targets:
        wanted = {sym.lower() for sym, _, _ in sell_targets}
        try:
            open_orders = client.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN))
            for o in open_orders:
                cid = str(getattr(o, "client_order_id", "") or "")
                sym_o = getattr(o, "symbol", "")
                if sym_o.lower() in wanted and cid.startswith(f"tq-stop-{sym_o.lower()}"):
                    stop_by_symbol[sym_o] = o
        except Exception as e:
            log.warning(f"  查询活跃挂单失败：{e}")

    submitted_sell_records: list = []
    for sym, qty, kind in sell_targets:
        order_req = build_moc_market_order(sym, qty, OrderSide.SELL,
                                           plan["plan_date"], kind)
        log.info(f"  SELL_MOC  {sym:8s} × {qty:4d} ({kind})")
        row = {
            "run_id": run_id, "signal_date": plan["signal_date"],
            "action": f"SELL_MOC_{kind.upper()}", "symbol": sym, "qty": qty,
            "order_type": "market", "time_in_force": "cls",
            "client_order_id": order_req.client_order_id,
            "status": "dry_run" if dry_run else "planned",
            "message": "MOC 二腿：收盘拍卖成交",
        }
        if audit:
            audit.append_order(row)
        if dry_run:
            submitted_sell_records.append({
                "symbol": sym, "qty": qty, "kind": kind,
                "client_order_id": order_req.client_order_id,
            })
            continue

        # 撤止损 → 提交卖单（顺序与三段式一致，尽可能缩短裸露窗口）
        existing_stop = stop_by_symbol.get(sym)
        stop_cancelled = False
        if existing_stop is not None:
            try:
                client.cancel_order_by_id(existing_stop.id)
                stop_cancelled = True
                log.info(f"  STOP  {sym:8s} 撤销既有止损 {existing_stop.client_order_id}")
            except Exception as e:
                log.warning(f"  {sym} 撤止损失败：{e}")
        try:
            client.submit_order(order_req)
            submitted_sell_records.append({
                "symbol": sym, "qty": qty, "kind": kind,
                "client_order_id": order_req.client_order_id,
            })
            summary["sell_orders"].append(order_req.client_order_id)
        except Exception as e:
            err = str(e).lower()
            if any(kw in err for kw in IDEMPOTENT_DUPLICATE_KEYWORDS):
                log.info(f"    ✅ {sym} 卖单已存在（duplicate），视为成功")
                submitted_sell_records.append({
                    "symbol": sym, "qty": qty, "kind": kind,
                    "client_order_id": order_req.client_order_id,
                })
                summary["sell_orders"].append(order_req.client_order_id)
            else:
                log.error(f"    ❌ 卖单提交失败 {sym}：{e}")
                summary["submit_failed"].append({"symbol": sym, "qty": qty, "kind": kind})
                if stop_cancelled and existing_stop is not None:
                    stop_qty = int(float(getattr(existing_stop, "qty", 0) or qty))
                    restore_req = _rebuild_stop_order_exact(
                        sym, stop_qty,
                        float(getattr(existing_stop, "stop_price", 0) or 0),
                        plan["plan_date"],
                    )
                    try:
                        client.submit_order(restore_req)
                        log.warning(f"    ⚠️ {sym} 已恢复止损 @ ${restore_req.stop_price:.2f}")
                    except Exception as re_err:
                        log.critical(f"    🚨 {sym} 止损恢复失败：{re_err}——仓位裸露！")

    # ── 买入侧：MOC 主路径 + LOO 兜底 ───────────────────────────────────────
    submitted_buy_records: list = []
    for b in buy_carry:
        sym  = b["symbol"]
        qty  = int(b.get("qty", 0))
        if qty <= 0:
            continue
        ref_price = float(b.get("price", 0) or quote_map.get(sym, 0) or 0)
        primary = build_moc_market_order(sym, qty, OrderSide.BUY,
                                         plan["plan_date"], "buy")
        log.info(f"  BUY_MOC   {sym:8s} × {qty:4d}  ref=${ref_price:.2f}")
        row = {
            "run_id": run_id, "signal_date": plan["signal_date"],
            "action": "BUY_MOC", "symbol": sym, "qty": qty,
            "order_type": "market", "time_in_force": "cls",
            "client_order_id": primary.client_order_id,
            "status": "dry_run" if dry_run else "planned",
            "message": f"MOC 二腿：收盘拍卖成交 ref=${ref_price:.4f}",
        }
        if audit:
            audit.append_order(row)
        if dry_run:
            submitted_buy_records.append({
                "symbol": sym, "qty": qty, "ref_price": ref_price,
                "client_order_id": primary.client_order_id,
                "fallback_kind": None, "fallback_client_order_id": None,
            })
            continue

        # 主路径：MOC BUY
        try:
            client.submit_order(primary)
            submitted_buy_records.append({
                "symbol": sym, "qty": qty, "ref_price": ref_price,
                "client_order_id": primary.client_order_id,
                "fallback_kind": None, "fallback_client_order_id": None,
            })
            summary["buy_orders"].append(primary.client_order_id)
            continue
        except Exception as e:
            err = str(e).lower()
            if any(kw in err for kw in IDEMPOTENT_DUPLICATE_KEYWORDS):
                log.info(f"    ✅ {sym} MOC 买单已存在（duplicate），视为成功")
                submitted_buy_records.append({
                    "symbol": sym, "qty": qty, "ref_price": ref_price,
                    "client_order_id": primary.client_order_id,
                    "fallback_kind": None, "fallback_client_order_id": None,
                })
                summary["buy_orders"].append(primary.client_order_id)
                continue
            log.warning(f"    ⚠️ {sym} MOC 买单被拒（{e}），降级 LOO 给 T+2 开盘拍卖")

        # 兜底：LOO 限价（价格保护）
        if ref_price <= 0:
            log.error(f"    ❌ {sym} 无参考价，LOO 兜底也无法提交")
            summary["submit_failed"].append({"symbol": sym, "qty": qty, "kind": "buy"})
            continue
        limit_price = ref_price * (1 + LOO_FALLBACK_MAX_CHASE_PCT)
        loo_req = build_loo_limit_order(sym, qty, OrderSide.BUY,
                                        limit_price, plan["plan_date"], "buy")
        try:
            client.submit_order(loo_req)
            log.info(f"    ↩︎ {sym} 已改 LOO @ ${loo_req.limit_price:.2f}（次日开盘拍卖）")
            submitted_buy_records.append({
                "symbol": sym, "qty": qty, "ref_price": ref_price,
                "client_order_id": primary.client_order_id,
                "fallback_kind": "loo",
                "fallback_client_order_id": loo_req.client_order_id,
                "fallback_limit_price": loo_req.limit_price,
            })
            summary["loo_fallback"].append(loo_req.client_order_id)
        except Exception as e:
            log.error(f"    ❌ {sym} LOO 兜底也失败：{e}")
            summary["submit_failed"].append({"symbol": sym, "qty": qty, "kind": "buy"})

    if not dry_run:
        state[PENDING_EXECUTE_KEY] = {
            "plan_date": plan["plan_date"],
            "signal_date": plan["signal_date"],
            "execute_date": str(today),
            "sells": submitted_sell_records,
            "buys": submitted_buy_records,
            "stop_triggered_syms": summary["stop_triggered_syms"],
        }
        state.pop(CYCLE_PLAN_KEY, None)

    return summary


def reconcile_moc_execute(client: TradingClient, state: dict, run_id: str,
                          dry_run: bool,
                          audit: Optional[AuditWriter] = None) -> dict:
    """
    phase=reconcile（T+2 09:45 ET 运行）：核实 pending_execute 中所有订单
    最终成交状态，为新建/加仓仓位补挂 GTC 止损单，清理 state。
    MOC 单在 T+1 16:00 已成交（同天可查），LOO 兜底单在 T+2 09:30 开盘拍卖成交。
    """
    pending = state.get(PENDING_EXECUTE_KEY)
    summary = {
        "had_pending": bool(pending),
        "sells_filled": [], "sells_unfilled": [],
        "buys_filled": [], "buys_unfilled": [],
        "stop_added": [], "note": "",
    }
    if not pending:
        log.info("  pending_execute 为空，无待核实。")
        return summary

    positions = client.get_all_positions()
    qty_map = {p.symbol: int(float(getattr(p, "qty", 0) or 0)) for p in positions}
    naked = [(s, q) for s, q in qty_map.items() if q < 0]
    if naked:
        summary["note"] = f"🚨 发现负持仓 {naked}，中止 reconcile"
        log.critical(f"  🚨 负持仓 {naked}，中止 reconcile 等待人工核对")
        return summary

    def _check(cid: str):
        try:
            o = client.get_order_by_client_id(cid)
            filled = float(getattr(o, "filled_qty", 0) or 0)
            status = getattr(getattr(o, "status", ""), "value",
                             str(getattr(o, "status", "")))
            return filled, status, o
        except Exception as e:
            log.warning(f"  查询 {cid} 失败：{e}")
            return None, None, None

    for rec in pending.get("sells", []):
        cid = rec["client_order_id"]
        filled, status, _ = _check(cid)
        if filled is None:
            summary["sells_unfilled"].append({**rec, "reason": "query_failed"})
            continue
        if filled >= rec["qty"]:
            summary["sells_filled"].append({**rec, "status": status})
        else:
            summary["sells_unfilled"].append({**rec, "filled_qty": filled, "status": status})

    for rec in pending.get("buys", []):
        # 主单 → 兜底顺序检查
        cid = rec["client_order_id"]
        filled, status, _ = _check(cid)
        actual_cid = cid
        if filled is not None and filled >= rec["qty"]:
            summary["buys_filled"].append({**rec, "status": status, "filled_via": "moc"})
        elif rec.get("fallback_client_order_id"):
            fb_cid = rec["fallback_client_order_id"]
            fb_filled, fb_status, _ = _check(fb_cid)
            actual_cid = fb_cid
            if fb_filled is not None and fb_filled >= rec["qty"]:
                summary["buys_filled"].append({**rec, "status": fb_status, "filled_via": "loo"})
            else:
                summary["buys_unfilled"].append({
                    **rec, "moc_filled": filled, "moc_status": status,
                    "loo_filled": fb_filled, "loo_status": fb_status,
                })
        else:
            summary["buys_unfilled"].append({
                **rec, "filled_qty": filled, "status": status,
            })

    # 为持有仓位补挂止损（H4 修复：真实运行也补挂，不只是 dry_run）
    if not dry_run and ENABLE_STOP_ORDERS and positions:
        try:
            existing_stops = {getattr(o, "symbol", ""): o for o in
                              client.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN))
                              if str(getattr(o, "client_order_id", "") or "").startswith("tq-stop-")}
        except Exception as e:
            log.warning(f"  查询既有止损失败：{e}")
            existing_stops = {}
        for p in positions:
            sym = p.symbol
            qty = int(float(getattr(p, "qty", 0) or 0))
            if qty <= 0:
                continue
            if sym in existing_stops and _order_is_active(existing_stops[sym]):
                continue
            ref_price = float(getattr(p, "avg_entry_price", 0) or 0)
            if ref_price <= 0:
                ref_price = float(getattr(p, "current_price", 0) or 0)
            if ref_price <= 0:
                log.warning(f"  {sym} 无参考价，止损补挂跳过")
                continue
            stop_req = build_stop_order(sym, qty, ref_price, pending["signal_date"])
            try:
                client.submit_order(stop_req)
                summary["stop_added"].append(sym)
                log.info(f"  STOP+ {sym:8s} × {qty}  stop=${stop_req.stop_price:.2f}")
            except Exception as e:
                err = str(e).lower()
                if any(kw in err for kw in IDEMPOTENT_DUPLICATE_KEYWORDS):
                    continue
                log.warning(f"  {sym} 止损补挂失败：{e}")

    # 全部核实完毕 → 清理 state
    if not dry_run:
        all_settled = (not summary["sells_unfilled"]) and (not summary["buys_unfilled"])
        if all_settled:
            state["last_rebalance"] = pending["signal_date"]
            state.pop(PENDING_EXECUTE_KEY, None)
            log.info("  ✅ pending_execute 全部核实完毕，state 已清理")
        else:
            log.warning(
                f"  ⚠️ 仍有未成交：sells={[r['symbol'] for r in summary['sells_unfilled']]} "
                f"buys={[r['symbol'] for r in summary['buys_unfilled']]}，保留 pending_execute"
            )
    return summary


def execute_buy_phase(client: TradingClient, state: dict, run_id: str, dry_run: bool,
                       audit: Optional[AuditWriter] = None,
                       sizing_capital: Optional[float] = None,
                       earnings_allow: Optional[set] = None) -> dict:
    """
    phase=buy（T+2 开盘前运行）：逐笔核实 pending_sell 中卖单的实际成交情况，
    再用账户当下真实可用资金提交买单（资金不足则按比例缩减，保留最小 1 股）。
    """
    pending = state.get(PENDING_SELL_KEY)
    summary = {
        "had_pending": bool(pending), "fill_issues": [], "orders": [],
        "scaled": False, "buy_pending": False, "note": "",
    }
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
    unresolved_sells = []  # 未完全成交的卖单，写回 cycle_plan 供下一轮 sell 阶段重试
    for o in pending.get("orders", []):
        cid = o["client_order_id"]
        try:
            order = client.get_order_by_client_id(cid)
            filled_qty = float(getattr(order, "filled_qty", 0) or 0)
            status_val = getattr(getattr(order, "status", ""), "value", str(getattr(order, "status", "")))
            if filled_qty < o["qty"]:
                remaining = int(o["qty"] - filled_qty)  # 整数股数，避免下游订单带小数 qty
                issue = f"{o['symbol']} 卖单未完全成交（filled={filled_qty}/{o['qty']}, status={status_val}）"
                log.warning(f"  ⚠️ {issue}")
                summary["fill_issues"].append(issue)
                bad_symbols.add(o["symbol"])
                if remaining > 0:
                    unresolved_sells.append({"symbol": o["symbol"], "qty": remaining, "kind": o.get("kind", "close")})
        except Exception as e:
            # 查询失败时无法确认实际成交量，保守按"完全未成交"处理，宁可重复尝试卖出
            # （若上次其实已成交，重试时会因 qty_available 不足而被券商拒绝，不会造成超卖）。
            issue = f"{o['symbol']} 卖单成交状态查询失败: {e}"
            log.warning(f"  ⚠️ {issue}")
            summary["fill_issues"].append(issue)
            bad_symbols.add(o["symbol"])
            unresolved_sells.append({"symbol": o["symbol"], "qty": o["qty"], "kind": o.get("kind", "close")})

    summary["had_fill_issues"] = bool(bad_symbols)

    # ── 用真实可用资金提交买单 ───────────────────────────────────────────────
    account = client.get_account()
    real_buying_power = float(account.buying_power)
    positions = client.get_all_positions()
    position_qty_map = {p.symbol: int(float(getattr(p, "qty", 0) or 0)) for p in positions}

    # ── F1 修复的下游守卫：核查是否存在负持仓（裸空头）───────────────────────
    # sell 阶段的实时持仓夹取（见 execute_sell_phase）是主防线。这里作为兜底：
    # 无论哪条路径造成的负持仓（未修复的旧版本崩溃残留、手工误操作、券商 bug），
    # 一旦发现立即中止本次 buy 并邮件报警。买入不受损失，但可避免继续叠加错误。
    naked_shorts = [(sym, q) for sym, q in position_qty_map.items() if q < 0]
    if naked_shorts:
        summary["naked_shorts"] = naked_shorts
        summary["note"] = (summary.get("note") or "") + \
            f" 🚨 发现负持仓 {naked_shorts}，中止 buy 阶段等待人工介入"
        log.critical(f"  🚨 发现负持仓（可能是历史裸空头）：{naked_shorts}")
        log.critical("     中止 buy 阶段。请人工核实 broker 侧仓位并平掉空头后再继续。")
        return summary
    position_market_value_map = {
        p.symbol: float(getattr(p, "market_value", 0) or 0)
        for p in positions
    }
    held_symbols = set(position_qty_map.keys())
    buy_carry = pending.get("buy_carry", [])
    pending_target_snapshot = pending.get("target_snapshot")
    if bad_symbols:
        skipped_syms = [b["symbol"] for b in buy_carry if b["symbol"] in bad_symbols]
        if skipped_syms:
            log.warning(f"  ⚠️ 以下标的卖单未确认成交，本轮暂停对应买入决策: {skipped_syms}")
        buy_carry = [b for b in buy_carry if b["symbol"] not in bad_symbols]

    # ── 财报避雷复核：pending_sell 里的 buy_carry 是 sell 阶段（甚至更早的 plan 阶段）
    # 冻结的旧计划，买入前再查一次财报日历，命中的标的直接取消本次买入（不发起卖出——
    # 已持仓部分的清仓交给下一轮 sell 阶段的复核逻辑）。
    new_blackout: set = set()
    buy_recheck_degraded = False
    buy_recheck_failed_symbols: set = set()
    if pending_target_snapshot is not None:
        earnings_check_syms = list(pending_target_snapshot.keys())
    else:
        earnings_check_syms = [b["symbol"] for b in buy_carry]
    if EARNINGS_BLACKOUT_DAYS > 0 and earnings_check_syms:
        check_syms = [sym for sym in earnings_check_syms if sym not in ("QQQ", "SPY")]
        new_blackout, buy_recheck_degraded, buy_recheck_failed_symbols = lookup_upcoming_earnings(
            check_syms, EARNINGS_BLACKOUT_DAYS
        )
        new_blackout &= set(check_syms)
        if earnings_allow:
            overridden = new_blackout & earnings_allow
            if overridden:
                log.warning(f"  ⚠️ buy 阶段手动豁免财报避雷：{sorted(overridden)}")
                new_blackout -= earnings_allow
        if new_blackout:
            log.warning(f"  📅 buy 阶段财报复核命中：{sorted(new_blackout)}，取消本次买入")
            summary["earnings_skipped"] = sorted(new_blackout)
            buy_carry = [b for b in buy_carry if b["symbol"] not in new_blackout]
    summary["earnings_recheck_degraded"] = buy_recheck_degraded
    summary["earnings_recheck_failed_symbols"] = sorted(buy_recheck_failed_symbols)

    # ── 财报剔除导致候选数变化：按剩余候选重新计算等权目标金额，而不是留着过时份额 ──
    target_n_sell = pending.get("target_n")
    summary["earnings_reallocated"] = False
    if pending_target_snapshot is not None:
        final_target_snapshot = {
            sym: data for sym, data in pending_target_snapshot.items()
            if sym not in new_blackout
        }
        full_reallocation_required = bool(pending.get("reallocation_required")) or bool(new_blackout)
    else:
        final_target_snapshot = None
        full_reallocation_required = False

    if full_reallocation_required:
        if sizing_capital is None:
            log.warning(
                "  ⚠️ 财报剔除后本应基于完整目标快照重新分配，但 sizing_capital 缺失，"
                "本轮仅过滤不重算（可能资金利用不充分）"
            )
        else:
            # bad_symbols（卖单未确认成交、本轮暂停对应买入决策的标的）既不会参与本轮重算买入
            # （见下面循环里的跳过），持有市值也已从可投资预算里扣掉——分母必须同步剔除它们，
            # 否则会用"排除了 bad_symbols 市值的预算"去除以"包含 bad_symbols 的候选数"，
            # 对真正参与本轮分配的标的造成明显低配（资金没有真正分完）。
            adjustable_target_snapshot = {
                sym: data for sym, data in final_target_snapshot.items() if sym not in bad_symbols
            }
            target_n_final = len(adjustable_target_snapshot)
            excluded_symbols = set(pending.get("earnings_forced_close", [])) | new_blackout | bad_symbols
            excluded_held_value = sum(position_market_value_map.get(sym, 0.0) for sym in excluded_symbols)
            summary["excluded_held_value"] = round(excluded_held_value, 2)
            investable = max(
                sizing_capital * (1 - MIN_CASH_BUFFER_PCT) - excluded_held_value,
                0.0,
            )
            new_target_val = min(investable / target_n_final, sizing_capital * MAX_POSITION_PCT) if target_n_final > 0 else 0.0
            resized = []
            for sym, target_data in adjustable_target_snapshot.items():
                price = float(target_data.get("price", 0) or 0)
                cur_qty = position_qty_map.get(sym, 0)
                new_qty = int(new_target_val / (price * 1.05)) if price > 0 else 0
                new_delta = new_qty - cur_qty
                if new_delta <= 0:
                    log.info(f"  {sym} 重新分配后已达/超目标仓位，本轮取消买入")
                    summary.setdefault("earnings_reallocation_dropped", []).append(sym)
                    continue
                new_drift = (cur_qty * price - new_target_val) / new_target_val if new_target_val > 0 else 0.0
                resized.append({
                    "symbol": sym,
                    "qty": new_delta,
                    "price": price,
                    "is_new": cur_qty == 0,
                    "drift": round(new_drift, 6),
                })
            buy_carry = resized
            summary["earnings_reallocated"] = True
            summary["target_val_reallocated"] = round(new_target_val, 2)
            log.info(
                f"  📅 财报剔除后候选数 {len(pending_target_snapshot)}→{target_n_final}，"
                f"基于完整目标快照重算等权目标金额=${new_target_val:,.0f}"
            )
    elif new_blackout and buy_carry:
        if target_n_sell is None or sizing_capital is None:
            log.warning(
                "  ⚠️ 财报剔除后本应重新分配买入金额，但 target_n/sizing_capital 缺失，"
                "本轮仅过滤不重算（可能资金利用不充分）"
            )
        else:
            target_n_final = max(target_n_sell - len(new_blackout), 0)
            investable = sizing_capital * (1 - MIN_CASH_BUFFER_PCT)
            new_target_val = min(investable / target_n_final, sizing_capital * MAX_POSITION_PCT) if target_n_final > 0 else 0.0
            resized = []
            for b in buy_carry:
                sym, price = b["symbol"], b["price"]
                cur_qty = position_qty_map.get(sym, 0)
                new_qty = int(new_target_val / (price * 1.05)) if price > 0 else 0
                new_delta = new_qty - cur_qty
                if new_delta <= 0:
                    log.info(f"  {sym} 重新分配后已达/超目标仓位，本轮取消买入")
                    summary.setdefault("earnings_reallocation_dropped", []).append(sym)
                    continue
                new_drift = (cur_qty * price - new_target_val) / new_target_val if new_target_val > 0 else 0.0
                resized.append({**b, "qty": new_delta, "drift": round(new_drift, 6)})
            buy_carry = resized
            summary["earnings_reallocated"] = True
            summary["target_val_reallocated"] = round(new_target_val, 2)
            log.info(
                f"  📅 财报剔除后候选数 {target_n_sell}→{target_n_final}，"
                f"重算等权目标金额=${new_target_val:,.0f}"
            )

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
    pending_buy_orders = []
    buy_submit_failed = []
    for b in buy_carry:
        qty = int(b["qty"] * scale) if scale < 1.0 else b["qty"]
        if qty <= 0:
            log.warning(f"  ⚠️ {b['symbol']} 缩减后数量为 0（资金不足），本轮跳过该标的买入")
            continue
        sym, price, is_new, drift = b["symbol"], b["price"], b.get("is_new", True), b.get("drift", 0.0)
        if is_new and sym in held_symbols:
            # buy_carry 的 is_new 是计划生成时算的，若计划卡了多轮才被消费，中途可能已经
            # 通过其他渠道建过仓——提交前用当下真实持仓再核实一次，防止同一标的重复建仓
            log.warning(f"  ⚠️ {sym} 计划为新建仓位，但当前已有持仓，跳过本次买入以防重复建仓")
            summary["fill_issues"].append(f"{sym} 跳过买入：计划新建但已有持仓")
            continue
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
        pending_item = {
            "symbol": sym,
            "qty": qty,
            "client_order_id": order_req.client_order_id,
            "status": "submitted",
        }
        if not dry_run:
            try:
                client.submit_order(order_req)
            except Exception as e:
                err_msg = str(e).lower()
                if any(kw in err_msg for kw in IDEMPOTENT_DUPLICATE_KEYWORDS):
                    log.info(f"    ✅ {sym} 买单此前已提交（client_order_id 重复，视为成功）")
                elif any(kw in err_msg for kw in ("halt", "not_tradable", "suspended", "asset_not_tradable")):
                    log.warning(f"    ⚠️ {sym} 停牌/LULD 熔断，写入重试队列")
                    _append_halt_pending(sym, qty, price, pending["signal_date"], run_id)
                    failed_item = {**pending_item, "status": "halted", "message": str(e)}
                    pending_buy_orders.append(failed_item)
                    buy_submit_failed.append(failed_item)
                else:
                    log.error(f"    ❌ 买入失败 {sym}: {e}")
                    failed_item = {**pending_item, "status": "submit_failed", "message": str(e)}
                    pending_buy_orders.append(failed_item)
                    buy_submit_failed.append(failed_item)
                continue
        orders_out.append(pending_item)
        if not dry_run:
            pending_buy_orders.append(pending_item)

    summary["orders"] = orders_out
    summary["submit_failed"] = buy_submit_failed

    # 实盘买单返回 accepted 只代表券商接单，不能假设已经成交。止损单必须等后续
    # reconcile_pending_buy() 核实成交并读取真实持仓后再补挂。
    stop_orders_submitted = 0
    if dry_run:
        try:
            fresh_positions = client.get_all_positions()
            stop_orders_submitted = ensure_stop_orders_for_positions(
                client, fresh_positions, pending["signal_date"], dry_run
            )
        except Exception as e:
            log.warning(f"  ⚠️ 买入后补挂止损单失败: {e}")
    summary["stop_orders_submitted"] = stop_orders_submitted

    if not dry_run:
        state["last_order_signal_date"] = pending["signal_date"]
        state.pop(PENDING_SELL_KEY, None)
        if pending_buy_orders:
            state[PENDING_BUY_KEY] = {
                "submit_date": str(today),
                "signal_date": pending["signal_date"],
                "orders": pending_buy_orders,
            }
            summary["buy_pending"] = True
        else:
            state["last_rebalance"] = str(today)
        if unresolved_sells:
            # 未确认成交的卖单不能就此放弃：写回一份新的 cycle_plan，close_all/trim 里只放
            # 未成交剩余量，对应的买入决策一并带上，下一轮 sell 阶段会自动重试卖出，
            # 再下一轮 buy 阶段重新核实并买入——避免这些标的被静默跳过、账户目标仓位永久跑偏。
            retry_close = [{"symbol": s["symbol"], "qty": s["qty"]} for s in unresolved_sells if s["kind"] == "close"]
            retry_trim  = [{"symbol": s["symbol"], "qty": s["qty"]} for s in unresolved_sells if s["kind"] != "close"]
            # 卖出未完全成交说明仓位仍部分存在，对应买入决策不再是"新建"而是"加仓"
            retry_buy = [
                {**b, "is_new": False} for b in pending.get("buy_carry", []) if b["symbol"] in bad_symbols
            ]
            # cycle_plan 此时可能已经非空——sell 阶段若曾有标的提交失败，会把只含那些标的的
            # 重试计划写在这里；不能无条件覆盖，否则那批标的的清仓/减仓决策会被本次买入阶段
            # 写回的（针对未确认成交卖单的）重试计划整体顶掉、永久丢失。按 symbol 去重合并。
            existing_plan = state.get(CYCLE_PLAN_KEY) or {}
            if existing_plan and existing_plan.get("signal_date") != pending["signal_date"]:
                summary["signal_date_mixed"] = True
                log.error(
                    f"  ❌ 待合并的 cycle_plan signal_date（{existing_plan.get('signal_date')}）与本轮 "
                    f"pending_sell signal_date（{pending['signal_date']}）不一致，两个周期被意外混合，请人工核查 state.json"
                )
            seen_syms = {c["symbol"] for c in retry_close} | {t["symbol"] for t in retry_trim}
            retry_close = retry_close + [c for c in existing_plan.get("close_all", []) if c["symbol"] not in seen_syms]
            retry_trim  = retry_trim  + [t for t in existing_plan.get("trim", [])      if t["symbol"] not in seen_syms]
            seen_buy_syms = {b["symbol"] for b in retry_buy}
            retry_buy = retry_buy + [b for b in existing_plan.get("buy", []) if b["symbol"] not in seen_buy_syms]
            retry_plan = {
                "plan_date": str(today),
                "signal_date": pending["signal_date"],
                "close_all": retry_close,
                "trim": retry_trim,
                "buy": retry_buy,
            }
            state[CYCLE_PLAN_KEY] = retry_plan
            summary["retry_plan_written"] = True
            log.warning(
                f"  ⚠️ {len(unresolved_sells)} 笔卖单未确认成交，已写回 cycle_plan 供下一轮重试："
                f"close={[s['symbol'] for s in retry_close]} trim={[s['symbol'] for s in retry_trim]}"
            )
    return summary


def reconcile_pending_buy(client: TradingClient, state: dict, run_id: str,
                          dry_run: bool, audit: Optional[AuditWriter] = None) -> dict:
    """核实 buy 阶段已提交订单；全部成交后才补挂止损并完成调仓周期。"""
    pending = state.get(PENDING_BUY_KEY)
    summary = {
        "had_pending": bool(pending),
        "completed": False,
        "fill_issues": [],
        "stop_orders_submitted": 0,
    }
    if not pending:
        log.info("  pending_buy 为空，无需核实买单。")
        return summary

    remaining_orders = []
    for item in pending.get("orders", []):
        sym = item["symbol"]
        qty = int(item["qty"])
        cid = item["client_order_id"]
        try:
            order = client.get_order_by_client_id(cid)
            filled_qty = float(getattr(order, "filled_qty", 0) or 0)
            raw_status = getattr(order, "status", "")
            broker_status = getattr(raw_status, "value", str(raw_status)).lower()
        except Exception as e:
            err_msg = str(e).lower()
            retry_attempt = int(item.get("retry_attempt", 0) or 0)
            confirmed_not_found = "404" in err_msg or "not found" in err_msg
            can_retry = (
                item.get("status") == "submit_failed"
                and confirmed_not_found
                and retry_attempt < 1
                and not dry_run
            )
            if can_retry:
                next_attempt = retry_attempt + 1
                retry_req = build_market_order(
                    sym, qty, OrderSide.BUY, pending["signal_date"],
                    f"buyretry{next_attempt}",
                )
                try:
                    client.submit_order(retry_req)
                    log.warning(
                        f"  ⚠️ {sym} 原买单确认不存在，已受控重提 × {qty} "
                        f"（client_order_id={retry_req.client_order_id}）"
                    )
                    remaining_orders.append({
                        **item,
                        "client_order_id": retry_req.client_order_id,
                        "status": "submitted",
                        "retry_attempt": next_attempt,
                    })
                except Exception as retry_error:
                    issue = f"{sym} 买单受控重提失败: {retry_error}"
                    log.error(f"  ❌ {issue}")
                    summary["fill_issues"].append(issue)
                    remaining_orders.append({
                        **item,
                        "status": "retry_failed",
                        "retry_attempt": next_attempt,
                        "message": str(retry_error),
                    })
                continue
            issue = f"{sym} 买单成交状态查询失败: {e}"
            log.warning(f"  ⚠️ {issue}")
            summary["fill_issues"].append(issue)
            remaining_orders.append({**item, "status": "query_error", "message": str(e)})
            continue

        if filled_qty >= qty:
            log.info(f"  ✅ {sym} 买单已完全成交（filled={filled_qty:g}/{qty}）")
            continue

        issue = f"{sym} 买单未完全成交（filled={filled_qty:g}/{qty}, status={broker_status}）"
        log.warning(f"  ⚠️ {issue}")
        summary["fill_issues"].append(issue)
        remaining_orders.append({
            **item,
            "status": broker_status or "unknown",
            "filled_qty": filled_qty,
        })

    if remaining_orders:
        if not dry_run:
            state[PENDING_BUY_KEY] = {**pending, "orders": remaining_orders}
        return summary

    try:
        fresh_positions = client.get_all_positions()
        summary["stop_orders_submitted"] = ensure_stop_orders_for_positions(
            client, fresh_positions, pending["signal_date"], dry_run
        )
    except Exception as e:
        issue = f"买单成交后补挂止损失败: {e}"
        log.warning(f"  ⚠️ {issue}")
        summary["fill_issues"].append(issue)
        return summary

    summary["completed"] = True
    if not dry_run:
        state.pop(PENDING_BUY_KEY, None)
        state["last_rebalance"] = str(date.today())
        state["last_order_signal_date"] = pending["signal_date"]
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
        earnings_blackout, earnings_degraded, _ = lookup_upcoming_earnings(
            check_syms, EARNINGS_BLACKOUT_DAYS
        )
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
    investable = equity * (1 - MIN_CASH_BUFFER_PCT)
    target_val = min(investable / len(target_set), equity * MAX_POSITION_PCT) if target_set else 0.0

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
        current_qty = int(float(getattr(current_map[sym], "qty", 0) or 0)) if sym in current_map else 0
        target_qty  = int(target_val / (price * 1.05))  # 5% 缓冲防开盘跳空超支
        if target_qty <= 0 and current_qty == 0:
            log.warning(
                f"  ⚠️  {sym} 单股价格 ${price:,.2f} 超过等权目标金额 ${target_val:,.2f}，"
                f"买入 1 股会超配，跳过本次建仓"
            )
            continue
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
    if not dry_run:
        cancel_stop_orders_for_symbols(
            client, [sym for sym, _ in close_all_list] + [sym for sym, *_ in trim_list]
        )
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
                err_msg = str(e).lower()
                if any(kw in err_msg for kw in IDEMPOTENT_DUPLICATE_KEYWORDS):
                    log.info(f"    ✅ {sym} 平仓单此前已提交（client_order_id 重复，视为成功）")
                    n_orders += 1
                else:
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
                err_msg = str(e).lower()
                if any(kw in err_msg for kw in IDEMPOTENT_DUPLICATE_KEYWORDS):
                    log.info(f"    ✅ {sym} TRIM 单此前已提交（client_order_id 重复，视为成功）")
                    n_orders += 1
                else:
                    log.error(f"    ❌ TRIM 失败 {sym}: {e}")

    # ── 2. BUY 阶段：新建 + add ──────────────────────────────────────────────────
    failed_buys: list[str] = []
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
                if any(kw in err_msg for kw in IDEMPOTENT_DUPLICATE_KEYWORDS):
                    log.info(f"    ✅ {sym} 买单此前已提交（client_order_id 重复，视为成功）")
                    n_orders += 1
                elif any(kw in err_msg for kw in ("halt", "not_tradable", "suspended", "asset_not_tradable")):
                    log.warning(f"    ⚠️ {sym} 停牌/LULD 熔断，写入重试队列")
                    _append_halt_pending(sym, qty, price, signal_date, run_id)
                else:
                    # 先记录失败继续处理下一个标的，不在循环内 raise：
                    # 卖单可能已提交成功，若这里中断整个函数会让剩余待买标的完全得不到处理，
                    # 账户停在"卖了没买"的半调仓状态。循环结束后统一 raise 供上层报警。
                    log.error(f"    ❌ 买入失败 {sym}: {e}")
                    failed_buys.append(f"{sym}: {e}")

    if not close_all_list and not trim_list and not buy_list:
        log.info("  持仓无需变动（目标与当前完全一致）")
    elif dry_run:
        log.info("  [DRY RUN] 上述订单均未提交")

    if failed_buys:
        raise RuntimeError(f"以下 {len(failed_buys)} 笔买单提交失败: {'; '.join(failed_buys)}")

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
                        help="核实 pending_buy 并重试 LULD 挂单（由单独 cron 在 9:45 AM ET 触发）")
    parser.add_argument("--earnings-allow", type=str, default="",
                        help="逗号分隔股票代码，本次运行手动豁免财报避雷强制出场（如 MU,AAPL）。"
                             "仅本次生效，需人工确认财报预期正面后使用，风险自负。")
    parser.add_argument("--phase", type=str, default="both",
                        choices=["both", "plan", "sell", "buy", "execute", "reconcile"],
                        help=(
                            "调仓阶段（默认 both = 人工一次性卖+买，仅供 dry-run/手动测试）:\n"
                            "  plan      — T 收盘后 ~16:05 ET：计算目标持仓与买卖计划，写入 state.json\n"
                            "  三段式路径（EXEC_MODE=three_phase 默认）：\n"
                            "    sell    — T+1 ~15:50 ET：主动让价提交限价卖单\n"
                            "    buy     — T+2 ~09:15 ET：核实卖单成交后用实际可用资金提交买单\n"
                            "  MOC 单段路径（EXEC_MODE=moc_single 推荐）：\n"
                            "    execute — T+1 ~15:35 ET：提交 SELL MOC + BUY MOC 二腿；BUY 被拒时降级 LOO 兜底\n"
                            "    reconcile — T+2 ~09:45 ET：核实成交、补挂止损、清理 state\n"
                            "阶段之间用真实交易日（节假日感知）衔接，不是固定周几。"
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
        state = _load_state()
        retry_run_id = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
        if state.get(PENDING_BUY_KEY):
            try:
                client = TradingClient(API_KEY, SECRET_KEY, paper=PAPER)
                reconcile_pending_buy(client, state, retry_run_id, dry_run=args.dry_run)
            except Exception as e:
                log.error(f"pending_buy 核实失败，本轮保留状态并继续 LULD 重试: {e}")
        retry_summary = retry_halted_orders(dry_run=args.dry_run, state=state, run_id=retry_run_id) or {}
        if not args.dry_run:
            _save_state(state)
        # --retry-halted 是独立 cron 分支，直接 return，走不到下面 finally 里唯一的
        # send_email() 调用——之前版本这条路径下无论发生什么都不会发邮件，是"LULD
        # 熔断静默卡住"的根因。这里仅在真正需要人工介入时（Broker 状态不确定）主动发一封，
        # 追价放弃（chase_capped）和确认不存在（confirmed_absent）均按约定不报警。
        uncertain = retry_summary.get("uncertain_symbols") or []
        if uncertain and EMAIL_ENABLED:
            try:
                subject = build_email_subject("halt_retry_stuck_uncertain", retry_run_id, PAPER)
                body_lines = [
                    "## LULD 熔断重试结束，以下标的 Broker 订单状态不确定，需人工介入核查",
                    f"- run_id: {retry_run_id}",
                    f"- 已用重试次数: {retry_summary.get('attempts_used', 0)}/{HALT_RETRY_MAX_ATTEMPTS}",
                    "",
                    "## 🚨 状态不确定标的（pending_buy 已保留，将继续阻塞下一次调仓计划）",
                    *[f"- {sym}" for sym in uncertain],
                ]
                if retry_summary.get("confirmed_absent_symbols"):
                    body_lines += [
                        "",
                        "## 已放弃（Broker 已确认订单不存在，未产生持仓，无需处理）",
                        *[f"- {sym}" for sym in retry_summary["confirmed_absent_symbols"]],
                    ]
                if retry_summary.get("chase_capped_symbols"):
                    body_lines += [
                        "",
                        "## 追价超过上限，本轮暂未处理（继续等待或人工确认是否追单）",
                        *[f"- {sym}" for sym in retry_summary["chase_capped_symbols"]],
                    ]
                send_email(subject, "\n".join(body_lines))
            except Exception as email_e:
                log.warning(f"LULD 重试报警邮件发送失败: {email_e}")
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

        # sizing_capital 提前到这里计算：phase=sell/buy 在下面会提前 return，
        # 需要在那之前就可用（buy 阶段财报剔除后重新分配买入金额要用到）。
        sizing_capital = SIM_CAPITAL_USD if SIM_CAPITAL_USD > 0 else equity
        if SIM_CAPITAL_USD > 0:
            log.info(f"  💰 仓位规模模拟：按 ${SIM_CAPITAL_USD:,.0f} 计算买入数量（账户真实净值 ${equity:,.2f} 仅用于回撤/Kill Switch 判断）")

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
            summary = execute_sell_phase(
                client, state, run_id, args.dry_run, audit, earnings_allow
            )
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
            elif summary.get("blocked_stale_pending_sell"):
                status = "pending_sell_stale_blocked"
                email_lines = ["\n".join([
                    "## 今日结论：检测到未消费的 pending_sell，本轮跳过卖出提交",
                    f"- run_id: {run_id}",
                    "- ⚠️ 上一轮 sell 阶段提交的卖单尚未被 buy 阶段核实/消费（buy 阶段可能遗漏运行或崩溃）",
                    "- 为避免覆盖丢失上一轮的卖单核实指针与买入计划，本轮未提交任何新卖单",
                    "- 请人工检查 buy 阶段运行记录，确认后手动触发 --phase buy 补跑",
                ])]
            elif summary.get("all_failed") and not summary.get("naked_positions"):
                # 注意：naked_positions 非空时必须优先走下面的 else 分支——
                # 全部卖单提交失败 + 止损单已撤销且恢复也失败，是比"单纯全部提交失败"更危险的
                # 无保护裸露场景，不能被这里的 order_submit_failed 状态吞掉、掩盖真正的报警等级。
                status = "order_submit_failed"
                email_lines = ["\n".join([
                    "## 今日结论：尾盘卖出全部提交失败",
                    f"- run_id: {run_id}",
                    "- ⚠️ 本轮 cycle_plan 中的卖单全部提交失败，已保留 cycle_plan 供下次 sell 重试，未推进周期",
                    "- 请人工检查 API 连接/账户状态/标的是否可交易",
                    *sell_earnings_email_lines(summary),
                ])]
            else:
                had_submit_failures = bool(summary.get("submit_failed"))
                had_naked_positions = bool(summary.get("naked_positions"))
                status = (
                    "sell_naked_stop_restore_failed" if had_naked_positions
                    else "sell_submitted_with_issues" if had_submit_failures
                    else "sell_submitted"
                )
                order_lines = [f"- {o['symbol']} {o['kind']} × {o['qty']}（client_order_id={o['client_order_id']}）"
                               for o in summary["orders"]] or ["- 无实际提交（全部下单失败，请查日志）"]
                buy_carry_lines = [f"- {b['symbol']} × {b['qty']} @ ~${b['price']:.2f}"
                                    for b in summary["buy_carry"]] or ["- 无后续买入计划"]
                failed_lines = [f"- {f['symbol']} {f['kind']} × {f['qty']}（提交失败，已写回 cycle_plan 供下轮重试）"
                                 for f in summary.get("submit_failed", [])]
                naked_lines = [f"- 🚨 {n['symbol']} × {n['qty']}（止损单已撤销且恢复失败，当前完全无保护）: {n['error']}"
                                for n in summary.get("naked_positions", [])]
                email_lines = ["\n".join([
                    "## 今日结论：尾盘前卖出已提交"
                    + ("（🚨 存在无保护裸露仓位，需立即人工介入）" if had_naked_positions
                       else "（部分标的提交失败，需人工复核）" if had_submit_failures else ""),
                    f"- run_id: {run_id}",
                    f"- 提示: {summary['note']}" if summary["note"] else "",
                    *sell_earnings_email_lines(summary),
                    "",
                    *(["## 🚨 无保护裸露仓位（原止损单已撤销、卖单与止损单恢复均失败）"] + naked_lines + [""] if naked_lines else []),
                    "## 已提交卖单",
                    *order_lines,
                    *(["", "## ⚠️ 提交失败标的"] + failed_lines if failed_lines else []),
                    "",
                    "## 次日开盘前将执行的买入计划预览",
                    *buy_carry_lines,
                ])]
            log.info(f"[phase=sell] 完成，status={status}")
            return

        if args.phase == "buy":
            summary = execute_buy_phase(
                client, state, run_id, args.dry_run, audit, sizing_capital, earnings_allow
            )
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
                status = buy_phase_status(summary)
                buy_pending = bool(summary.get("buy_pending"))
                order_lines = [f"- {o['symbol']} × {o['qty']}" for o in summary["orders"]] or ["- 无实际提交"]
                issue_lines = summary["fill_issues"] or ["- 无异常，全部卖单如期成交"]
                failed_buy_lines = [
                    f"- {o['symbol']} × {o['qty']}（{o['status']}，已保留 pending_buy）"
                    for o in summary.get("submit_failed", [])
                ]
                email_lines = ["\n".join([
                    (
                        "## 今日结论：买单已提交，等待 09:45 ET 成交核实"
                        if buy_pending
                        else "## 今日结论：调仓周期完成"
                    ) + ("（存在异常，需人工复核）" if status.endswith("with_issues") else ""),
                    f"- run_id: {run_id}",
                    f"- 提示: {summary['note']}" if summary["note"] else "",
                    f"- 买入资金是否缩减: {'是' if summary['scaled'] else '否'}",
                    (
                        "- 止损单: 待买单确认成交后按真实持仓补挂"
                        if buy_pending
                        else f"- 买入后新挂止损单: {summary.get('stop_orders_submitted', 0)} 笔"
                    ),
                    f"- 📅 财报复核取消买入: {summary['earnings_skipped']}" if summary.get("earnings_skipped") else "",
                    f"- 📅 已按剩余候选重新计算等权买入金额: ${summary['target_val_reallocated']:,.0f}" if summary.get("earnings_reallocated") else "",
                    f"- ⚠️ 财报日历查询失败并默认放行: {summary['earnings_recheck_failed_symbols']}" if summary.get("earnings_recheck_failed_symbols") else "",
                    "- ⚠️ 财报日历查询降级（本轮未能完整复核，请留意后续手动核查）" if summary.get("earnings_recheck_degraded") else "",
                    "",
                    "## 卖单成交核实",
                    *issue_lines,
                    "",
                    "## 已提交买单（等待成交核实）" if buy_pending else "## 已确认完成的买入计划",
                    *order_lines,
                    *(["", "## 买单提交异常"] + failed_buy_lines if failed_buy_lines else []),
                    *(["", "⚠️ 待合并的 cycle_plan 与 pending_sell signal_date 不一致，两个周期被意外混合，请人工核查 state.json"]
                      if summary.get("signal_date_mixed") else []),
                ])]
            log.info(f"[phase=buy] 完成，status={status}")
            return

    # ── phase=execute（moc_single 路径）─────────────────────────────────────
        if args.phase == "execute":
            if EXEC_MODE != "moc_single":
                log.warning(f"⚠️ EXEC_MODE={EXEC_MODE}，非 moc_single，phase=execute 拒绝运行")
                status = "exec_mode_mismatch"
                email_lines = [build_skip_email_body({
                    "run_id": run_id, "status": status,
                    "conclusion": f"EXEC_MODE={EXEC_MODE}，不允许 phase=execute。请把 .env 中 EXEC_MODE 改为 moc_single 或改用 --phase sell/buy",
                    "equity": equity, "buying_power": buying_power, "drawdown": dd,
                    "positions": existing_positions,
                    "last_rebalance": state.get("last_rebalance"),
                    "cycle_plan_summary": "有" if state.get(CYCLE_PLAN_KEY) else "无",
                    "pending_sell_summary": "n/a",
                })]
                log.info(f"[phase=execute] 完成，status={status}")
                return
            summary = execute_moc_phase(client, state, run_id, args.dry_run, audit, earnings_allow)
            _save_state(state)
            if not summary["had_plan"]:
                status = "no_pending_plan"
                email_lines = [build_skip_email_body({
                    "run_id": run_id, "status": status,
                    "conclusion": "无待执行调仓计划（cycle_plan 为空）",
                    "equity": equity, "buying_power": buying_power, "drawdown": dd,
                    "positions": existing_positions,
                    "last_rebalance": state.get("last_rebalance"),
                    "cycle_plan_summary": "无", "pending_sell_summary": "n/a",
                })]
            elif summary.get("blocked_stale_pending"):
                status = "execute_blocked_stale_pending"
                email_lines = ["\n".join([
                    "## 今日结论：检测到旧调仓周期残留 state，本轮跳过执行",
                    f"- run_id: {run_id}",
                    "- ⚠️ pending_execute / pending_sell / pending_buy 之一非空",
                    "- 请人工检查上轮 reconcile 是否遗漏运行、broker 侧持仓与挂单，"
                      "确认后手动清理 state.json 再触发 --phase execute 补跑",
                ])]
            else:
                had_fail = bool(summary.get("submit_failed"))
                had_stop_triggered = bool(summary.get("stop_triggered_syms"))
                had_loo = bool(summary.get("loo_fallback"))
                status = (
                    "execute_submitted_with_issues" if had_fail else
                    "execute_submitted_with_loo_fallback" if had_loo else
                    "execute_submitted"
                )
                sell_lines = [f"- {cid}" for cid in summary["sell_orders"]] or ["- 无卖单"]
                buy_lines  = [f"- {cid}" for cid in summary["buy_orders"]] or ["- 无买单（MOC 主路径）"]
                loo_lines  = [f"- {cid}（次日开盘 LOO 兜底）" for cid in summary["loo_fallback"]]
                fail_lines = [f"- {f['symbol']} {f['kind']} × {f['qty']}"
                              for f in summary.get("submit_failed", [])]
                stop_lines = [f"- {s}（实时可用持仓 0，可能止损已触发，跳过）"
                              for s in summary.get("stop_triggered_syms", [])]
                email_lines = ["\n".join([
                    "## 今日结论：MOC 二腿已提交，等待 16:00 收盘拍卖成交"
                    + ("（部分标的转 LOO 兜底）" if had_loo else "")
                    + ("（存在提交异常，需人工复核）" if had_fail else ""),
                    f"- run_id: {run_id}",
                    f"- 提示: {summary['note']}" if summary["note"] else "",
                    f"- 📅 财报复核强制清仓: {summary['earnings_forced_syms']}"
                        if summary.get("earnings_forced_syms") else "",
                    "",
                    "## 已提交 SELL MOC",
                    *sell_lines,
                    "",
                    "## 已提交 BUY MOC",
                    *buy_lines,
                    *(["", "## LOO 兜底（次日开盘拍卖）"] + loo_lines if loo_lines else []),
                    *(["", "## ⚠️ 提交失败"] + fail_lines if fail_lines else []),
                    *(["", "## ⚠️ 实时持仓 0（止损可能已触发）"] + stop_lines if stop_lines else []),
                ])]
            log.info(f"[phase=execute] 完成，status={status}")
            return

    # ── phase=reconcile（moc_single 路径）───────────────────────────────────
        if args.phase == "reconcile":
            if EXEC_MODE != "moc_single":
                log.warning(f"⚠️ EXEC_MODE={EXEC_MODE}，非 moc_single，phase=reconcile 拒绝运行")
                status = "exec_mode_mismatch"
                _save_state(state)
                log.info(f"[phase=reconcile] 完成，status={status}")
                return
            summary = reconcile_moc_execute(client, state, run_id, args.dry_run, audit)
            _save_state(state)
            if not summary["had_pending"]:
                status = "no_pending_plan"
                email_lines = [build_skip_email_body({
                    "run_id": run_id, "status": status,
                    "conclusion": "无待核实调仓（pending_execute 为空）",
                    "equity": equity, "buying_power": buying_power, "drawdown": dd,
                    "positions": existing_positions,
                    "last_rebalance": state.get("last_rebalance"),
                    "cycle_plan_summary": "n/a", "pending_sell_summary": "n/a",
                })]
            else:
                unfilled = summary["sells_unfilled"] or summary["buys_unfilled"]
                status = "reconcile_incomplete" if unfilled else "reconcile_completed"
                if summary["note"]:
                    status = "reconcile_naked_short"
                filled_sells = [f"- {r['symbol']} × {r['qty']}" for r in summary["sells_filled"]] or ["- 无"]
                filled_buys  = [f"- {r['symbol']} × {r['qty']}（via {r.get('filled_via')}）"
                                for r in summary["buys_filled"]] or ["- 无"]
                unfilled_lines = ([f"- SELL {r['symbol']} × {r['qty']} status={r.get('status')}"
                                    for r in summary["sells_unfilled"]] +
                                  [f"- BUY  {r['symbol']} × {r['qty']} moc={r.get('moc_status')} loo={r.get('loo_status', 'n/a')}"
                                    for r in summary["buys_unfilled"]])
                stop_lines = [f"- {s}" for s in summary["stop_added"]] or ["- 无新增"]
                email_lines = ["\n".join([
                    "## 今日结论：MOC 调仓周期核实" +
                    ("完成" if not unfilled else "存在未成交（需人工检查）") +
                    ("（🚨 发现负持仓）" if summary["note"] else ""),
                    f"- run_id: {run_id}",
                    f"- 提示: {summary['note']}" if summary["note"] else "",
                    "",
                    "## SELL 已成交",
                    *filled_sells,
                    "",
                    "## BUY 已成交",
                    *filled_buys,
                    *(["", "## ⚠️ 未成交（保留 pending_execute 供人工核查）"] + unfilled_lines
                      if unfilled_lines else []),
                    "",
                    "## 新挂止损单",
                    *stop_lines,
                ])]
            log.info(f"[phase=reconcile] 完成，status={status}")
            return

    # ── phase=plan：上一周期未完成则不重新计算 ────────────────────────────────
        if args.phase == "plan" and has_incomplete_cycle(state):
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
                "warning": "cycle_plan、pending_sell 或 pending_buy 尚未清空，请人工检查 state.json 并确认上一轮周期状态。",
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
                f"⚠️ 财报日历查询失败并默认放行: {plan['earnings_failed_symbols']}"
                if plan.get("earnings_failed_symbols") else "",
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
        if should_escalate_to_error(status):
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
        if status in NO_EMAIL_STATUSES:
            log.info(f"status={status} 在免打扰名单中，不发送日报邮件")
        else:
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

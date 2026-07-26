#!/usr/bin/env python3
"""
repair_state.py —— state.json 半自动修复工具（HANDOFF §7.3）

用途：state.json 被污染/损坏（缺 high_watermark / last_rebalance / kill_switch
关键键，F2 校验会拒绝加载）时，对照 state.json.bak 与 broker 真实状态生成
一份合法的修复方案，人工确认后写回。

默认只打印方案不落盘（dry-run）；加 --yes 才写文件，写之前会把现有
state.json 备份为 state.json.pre-repair-<时间戳>。

关键键的修复取值规则（保守优先）：
  high_watermark  max(bak 值, 当前账户净值)——宁可偏高让 Kill Switch 更早触发，
                  也不能偏低抬高熔断基准；可用 --high-watermark 人工指定
  last_rebalance  取 bak 值；bak 也没有则为 None（下次运行视为可调仓），
                  可用 --last-rebalance YYYY-MM-DD 人工指定
  kill_switch     取 bak 值；bak 也没有则 False。bak 为 true 时保留 true
                  （熔断状态必须人工显式清零，本工具不代清）

pending_buy / pending_sell / pending_execute 中的每笔订单都会拿
client_order_id 去 broker 核实：
  - 404 / canceled / expired / rejected → 终态，从 pending 中剔除
  - filled → 终态，剔除并提醒核对持仓与止损单
  - 其余（new / accepted / partially_filled ...）→ 在途，保留

用法：
  python3 tools/repair_state.py            # 只看方案
  python3 tools/repair_state.py --yes      # 确认写回
"""

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

LIVE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(LIVE_DIR))

from dotenv import load_dotenv          # noqa: E402
from alpaca.trading.client import TradingClient          # noqa: E402
from alpaca.trading.requests import GetOrdersRequest      # noqa: E402
from alpaca.trading.enums import QueryOrderStatus         # noqa: E402

load_dotenv(LIVE_DIR / ".env")

STATE_FILE = Path(os.getenv("TRADE_QUANT_STATE_FILE", str(LIVE_DIR / "state.json")))
STATE_BACKUP_FILE = Path(os.getenv("TRADE_QUANT_STATE_BACKUP_FILE",
                                   str(LIVE_DIR / "state.json.bak")))

API_KEY = os.getenv("ALPACA_API_KEY", "")
SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "")
PAPER = os.getenv("ALPACA_PAPER", "true").lower() != "false"

CRITICAL_KEYS = ("high_watermark", "last_rebalance", "kill_switch")
# broker 侧订单终态：这些状态的订单不会再变化，可以安全从 pending 中剔除
TERMINAL_STATUSES = {"filled", "canceled", "cancelled", "expired", "rejected",
                     "done_for_day", "stopped", "replaced"}


def _read_json(path: Path):
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else None
    except Exception as e:
        print(f"  ⚠️ {path.name} 解析失败：{e}")
        return None


def _check_order(client: TradingClient, cid: str):
    """返回 (status, filled_qty, note)。status=None 表示 broker 侧不存在（404）。"""
    try:
        o = client.get_order_by_client_id(cid)
        status = getattr(getattr(o, "status", ""), "value", str(getattr(o, "status", "")))
        filled = float(getattr(o, "filled_qty", 0) or 0)
        return status.lower(), filled, ""
    except Exception as e:
        msg = str(e)
        if "not found" in msg.lower() or "40410000" in msg:
            return None, 0.0, "broker 无此订单（从未被接受或已被清理）"
        # 网络/权限等不确定错误：不能当作不存在处理
        return "query_error", 0.0, msg


def _audit_pending_orders(client: TradingClient, records: list, id_key: str = "client_order_id"):
    """核实一组 pending 订单，返回 (保留列表, 剔除报告列表)。"""
    keep, dropped = [], []
    for rec in records:
        cid = rec.get(id_key, "")
        if not cid:
            dropped.append({**rec, "_reason": "记录缺 client_order_id，无法核实，剔除"})
            continue
        status, filled, note = _check_order(client, cid)
        if status is None:
            dropped.append({**rec, "_reason": f"404：{note}"})
        elif status in TERMINAL_STATUSES:
            reason = f"终态 {status}（filled={filled:g}）"
            if status == "filled":
                reason += " ⚠️ 已成交：请核对持仓记账与止损单是否就位"
            dropped.append({**rec, "_reason": reason})
        elif status == "query_error":
            keep.append(rec)
            print(f"  ⚠️ {cid} 查询失败（{note}），保守保留，请稍后重试")
        else:
            keep.append(rec)
            print(f"  ℹ️ {cid} 仍在途（{status}），保留")
    return keep, dropped


def main():
    ap = argparse.ArgumentParser(description="state.json 半自动修复")
    ap.add_argument("--yes", action="store_true", help="确认写回（默认只打印方案）")
    ap.add_argument("--high-watermark", type=float, default=None,
                    help="人工指定 high_watermark（覆盖自动取值）")
    ap.add_argument("--last-rebalance", type=str, default=None,
                    help="人工指定 last_rebalance（YYYY-MM-DD，覆盖自动取值）")
    args = ap.parse_args()

    print(f"state 文件：{STATE_FILE}")
    state = _read_json(STATE_FILE) or {}
    bak = _read_json(STATE_BACKUP_FILE) or {}

    if not API_KEY or not SECRET_KEY:
        print("❌ .env 缺 ALPACA_API_KEY / ALPACA_SECRET_KEY，无法核实 broker 状态")
        sys.exit(1)
    client = TradingClient(API_KEY, SECRET_KEY, paper=PAPER)
    account = client.get_account()
    equity = float(account.equity)
    positions = client.get_all_positions()
    print(f"broker：模式={'Paper' if PAPER else 'Live'}  净值=${equity:,.2f}  "
          f"持仓={[(p.symbol, p.qty) for p in positions] or '无'}")

    repaired = dict(state)
    changes = []

    # ── 关键键补齐 ────────────────────────────────────────────────────────
    missing = [k for k in CRITICAL_KEYS if k not in state]
    if missing:
        print(f"\n缺失关键键：{missing}")
    if "high_watermark" not in state:
        if args.high_watermark is not None:
            hwm = args.high_watermark
            src = "命令行指定"
        else:
            bak_hwm = bak.get("high_watermark")
            hwm = max(float(bak_hwm), equity) if bak_hwm is not None else equity
            src = f"max(bak={bak_hwm}, 当前净值={equity:,.2f})" if bak_hwm is not None \
                else f"当前净值 {equity:,.2f}（bak 无记录，建议人工核实历史最高净值）"
        repaired["high_watermark"] = hwm
        changes.append(f"high_watermark ← {hwm}  （{src}）")
    if "last_rebalance" not in state:
        if args.last_rebalance is not None:
            lr = args.last_rebalance
            datetime.strptime(lr, "%Y-%m-%d")  # 校验格式
            src = "命令行指定"
        else:
            lr = bak.get("last_rebalance")
            src = "bak" if "last_rebalance" in bak else "无来源，置 None（下次运行视为可调仓）"
        repaired["last_rebalance"] = lr
        changes.append(f"last_rebalance ← {lr}  （{src}）")
    if "kill_switch" not in state:
        ks = bool(bak.get("kill_switch", False))
        repaired["kill_switch"] = ks
        changes.append(f"kill_switch ← {ks}  （{'bak' if 'kill_switch' in bak else '默认 False'}）")
        if ks:
            print("  🚨 bak 中 kill_switch=true，将保留熔断状态；确认恢复交易需人工改回 false")

    # ── pending 订单逐笔核实 ──────────────────────────────────────────────
    for block_key, list_path in (("pending_buy", "orders"), ("pending_sell", "orders")):
        block = state.get(block_key)
        if not block:
            continue
        print(f"\n核实 {block_key}：")
        keep, dropped = _audit_pending_orders(client, block.get(list_path, []))
        for d in dropped:
            changes.append(f"{block_key} 剔除 {d.get('symbol')}×{d.get('qty')}：{d['_reason']}")
        if keep:
            repaired[block_key] = {**block, list_path: keep}
        else:
            repaired.pop(block_key, None)
            changes.append(f"{block_key} 全部订单终态/不存在 → 整块移除")

    pe = state.get("pending_execute")
    if pe:
        print("\n核实 pending_execute：")
        keep_s, drop_s = _audit_pending_orders(client, pe.get("sells", []))
        keep_b, drop_b = _audit_pending_orders(client, pe.get("buys", []))
        for d in drop_s + drop_b:
            changes.append(f"pending_execute 剔除 {d.get('symbol')}×{d.get('qty')}：{d['_reason']}")
        if keep_s or keep_b:
            repaired["pending_execute"] = {**pe, "sells": keep_s, "buys": keep_b}
        else:
            repaired.pop("pending_execute", None)
            changes.append("pending_execute 全部订单终态/不存在 → 整块移除"
                           "（⚠️ 若其中有 filled 买单，请确认止损已挂：跑一次 --phase reconcile）")

    # ── 输出方案 ─────────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    if not changes:
        print("✅ state.json 无需修复（关键键齐全，无可剔除的 pending 订单）")
        return
    print("修复方案：")
    for c in changes:
        print(f"  · {c}")
    print("\n修复后的 state.json：")
    print(json.dumps(repaired, indent=2, ensure_ascii=False, default=str))

    if not args.yes:
        print("\n（dry-run：未写盘。确认无误后加 --yes 执行）")
        return

    if STATE_FILE.exists():
        backup = STATE_FILE.with_name(
            f"state.json.pre-repair-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
        backup.write_text(STATE_FILE.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"\n已备份原文件 → {backup.name}")
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(repaired, indent=2, ensure_ascii=False, default=str),
                   encoding="utf-8")
    os.replace(tmp, STATE_FILE)
    print(f"✅ 已写回 {STATE_FILE}")


if __name__ == "__main__":
    main()

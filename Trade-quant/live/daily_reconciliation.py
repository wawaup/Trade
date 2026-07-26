#!/usr/bin/env python3
"""
daily_reconciliation.py —— 账户级每日对账（FIX_PLAN H5 兜底，2026-07-27）

独立 cron（15:55 ET，两种 EXEC_MODE 都注册）：盘后比对 broker 真实状态与
state.json 记账，任一差异立即紧急邮件 + 退出码 1。与 service_watchdog.py
职责分离：watchdog 只看运行新鲜度，本脚本只看账户一致性。

对账项（每一项都对应一次真实事故或审计发现的风险路径）：
  1. 负持仓                    —— 任何 qty<0 都是裸空，CRITICAL
  2. 单票集中度超限            —— 市值/净值 > MAX_POSITION_PCT（2026-07-09 DOCN
                                  678 股 ≈$99k 事故的直接防线）
  3. 持仓缺止损保护            —— ENABLE_STOP_ORDERS 时每仓必须有活跃 tq-stop-* 单
  4. 孤儿/超量止损             —— 止损单无对应持仓（触发即开空）或数量超过持仓
  5. 意外挂单                  —— client_order_id 不带 tq- 前缀（人工/其它程序下单）
  6. kill_switch 一致性        —— state 已熔断但账户仍有持仓/非止损挂单
  7. 高水位新鲜度              —— equity 明显高于 high_watermark，说明主 cron
                                  没在更新 state（服务可能停摆）
  8. pending 陈旧              —— pending_execute/sell/buy 超过 3 天未被消费

用法：
  python3 daily_reconciliation.py             # 对账并（有差异时）发邮件
  python3 daily_reconciliation.py --no-email  # 本地手动核查，只打印
"""

import argparse
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# 复用主程序的配置解析 / state 加载（F2 校验语义）/ 邮件 / 订单判活，
# 保证对账口径与交易口径永远一致。
import alpaca_trader as trader  # noqa: E402

STOP_PREFIX = "tq-stop-"
KNOWN_PREFIX = "tq-"                 # 本系统所有订单的 client_order_id 前缀
EQUITY_TOL_PCT = 0.005               # 高水位新鲜度容差 ±0.5%
CONCENTRATION_TOL = 1.05             # 集中度超限容差（避免涨跌导致的边界误报）
PENDING_STALE_DAYS = 3               # pending 块超过该天数未消费视为陈旧


def build_report(state: dict, equity: float, positions: list, open_orders: list,
                 *, max_position_pct: float, enable_stops: bool,
                 today: date) -> dict:
    """纯函数：输入快照，输出差异清单。不做任何网络/文件 IO，便于单测。

    positions 元素需有 symbol / qty / market_value 属性；
    open_orders 元素需有 symbol / qty / side / client_order_id / status 属性。
    """
    issues: list = []      # 任一条即紧急邮件
    notes: list = []       # 不报警但写进日报正文的观察项

    qty_map = {p.symbol: int(float(getattr(p, "qty", 0) or 0)) for p in positions}
    mv_map = {p.symbol: float(getattr(p, "market_value", 0) or 0) for p in positions}

    active_orders = [o for o in open_orders if trader._order_is_active(o)]
    stop_orders = [o for o in active_orders
                   if str(getattr(o, "client_order_id", "") or "").startswith(STOP_PREFIX)]
    foreign_orders = [o for o in active_orders
                      if not str(getattr(o, "client_order_id", "") or "").startswith(KNOWN_PREFIX)]

    # 1. 负持仓
    for sym, qty in qty_map.items():
        if qty < 0:
            issues.append(f"负持仓：{sym} qty={qty}（裸空，须立即人工处理）")

    # 2. 单票集中度（DOCN 678 股事故防线）
    if equity > 0:
        for sym, mv in mv_map.items():
            pct = abs(mv) / equity
            if pct > max_position_pct * CONCENTRATION_TOL:
                issues.append(
                    f"集中度超限：{sym} 市值 ${abs(mv):,.0f} 占净值 {pct:.0%}，"
                    f"超过上限 {max_position_pct:.0%}（参照 2026-07-09 DOCN 事故）"
                )

    # 3. 持仓缺止损 / 4. 孤儿与超量止损
    stops_by_symbol: dict = {}
    for o in stop_orders:
        stops_by_symbol.setdefault(getattr(o, "symbol", ""), []).append(o)

    # 调仓周期在途时（execute 已撤止损、reconcile 还没补挂），缺止损属预期，降级为 note
    in_flight = bool(state.get("pending_execute") or state.get("pending_sell"))
    if enable_stops:
        for sym, qty in qty_map.items():
            if qty <= 0 or sym in stops_by_symbol:
                continue
            msg = f"持仓无止损保护：{sym} × {qty}"
            if in_flight:
                notes.append(msg + "（调仓在途，等 reconcile 补挂；次日仍缺则会报警）")
            else:
                issues.append(msg + "（无在途调仓可解释，止损可能被撤后未恢复）")

    for sym, orders in stops_by_symbol.items():
        held = qty_map.get(sym, 0)
        stop_qty = sum(int(float(getattr(o, "qty", 0) or 0)) for o in orders)
        if held <= 0:
            issues.append(f"孤儿止损：{sym} 无持仓但有活跃止损 ×{stop_qty}（触发即开空，应撤单）")
        elif stop_qty > held:
            issues.append(f"止损超量：{sym} 止损 ×{stop_qty} > 持仓 ×{held}（触发将开空）")
        elif stop_qty < held:
            notes.append(f"止损不足量：{sym} 止损 ×{stop_qty} < 持仓 ×{held}（部分仓位裸露）")

    # 5. 意外挂单
    for o in foreign_orders:
        issues.append(
            f"意外挂单：{getattr(o, 'symbol', '?')} {getattr(o, 'side', '?')} "
            f"×{getattr(o, 'qty', '?')} client_order_id={getattr(o, 'client_order_id', '')!r}"
            "（非本系统 tq- 前缀，疑似人工/其它程序下单）"
        )

    # 6. kill_switch 一致性
    if state.get("kill_switch"):
        non_stop_active = [o for o in active_orders if o not in stop_orders]
        if qty_map and any(q > 0 for q in qty_map.values()):
            issues.append(f"kill_switch=true 但仍有持仓：{ {s: q for s, q in qty_map.items() if q > 0} }")
        if non_stop_active:
            issues.append(f"kill_switch=true 但仍有非止损活跃挂单 {len(non_stop_active)} 笔")

    # 7. 高水位新鲜度（主 cron 停摆探测）
    hwm = state.get("high_watermark")
    if hwm and equity > float(hwm) * (1 + EQUITY_TOL_PCT):
        issues.append(
            f"高水位未更新：净值 ${equity:,.2f} 已高于 high_watermark ${float(hwm):,.2f} "
            f"超过 {EQUITY_TOL_PCT:.1%}，主策略 cron 可能未运行或 state 写入失败"
        )

    # 8. pending 陈旧
    for key, date_key in (("pending_execute", "execute_date"),
                          ("pending_sell", "sell_date"),
                          ("pending_buy", "submit_date")):
        block = state.get(key)
        if not block:
            continue
        raw = block.get(date_key)
        try:
            age = (today - date.fromisoformat(str(raw))).days if raw else None
        except ValueError:
            age = None
        if age is None:
            issues.append(f"{key} 存在但缺少/无法解析 {date_key}，无法判断新鲜度")
        elif age > PENDING_STALE_DAYS:
            issues.append(f"{key} 已滞留 {age} 天未被消费（{date_key}={raw}），下游阶段可能未运行")

    return {
        "issues": issues,
        "notes": notes,
        "equity": equity,
        "positions": qty_map,
        "active_stops": {s: sum(int(float(getattr(o, 'qty', 0) or 0)) for o in ol)
                         for s, ol in stops_by_symbol.items()},
        "foreign_order_count": len(foreign_orders),
    }


def format_report(report: dict) -> str:
    lines = [
        f"每日对账报告  {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"净值 ${report['equity']:,.2f}  持仓 {report['positions'] or '无'}  "
        f"活跃止损 {report['active_stops'] or '无'}",
        "",
    ]
    if report["issues"]:
        lines.append(f"🚨 差异 {len(report['issues'])} 项：")
        lines += [f"  {i+1}. {msg}" for i, msg in enumerate(report["issues"])]
    else:
        lines.append("✅ 全部一致，无差异")
    if report["notes"]:
        lines.append("")
        lines.append("观察项（不报警）：")
        lines += [f"  · {msg}" for msg in report["notes"]]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="账户级每日对账")
    ap.add_argument("--no-email", action="store_true", help="只打印，不发邮件")
    args = ap.parse_args()

    state = trader._load_state()   # F2 校验：state 损坏时这里硬失败，同样会让 cron 报警走出来
    client = trader.TradingClient(trader.API_KEY, trader.SECRET_KEY, paper=trader.PAPER)
    account = client.get_account()
    positions = client.get_all_positions()
    open_orders = client.get_orders(
        trader.GetOrdersRequest(status=trader.QueryOrderStatus.OPEN))

    report = build_report(
        state, float(account.equity), positions, open_orders,
        max_position_pct=trader.MAX_POSITION_PCT,
        enable_stops=trader.ENABLE_STOP_ORDERS,
        today=date.today(),
    )
    text = format_report(report)
    print(text)

    if report["issues"] and not args.no_email:
        mode = "Paper" if trader.PAPER else "Live"
        trader.send_email(f"Trade Quant {mode} 紧急报警-每日对账差异 ×{len(report['issues'])}", text)
    return 1 if report["issues"] else 0


if __name__ == "__main__":
    sys.exit(main())

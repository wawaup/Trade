"""
strategy_params.py —— 策略核心阈值的单一来源（P5 单源化，2026-07-17）

此前 TOP_N / MIN_SCORE / VOL_MIN / REBALANCE_DAYS / KILL_DD 在三处重复定义
（live/alpaca_trader.py、research/order_feasibility_audit.py、
research/factor_combo_backtest.py 的 argparse 默认值），任何一处改动都可能
造成回测与实盘口径悄悄漂移。现在三处全部从这里导入。

改这些值 = 同时改变回测口径和实盘行为，必须在 RESEARCH_LOG 记录消融依据。
"""

REBALANCE_DAYS = 5      # 每 N 交易日调仓一次
TOP_N          = 5      # 持仓上限（Top-3/5/7/10/15 Sharpe 相同：双过滤后候选常 ≤3 只）
MIN_SCORE      = 1.0    # Combo Score 入场门槛
VOL_MIN        = 1.2    # Vol_Shock 放量倍数（相对倍数，兼容 IEX 偏小的绝对成交量）
KILL_DD        = -0.30  # Kill Switch 触发阈值（从账户高水位回撤 30%，静默期后可复出）

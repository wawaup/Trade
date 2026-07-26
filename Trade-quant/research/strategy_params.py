"""
strategy_params.py —— 策略核心参数的单一来源

## v2（当前生产策略，RESEARCH_LOG §15.7/15.7b，2026-07-27 冻结）

四因子等权 z 合成 + Top-20 + 单票 10% + 无绝对过滤 + bear_flat_45 +
熔断 −30%（静默 20 日可复出）+ 目标波动率 0.25 + 10 日调仓。
全样本 +29.5%/1.29/−21.8%；OOS(2023+) +46.9%/1.76/−15.1%；零熔断。
live/alpaca_trader.py 从这里导入 V2_* 常量。

改任何 V2_* 值 = 改变生产策略，必须先过 §15.6 门槛
（walk-forward 两个子样本风险调整 ≥ 月度等权+VT0.25）并记录 RESEARCH_LOG。

## v1（legacy，已封存，§15.2 判定为 2022 过拟合）

仅供 factor_combo_backtest.py 默认参数复现历史结果用，不得用于生产。
"""

# ── v2 生产参数（2026-07-27 冻结）───────────────────────────────────────────
V2_FACTOR_WEIGHTS = {          # 等权 z 合成；因子定义见 factor_scanner.compute_factors
    "Mom_12_1":  0.25,         # 12-1 月动量（Jegadeesh-Titman）
    "Prox_52W":  0.25,         # 距 52 周高点（George-Hwang 锚定）
    "LowVol_60": 0.25,         # 低波动异象（Ang et al.，因子内已取负号）
    "RS_Beta":   0.25,         # Beta 调整残差动量（v1 幸存者，§15.7 平反）
}
V2_TOP_N           = 20        # 持仓数（组合宽度是 §15.3 的核心教训）
V2_MAX_POSITION_PCT = 0.10     # 单票权重上限
V2_REBALANCE_DAYS  = 10        # 调仓周期（交易日）；慢信号配慢调仓
V2_BEAR_FLAT_DAYS  = 45        # 熊市（QQQ<MA50）连续天数 ≤ 此值时空仓
V2_VOL_TARGET      = 0.25      # 目标年化波动率；exposure ×= min(1, target/realized)
V2_VOL_SPAN        = 20        # 实现波动 EWMA 窗口（交易日）
# v2 无 MIN_SCORE / VOL_MIN 绝对过滤（§15.3 判定为头号 alpha 杀手）；
# 无 choppy_half / soft_drawdown（§15.7b 消融为零贡献）。

# ── 共用风控 ────────────────────────────────────────────────────────────────
KILL_DD        = -0.30  # Kill Switch 触发阈值（从账户高水位回撤 30%，静默期后可复出）
COOLDOWN_DAYS  = 20     # 熔断静默期最少交易日（或 QQQ 回 MA50 任一满足复出）

# ── v1 legacy（封存，仅供回测复现；生产禁用）────────────────────────────────
REBALANCE_DAYS = 5      # v1 调仓周期
TOP_N          = 5      # v1 持仓上限
MIN_SCORE      = 1.0    # v1 Combo 入场门槛（§15.3：OOS 头号杀手）
VOL_MIN        = 1.2    # v1 放量过滤（同上）

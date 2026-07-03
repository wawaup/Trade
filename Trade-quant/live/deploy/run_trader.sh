#!/bin/bash
# GCP VM 每日执行包装脚本
# 设置：chmod +x run_trader.sh
#
# 三阶段调仓（全部为美东时间 ET，由服务器时区 America/New_York + CRON_TZ 保证）：
#   plan — 16:05 ET（T 日收盘后）  计算目标持仓与买卖计划，写入 state.json，不下单
#          $PYTHON alpaca_trader.py --phase plan
#   sell — 15:50 ET（T+1 尾盘前） 读取计划，主动让价到买一价提交限价卖单
#          $PYTHON alpaca_trader.py --phase sell
#   buy  — 09:15 ET（T+2 开盘前） 核实卖单成交后，用实际可用资金提交买单
#          $PYTHON alpaca_trader.py --phase buy
# 三段之间用真实交易日（节假日感知）衔接，不是固定的周几；每个交易日都会触发，
# 脚本内部自行判断当天是否需要动作（非目标日快速返回）。
# 具体 cron 时间由 deploy/cron_setup.sh 统一注册，勿在此处硬编码时间表。
#
# LULD 熔断重试：09:45 ET （见 cron_setup.sh 中 HALT_RETRY_CMD）
# 服务失效监控：20:20 ET （见 cron_setup.sh 中 WATCHDOG_CMD）

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="${VENV_PYTHON:-python3}"
TIMEOUT_SEC="${TRADER_TIMEOUT_SEC:-600}"

cd "$SCRIPT_DIR"

export PYTHONUNBUFFERED=1

echo "=============================="
echo "$(date '+%Y-%m-%d %H:%M:%S %Z') 策略执行开始"
echo "=============================="

# 超时兜底：防止进程真正 hang 住既不退出也不写 paper_runs.csv，watchdog 因此永远看不到失败记录
timeout "$TIMEOUT_SEC" $PYTHON alpaca_trader.py "$@"

echo "$(date '+%Y-%m-%d %H:%M:%S %Z') 执行完成"

#!/bin/bash
# GCP VM 每日执行包装脚本
# 设置：chmod +x run_trader.sh
# Cron（美东 16:35）：
#   夏令时（EDT = UTC-4）：35 20 * * 1-5
#   标准时（EST = UTC-5）：35 21 * * 1-5
#   保守写法（全年在 21:35 UTC，偶尔在夏天延迟 1 小时，无碍）：
#   35 21 * * 1-5 /path/to/run_trader.sh >> /var/log/trader_cron.log 2>&1
#   服务失效监控可在主任务之后额外运行：
#   20 22 * * 1-5 cd /path/to/live && python3 service_watchdog.py >> /var/log/trader_watchdog.log 2>&1

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="${VENV_PYTHON:-python3}"

cd "$SCRIPT_DIR"

export PYTHONUNBUFFERED=1

echo "=============================="
echo "$(date '+%Y-%m-%d %H:%M:%S %Z') 策略执行开始"
echo "=============================="

$PYTHON alpaca_trader.py "$@"

echo "$(date '+%Y-%m-%d %H:%M:%S %Z') 执行完成"

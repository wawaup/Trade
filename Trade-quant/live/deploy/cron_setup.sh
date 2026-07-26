#!/bin/bash
# GCP VM 部署 & Cron 设置向导
# 在 GCP VM 上首次执行：bash cron_setup.sh
# 前置条件：VM 已克隆代码、Python 3.10+

set -e

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENV_DIR="$PROJECT_ROOT/venv"
LOG_DIR="/var/log/trader"
PYTHON_BIN="$VENV_DIR/bin/python"
PIP_BIN="$VENV_DIR/bin/pip"

echo "=============================="
echo " Alpaca Trader GCP 部署向导"
echo " 项目路径：$PROJECT_ROOT"
echo "=============================="

# ---------- 1. 创建虚拟环境 ----------
if [ -d "$VENV_DIR" ] && { [ ! -x "$PYTHON_BIN" ] || [ ! -x "$PIP_BIN" ]; }; then
    echo "[1/5] 检测到虚拟环境不完整，重建..."
    rm -rf "$VENV_DIR"
fi

if [ ! -d "$VENV_DIR" ]; then
    echo "[1/5] 创建 Python 虚拟环境..."
    python3 -m venv "$VENV_DIR"
else
    echo "[1/5] 虚拟环境已存在且完整，跳过"
fi

# ---------- 2. 安装依赖 ----------
echo "[2/5] 安装 live 目录依赖..."
"$PYTHON_BIN" -m pip install -q --upgrade pip
"$PYTHON_BIN" -m pip install -q -r "$PROJECT_ROOT/requirements.txt"
echo "      live 交易脚本不默认安装完整 research 依赖（vectorbt/quantstats/jupyter/pandas-ta 等仅用于本地研究）"

# ---------- 3. 配置 .env ----------
echo "[3/5] 检查 .env 配置..."
ENV_FILE="$PROJECT_ROOT/.env"
if [ ! -f "$ENV_FILE" ]; then
    cp "$PROJECT_ROOT/.env.example" "$ENV_FILE"
    echo "  ⚠️  已创建 $ENV_FILE"
    echo "  ⚠️  请编辑并填入真实 API Keys，然后重新运行本脚本"
    echo "      nano $ENV_FILE"
    exit 1
fi
if grep -q "YOUR_API_KEY_HERE" "$ENV_FILE"; then
    echo "  ❌ .env 中仍有占位符，请填入真实 API Keys"
    echo "      nano $ENV_FILE"
    exit 1
fi
chmod 600 "$ENV_FILE"
echo "      .env 已配置（权限已收紧为 600）"

# ---------- 4. 创建日志目录 ----------
echo "[4/5] 创建日志目录 $LOG_DIR..."
sudo mkdir -p "$LOG_DIR"
sudo chown "$(whoami)" "$LOG_DIR"
chmod 750 "$LOG_DIR"
touch "$LOG_DIR/trader.log"

STATE_FILE="$PROJECT_ROOT/state.json"
if [ -f "$STATE_FILE" ]; then
    chmod 600 "$STATE_FILE"
fi

# ---------- 5. 注册 Cron Job ----------
echo "[5/5] 注册 Cron Job（主策略 + 股票池监控 + 服务健康检查）..."

# ⚠️  时区前置要求：所有 Cron 时间均以美东时间（ET）表达，与夏/冬令时无关。
#      首次部署前必须执行：sudo timedatectl set-timezone America/New_York
#      验证：timedatectl | grep "Time zone"  # 应显示 America/New_York
CURRENT_TZ=$(timedatectl show --property=Timezone --value 2>/dev/null || cat /etc/timezone 2>/dev/null || echo "unknown")
if [ "$CURRENT_TZ" != "America/New_York" ]; then
    echo ""
    echo "  ⚠️  当前服务器时区为 '$CURRENT_TZ'，建议改为 America/New_York："
    echo "      sudo timedatectl set-timezone America/New_York"
    echo "  （不改时区会导致夏令时切换时 LULD 重试任务在盘前错误触发）"
    echo "  继续部署（使用当前时区）..."
    echo ""
fi

RUNNER="$PROJECT_ROOT/deploy/run_trader.sh"
chmod +x "$RUNNER"

# 从 .env 读取 EXEC_MODE，决定注册哪套调仓时序：
#   three_phase（默认，向后兼容）：plan(T 16:05) → sell(T+1 15:50) → buy(T+2 09:15)
#   moc_single（推荐，FIX_PLAN §3 方案 A+）：plan(T 16:05) → execute(T+1 15:35) → reconcile(T+2 09:45)
EXEC_MODE_CONF=$(grep -E "^EXEC_MODE=" "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2 | tr -d '"' | tr -d "'" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')
EXEC_MODE_CONF=${EXEC_MODE_CONF:-three_phase}
if [ "$EXEC_MODE_CONF" != "three_phase" ] && [ "$EXEC_MODE_CONF" != "moc_single" ]; then
    echo "  ⚠️  .env 中 EXEC_MODE=$EXEC_MODE_CONF 无效，回退到 three_phase"
    EXEC_MODE_CONF="three_phase"
fi
echo "      EXEC_MODE=$EXEC_MODE_CONF"

# 所有时间均为美东时间（ET）。CRON_TZ 控制 cron 触发时间，TZ 传入脚本运行环境。
UNIVERSE_CMD="10 16 * * 1-5 cd $PROJECT_ROOT && $PYTHON_BIN universe_monitor.py >> $LOG_DIR/universe_monitor.log 2>&1"

# ── 共通：plan（T 收盘后 16:05）+ watchdog（20:20）+ 每日对账（15:55）─────────
PLAN_CMD="05 16 * * 1-5 TZ=America/New_York VENV_PYTHON=$PYTHON_BIN $RUNNER --phase plan >> $LOG_DIR/trader.log 2>&1"
WATCHDOG_CMD="20 20 * * 1-5 cd $PROJECT_ROOT && $PYTHON_BIN service_watchdog.py >> $LOG_DIR/watchdog.log 2>&1"
# 账户级每日对账（FIX_PLAN H5）：broker 持仓/挂单/净值 vs state.json，差异即紧急邮件。
# 两种 EXEC_MODE 都注册；与 watchdog 职责分离（watchdog 只看运行新鲜度）。
RECON_DAILY_CMD="55 15 * * 1-5 TZ=America/New_York cd $PROJECT_ROOT && $PYTHON_BIN daily_reconciliation.py >> $LOG_DIR/reconciliation.log 2>&1"

if [ "$EXEC_MODE_CONF" = "moc_single" ]; then
    # ── MOC 单段路径 ─────────────────────────────────────────────────────
    # execute — T+1 15:35 ET：提交 SELL MOC + BUY MOC 二腿（Alpaca MOC 硬截止 15:50，留 15 分钟余量）
    EXECUTE_CMD="35 15 * * 1-5 TZ=America/New_York VENV_PYTHON=$PYTHON_BIN $RUNNER --phase execute >> $LOG_DIR/trader.log 2>&1"
    # reconcile — T+2 09:45 ET：核实 MOC / LOO 成交、补挂止损、清理 state
    RECONCILE_CMD="45 09 * * 1-5 TZ=America/New_York VENV_PYTHON=$PYTHON_BIN $RUNNER --phase reconcile >> $LOG_DIR/trader.log 2>&1"
    SELL_CMD=""
    CRON_CMD=""
    HALT_RETRY_CMD=""
else
    # ── 三段式路径（历史/默认）──────────────────────────────────────────
    # sell — T+1 15:50 ET
    SELL_CMD="50 15 * * 1-5 TZ=America/New_York VENV_PYTHON=$PYTHON_BIN $RUNNER --phase sell >> $LOG_DIR/trader.log 2>&1"
    # buy  — T+2 09:15 ET
    CRON_CMD="15 09 * * 1-5 TZ=America/New_York VENV_PYTHON=$PYTHON_BIN $RUNNER --phase buy >> $LOG_DIR/trader.log 2>&1"
    # 买单成交核实 + LULD 熔断重试 — 09:45 ET
    HALT_RETRY_CMD="45 09 * * 1-5 cd $PROJECT_ROOT && $PYTHON_BIN alpaca_trader.py --retry-halted >> $LOG_DIR/trader.log 2>&1"
    EXECUTE_CMD=""
    RECONCILE_CMD=""
fi

CRON_BEGIN="# TRADE_QUANT_CRON_BEGIN"
CRON_END="# TRADE_QUANT_CRON_END"

# 先移除本脚本管理的旧定时任务区块，再添加最新区块；保留用户其它 crontab 内容。
TMPFILE=$(mktemp)
crontab -l 2>/dev/null | awk -v begin="$CRON_BEGIN" -v end="$CRON_END" '
    $0 == begin { skip=1; next }
    $0 == end { skip=0; next }
    skip != 1 { print }
' > "$TMPFILE" || true
echo "$CRON_BEGIN" >> "$TMPFILE"
echo "CRON_TZ=America/New_York" >> "$TMPFILE"
echo "TZ=America/New_York" >> "$TMPFILE"
echo "$UNIVERSE_CMD" >> "$TMPFILE"
echo "$PLAN_CMD" >> "$TMPFILE"
[ -n "$EXECUTE_CMD" ] && echo "$EXECUTE_CMD" >> "$TMPFILE"
[ -n "$RECONCILE_CMD" ] && echo "$RECONCILE_CMD" >> "$TMPFILE"
[ -n "$SELL_CMD" ] && echo "$SELL_CMD" >> "$TMPFILE"
[ -n "$CRON_CMD" ] && echo "$CRON_CMD" >> "$TMPFILE"
[ -n "$HALT_RETRY_CMD" ] && echo "$HALT_RETRY_CMD" >> "$TMPFILE"
echo "$RECON_DAILY_CMD" >> "$TMPFILE"
echo "$WATCHDOG_CMD" >> "$TMPFILE"
echo "$CRON_END" >> "$TMPFILE"
crontab "$TMPFILE"
rm -f "$TMPFILE"

echo ""
echo "=============================="
echo "✅ 部署完成"
echo ""
echo "已注册 Cron（EXEC_MODE=$EXEC_MODE_CONF）："
echo "  $UNIVERSE_CMD"
echo "  $PLAN_CMD"
[ -n "$EXECUTE_CMD" ] && echo "  $EXECUTE_CMD"
[ -n "$RECONCILE_CMD" ] && echo "  $RECONCILE_CMD"
[ -n "$SELL_CMD" ] && echo "  $SELL_CMD"
[ -n "$CRON_CMD" ] && echo "  $CRON_CMD"
[ -n "$HALT_RETRY_CMD" ] && echo "  $HALT_RETRY_CMD"
echo "  $RECON_DAILY_CMD"
echo "  $WATCHDOG_CMD"
echo ""
echo "下一步："
echo "  1. 验证连通性："
echo "       $PYTHON_BIN $PROJECT_ROOT/alpaca_verify.py"
echo ""
echo "  2. 首次 dry-run（预览今日信号，不下单）："
echo "       $PYTHON_BIN $PROJECT_ROOT/alpaca_trader.py --dry-run --force"
echo ""
echo "  3. 查看实时日志："
echo "       tail -f $LOG_DIR/trader.log"
echo "       tail -f $LOG_DIR/universe_monitor.log"
echo "       tail -f $LOG_DIR/watchdog.log"
echo ""
if [ "$EXEC_MODE_CONF" = "moc_single" ]; then
    echo "  4. 手动触发一次（MOC 单段路径）："
    echo "       $RUNNER --phase plan       --force   # T 收盘后"
    echo "       $RUNNER --phase execute    --force   # T+1 尾盘前"
    echo "       $RUNNER --phase reconcile  --force   # T+2 开盘后 15 分钟"
else
    echo "  4. 手动触发一次（三段式路径）："
    echo "       $RUNNER --phase plan --force"
    echo "       $RUNNER --phase sell --force"
    echo "       $RUNNER --phase buy  --force"
    echo "     注意：不带 --phase（默认 both）会走旧版一次性 rebalance()，仅供 dry-run/测试使用。"
fi
echo ""
echo "  切换到 MOC 单段：在 .env 中设置 EXEC_MODE=moc_single 后重跑本脚本"
echo ""
echo "  5. 启动只读复盘 API（建议仅 127.0.0.1 + SSH tunnel）："
echo "       export REVIEW_API_TOKEN='change-me-long-random-token'"
echo "       $PYTHON_BIN $PROJECT_ROOT/review_api.py --host 127.0.0.1 --port 8765"
echo ""
echo "     本地建立隧道后访问："
echo "       ssh -L 8765:127.0.0.1:8765 <user>@<gcp-host>"
echo "       curl -H \"Authorization: Bearer \$REVIEW_API_TOKEN\" http://127.0.0.1:8765/review/latest"
echo "=============================="

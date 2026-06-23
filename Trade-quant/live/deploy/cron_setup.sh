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
echo "      .env 已配置"

# ---------- 4. 创建日志目录 ----------
echo "[4/5] 创建日志目录 $LOG_DIR..."
sudo mkdir -p "$LOG_DIR"
sudo chown "$(whoami)" "$LOG_DIR"
chmod 755 "$LOG_DIR"
touch "$LOG_DIR/trader.log"

# ---------- 5. 注册 Cron Job ----------
echo "[5/5] 注册 Cron Job（主策略 + 股票池监控 + 服务健康检查）..."

RUNNER="$PROJECT_ROOT/deploy/run_trader.sh"
chmod +x "$RUNNER"

CRON_CMD="35 21 * * 1-5 VENV_PYTHON=$PYTHON_BIN $RUNNER >> $LOG_DIR/trader.log 2>&1"
UNIVERSE_CMD="05 21 * * 1-5 cd $PROJECT_ROOT && $PYTHON_BIN universe_monitor.py >> $LOG_DIR/universe_monitor.log 2>&1"
WATCHDOG_CMD="20 22 * * 1-5 cd $PROJECT_ROOT && $PYTHON_BIN service_watchdog.py >> $LOG_DIR/watchdog.log 2>&1"
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
echo "$UNIVERSE_CMD" >> "$TMPFILE"
echo "$CRON_CMD" >> "$TMPFILE"
echo "$WATCHDOG_CMD" >> "$TMPFILE"
echo "$CRON_END" >> "$TMPFILE"
crontab "$TMPFILE"
rm -f "$TMPFILE"

echo ""
echo "=============================="
echo "✅ 部署完成"
echo ""
echo "已注册 Cron："
echo "  $UNIVERSE_CMD"
echo "  $CRON_CMD"
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
echo "  4. 手动触发一次（正式下单）："
echo "       $RUNNER --force"
echo ""
echo "  5. 启动只读复盘 API（建议仅 127.0.0.1 + SSH tunnel）："
echo "       export REVIEW_API_TOKEN='change-me-long-random-token'"
echo "       $PYTHON_BIN $PROJECT_ROOT/review_api.py --host 127.0.0.1 --port 8765"
echo ""
echo "     本地建立隧道后访问："
echo "       ssh -L 8765:127.0.0.1:8765 <user>@<gcp-host>"
echo "       curl -H \"Authorization: Bearer \$REVIEW_API_TOKEN\" http://127.0.0.1:8765/review/latest"
echo "=============================="

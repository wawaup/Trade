# QuantDinger 研究平台搭建计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 以 QuantDinger-main 为可视化平台，Trade-quant 为研究工具箱，实现 SPCXB 历史数据回测（带买卖点标注+资金曲线）和量化因子数据本地存储。

**Architecture:** QuantDinger 负责展示层（Docker 全栈：Postgres/Redis/Flask/前端），Trade-quant/strategies/ 存放可直接粘贴进 QuantDinger 策略编辑器的 ScriptStrategy 脚本，Trade-quant/research/ 存放离线因子计算和分析工具。两套代码共享同一份策略逻辑，但互相独立运行。

**Tech Stack:** Docker Compose, QuantDinger ScriptStrategy (on_bar/ctx API), ccxt (Binance Spot), pandas, pyarrow (Parquet), numpy, matplotlib

---

## 文件结构

```
QuantDinger-main/
  backend_api_python/.env          ← 新建（从 env.example 复制）

Trade-quant/
  strategies/
    spcxb_v1_script.py             ← 新建：QuantDinger ScriptStrategy（无外部依赖，可粘贴）
  research/
    requirements.txt               ← 新建：研究环境依赖
    fetch_ohlcv.py                 ← 新建：Binance历史K线下载
    compute_factors.py             ← 新建：KDJ/MACD/ATR/VWAP 批量计算
    factor_store.py                ← 新建：Parquet 读写封装
    01_factor_analysis.py          ← 新建：因子探索分析脚本
  data/
    spcxb_1h_factors.parquet       ← 运行后生成
```

---

## Task 1: QuantDinger Docker 启动配置

**Files:**
- Create: `QuantDinger-main/backend_api_python/.env`

- [ ] **Step 1: 复制 env.example 并配置最小启动参数**

```bash
cd /Users/admin/dev/Trade/QuantDinger-main
cp backend_api_python/env.example backend_api_python/.env
```

然后编辑 `.env`，把以下行替换（用 python3 生成 SECRET_KEY）：

```bash
SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))")
echo "SECRET_KEY=$SECRET_KEY" >> /dev/null  # 记录这个值
```

在 `backend_api_python/.env` 中找到并更新：
```
SECRET_KEY=<上面生成的值，替换原来的 quantdinger-secret-key-change-me>
ADMIN_USER=admin
ADMIN_PASSWORD=your_password_here
DATABASE_URL=postgresql://quantdinger:quantdinger123@postgres:5432/quantdinger
REDIS_HOST=redis
REDIS_PORT=6379
CCXT_DEFAULT_EXCHANGE=binance
```

- [ ] **Step 2: 启动 Docker 栈**

```bash
cd /Users/admin/dev/Trade/QuantDinger-main
docker-compose up -d
```

等待约 30 秒后检查：

```bash
docker-compose ps
```

期望输出：所有 service 状态为 `Up`（postgres、redis、backend、frontend）

- [ ] **Step 3: 验证服务正常**

```bash
curl -s http://localhost:8888/api/health | python3 -m json.tool
```

期望：`{"status": "ok"}` 或类似健康响应。

打开浏览器访问 `http://localhost:8888`，用 admin/your_password_here 登录。

- [ ] **Step 4: 验证 Binance 数据源可用**

在浏览器中：
1. 新建策略 → 选 Symbol: BTCUSDT，Market: Crypto，Exchange: Binance，Spot，Timeframe: 1H
2. 点击"Backtest"，选最近 30 天
3. 期望：能看到 K 线数据和空策略的资金曲线（从头到尾持平）

---

## Task 2: 验证 SPCXB B-Stock 数据获取

**Files:** 无需创建文件，仅 UI 操作验证

- [ ] **Step 1: 在 QuantDinger 中测试 SPCXB 数据**

在策略编辑器中：
- Symbol: `SPCXBUSDT`
- Market: `Crypto`
- Exchange: `Binance`（Spot 模式）
- Timeframe: `1H`
- Backtest 范围: 2024-01-01 to 2024-12-31

点击 Backtest，如果看到价格图表（不是报错），则数据源验证通过。

- [ ] **Step 2: 如果报错，改用 ccxt 直接验证**

```bash
cd /Users/admin/dev/Trade
pip install ccxt --quiet
python3 -c "
import ccxt
ex = ccxt.binance({'options': {'defaultType': 'spot'}})
bars = ex.fetch_ohlcv('SPCXBUSDT', '1h', limit=5)
print(f'Got {len(bars)} bars, last close: {bars[-1][4]}')
"
```

期望：输出 `Got 5 bars, last close: <价格>`。如果这步通过但 QuantDinger 仍然报错，检查 docker 网络是否能访问 Binance（国内需配代理）。

- [ ] **Step 3: 记录数据可用时间范围**

```bash
python3 -c "
import ccxt, datetime
ex = ccxt.binance({'options': {'defaultType': 'spot'}})
# 获取最早可用数据
bars = ex.fetch_ohlcv('SPCXBUSDT', '1d', since=int(datetime.datetime(2023,1,1).timestamp()*1000), limit=5)
if bars:
    print('最早日线数据:', datetime.datetime.utcfromtimestamp(bars[0][0]/1000))
else:
    print('无 2023 数据')
bars2 = ex.fetch_ohlcv('SPCXBUSDT', '1d', limit=1)
print('最新数据:', datetime.datetime.utcfromtimestamp(bars2[-1][0]/1000))
"
```

记录输出的时间范围，用于后续设置回测区间。

---

## Task 3: 编写 SPCXB 核心仓+T仓 ScriptStrategy

**Files:**
- Create: `Trade-quant/strategies/spcxb_v1_script.py`

这个文件是一个**纯文本脚本**，可以整体粘贴进 QuantDinger 的策略代码编辑框。脚本内不能有任何 `import`（QuantDinger safe_exec 只提供 `np` 和 `pd`），所有指标函数必须内联。

- [ ] **Step 1: 创建策略文件**

创建 `/Users/admin/dev/Trade/Trade-quant/strategies/spcxb_v1_script.py`，内容如下：

```python
# SPCXB 核心仓+T仓策略 v1
# QuantDinger ScriptStrategy — 粘贴进策略编辑器即可运行
#
# 推荐配置：
#   Symbol:    SPCXBUSDT
#   Market:    Crypto
#   Exchange:  Binance (Spot)
#   Timeframe: 1H
#   初始资金:  10000 USDT
#
# 策略逻辑（四层过滤）：
#   第一层：MA5/MA10/MA20 多头排列 → uptrend
#   第二层：价格在 session VWAP 上方
#   第三层：KDJ J值<70 + 量能放大 1.5x + VWAP 回踩收复
#   第四层：ATR 动态止损，分层止盈 1.8%/1.4%/1.0%

def on_init(ctx):
    ctx.state = {'layers': 0}

def on_bar(ctx, bar):
    # ── 参数 ──────────────────────────────────────────────
    min_atr      = ctx.param('min_atr_pct', 0.003)     # 最低 ATR 过滤
    atr_sl_mult  = ctx.param('atr_sl_mult', 1.5)       # ATR 止损倍数
    vol_mult     = ctx.param('vol_multiplier', 1.5)    # 量能倍数
    tp1          = ctx.param('tp1', 0.018)              # 第一层止盈
    tp2          = ctx.param('tp2', 0.014)
    tp3          = ctx.param('tp3', 0.010)
    max_layers   = ctx.param('max_layers', 3)
    session_bars_n = ctx.param('session_hours', 13)    # 会话小时数（用于 VWAP）
    vwap_buffer  = ctx.param('vwap_buffer', 0.001)     # VWAP 上方缓冲

    bars = ctx.bars(200)
    if len(bars) < 30:
        return

    closes  = [b.close  for b in bars]
    highs   = [b.high   for b in bars]
    lows    = [b.low    for b in bars]
    volumes = [b.volume for b in bars]

    # ── 内联指标函数 ───────────────────────────────────────

    def _sma(vals, n):
        if len(vals) < n or n <= 0:
            return None
        return sum(vals[-n:]) / n

    def _atr_pct(bars_list, period=14):
        if len(bars_list) < period + 1:
            return None
        trs = []
        for i in range(len(bars_list) - period, len(bars_list)):
            prev_c = bars_list[i - 1].close
            curr   = bars_list[i]
            trs.append(max(
                curr.high - curr.low,
                abs(curr.high - prev_c),
                abs(curr.low  - prev_c),
            ))
        last_close = bars_list[-1].close
        return (sum(trs) / period) / last_close if last_close > 0 else None

    def _session_vwap(bars_list, n):
        window = bars_list[-min(n, len(bars_list)):]
        total_vol = sum(b.volume for b in window)
        if total_vol <= 0:
            return bar.close
        return sum((b.high + b.low + b.close) / 3 * b.volume for b in window) / total_vol

    def _kdj(bars_list, period=9, smooth=3):
        if len(bars_list) < period:
            return None, None, None
        alpha = 1.0 / smooth
        k = d = 50.0
        for i in range(len(bars_list)):
            win = bars_list[max(0, i - period + 1):i + 1]
            lo = min(b.low  for b in win)
            hi = max(b.high for b in win)
            rsv = 50.0 if hi == lo else (bars_list[i].close - lo) / (hi - lo) * 100
            k = (1 - alpha) * k + alpha * rsv
            d = (1 - alpha) * d + alpha * k
        return k, d, 3 * k - 2 * d

    def _ema(vals, n):
        if len(vals) < n:
            return None
        alpha = 2.0 / (n + 1)
        e = vals[0]
        for v in vals[1:]:
            e = alpha * v + (1 - alpha) * e
        return e

    # ── 计算指标 ───────────────────────────────────────────
    ma5  = _sma(closes, 5)
    ma10 = _sma(closes, 10)
    ma20 = _sma(closes, 20)

    if ma5 is None or ma10 is None or ma20 is None:
        return

    if ma5 > ma10 > ma20:
        trend = 'up'
    elif closes[-1] < ma20 * 0.99:
        trend = 'broken'
    else:
        trend = 'neutral'

    daily_atr = _atr_pct(bars, 14)
    if daily_atr is None or daily_atr < min_atr:
        return

    vwap = _session_vwap(bars, session_bars_n)

    kval, dval, jval = _kdj(bars[-30:])

    ema12 = _ema(closes[-50:], 12)
    ema26 = _ema(closes[-50:], 26)
    macd_positive = (ema12 is not None and ema26 is not None and ema12 > ema26)

    avg_vol   = sum(volumes[-20:]) / max(1, min(20, len(volumes)))
    vol_spike = bar.volume > avg_vol * vol_mult

    prev = bars[-2] if len(bars) >= 2 else bar

    # ── 止损 / 止盈（持仓时优先检查）──────────────────────
    layers = ctx.state.get('layers', 0)

    if ctx.position > 0:
        entry = ctx.position.get('long_entry', bar.close)
        if entry <= 0:
            entry = bar.close
        pnl = (bar.close - entry) / entry

        # ATR 动态止损
        stop = daily_atr * atr_sl_mult
        if pnl <= -stop:
            ctx.close_position()
            ctx.state['layers'] = 0
            ctx.log(f"STOP: pnl={pnl:.2%} <= -{stop:.2%}")
            return

        # 分层止盈
        tp_targets = [tp1, tp2, tp3]
        tp = tp_targets[min(layers - 1, 2)] if layers > 0 else tp1
        if pnl >= tp:
            ctx.sell(reason=f"TP layer {layers} pnl={pnl:.2%}")
            ctx.state['layers'] = max(0, layers - 1)
            ctx.log(f"TP: layer={layers} pnl={pnl:.2%}")
            return

        # 加仓（最多 max_layers 层）
        if layers < max_layers and trend == 'up' and vol_spike and pnl < 0.005:
            ctx.buy(reason=f"add_layer_{layers+1}")
            ctx.state['layers'] = layers + 1
            ctx.log(f"ADD: layer={layers+1}")
        return

    # ── 入场（空仓时）──────────────────────────────────────
    if trend != 'up':
        return
    if bar.close < vwap * (1 - vwap_buffer):
        return

    # 形态 A：VWAP 回踩收复
    vwap_reclaim = prev.low < vwap and bar.close > vwap

    # 形态 B：MACD + 量能动量
    momentum = macd_positive and vol_spike and bar.close > prev.close * 1.002

    # J 值过滤（避免超买入场）
    j_ok = jval is None or jval < 70

    if vwap_reclaim and j_ok and vol_spike:
        ctx.buy(reason="vwap_reclaim")
        ctx.state['layers'] = 1
        ctx.log(f"BUY vwap_reclaim j={jval:.1f if jval else 'N/A'}")
    elif momentum and j_ok:
        ctx.buy(reason="momentum")
        ctx.state['layers'] = 1
        ctx.log(f"BUY momentum macd_pos={macd_positive}")
```

- [ ] **Step 2: 验证脚本语法正确**

```bash
cd /Users/admin/dev/Trade
python3 -c "
with open('Trade-quant/strategies/spcxb_v1_script.py') as f:
    code = f.read()
compile(code, 'spcxb_v1_script.py', 'exec')
print('语法检查通过')
"
```

期望输出：`语法检查通过`

- [ ] **Step 3: 本地快速逻辑验证**

```bash
cd /Users/admin/dev/Trade
python3 << 'EOF'
# 模拟 QuantDinger ctx 接口，验证 on_bar 不会崩
class MockBar:
    def __init__(self, o, h, l, c, v):
        self.open, self.high, self.low, self.close, self.volume = o, h, l, c, v
    def get(self, k, d=None):
        return getattr(self, k, d)

class MockPosition(dict):
    def __gt__(self, o): return False
    def __eq__(self, o): return True

class MockCtx:
    def __init__(self):
        self.position = MockPosition()
        self.state = {}
        self._params = {}
        self._orders = []
        self._logs = []
        self._bar_history = [MockBar(100+i*0.1, 101+i*0.1, 99+i*0.1, 100.5+i*0.1, 1000+i*10) for i in range(200)]
    def param(self, k, d=None):
        return self._params.get(k, d)
    def bars(self, n=1):
        return self._bar_history[-n:]
    def buy(self, **kw): self._orders.append(('buy', kw))
    def sell(self, **kw): self._orders.append(('sell', kw))
    def close_position(self): self._orders.append(('close',))
    def log(self, m): self._logs.append(m)

exec(open('Trade-quant/strategies/spcxb_v1_script.py').read())
ctx = MockCtx()
on_init(ctx)
bar = MockBar(100, 101, 99, 100.5, 2000)
on_bar(ctx, bar)
print("on_bar 执行成功, orders:", ctx._orders, "logs:", ctx._logs[:3])
EOF
```

期望：`on_bar 执行成功` 且无报错。

- [ ] **Step 4: 提交**

```bash
cd /Users/admin/dev/Trade
git add Trade-quant/strategies/spcxb_v1_script.py
git commit -m "feat: add SPCXB ScriptStrategy v1 for QuantDinger"
```

---

## Task 4: 在 QuantDinger 中导入策略并运行回测

**Files:** 无需新建，在 QuantDinger UI 操作

- [ ] **Step 1: 在 QuantDinger 创建策略**

打开 `http://localhost:8888`，进入 Strategies → New Strategy：
- Name: `SPCXB CoreT v1`
- Strategy Type: `Script Strategy`
- Symbol: `SPCXBUSDT`
- Market: `Crypto`
- Exchange: `Binance`（Spot）
- Timeframe: `1H`

在代码编辑框中，粘贴 `Trade-quant/strategies/spcxb_v1_script.py` 的全部内容（从 `def on_init` 到结尾）。

- [ ] **Step 2: 运行回测验证买卖点和资金曲线**

点击 Backtest 按钮，配置：
- Start: 数据最早可用日期（Task 2 Step 3 记录的值）
- End: 今天（2026-06-20）
- Initial Capital: 10000 USDT

期望：
- 图表上有买卖标记（绿色买入 ▲，红色卖出 ▼）
- 资金曲线图可见
- 无 Python 错误弹出

如果出现错误，检查报错信息。常见问题：
- `NameError: name 'np' is not defined` → QuantDinger 已提供 np，代码中不应该有裸 numpy 调用（当前脚本没有）
- `ctx.state not defined` → 检查 on_init 是否被正确调用

- [ ] **Step 3: 记录核心指标**

在 QuantDinger 回测结果页面记录：
- Total Return %
- Max Drawdown %
- Sharpe Ratio（如显示）
- 交易次数

将这些数据记录在 `Trade-quant/research/backtest_baseline.txt` 中：

```
SPCXB CoreT v1 — 基线回测记录
日期: 2026-06-20
参数: 默认（tp1=1.8%, tp2=1.4%, tp3=1.0%, vol_mult=1.5, atr_sl_mult=1.5）
Total Return: ____%
Max Drawdown: ____%
Trades: ____
```

---

## Task 5: 研究数据下载工具

**Files:**
- Create: `Trade-quant/research/requirements.txt`
- Create: `Trade-quant/research/fetch_ohlcv.py`

- [ ] **Step 1: 创建研究环境依赖文件**

创建 `/Users/admin/dev/Trade/Trade-quant/research/requirements.txt`：

```
ccxt>=4.0.0
pandas>=2.0.0
pyarrow>=14.0.0
numpy>=1.24.0
matplotlib>=3.7.0
```

安装：

```bash
pip install -r /Users/admin/dev/Trade/Trade-quant/research/requirements.txt
```

期望：所有包安装成功，无版本冲突报错。

- [ ] **Step 2: 创建 OHLCV 下载工具**

创建 `/Users/admin/dev/Trade/Trade-quant/research/fetch_ohlcv.py`：

```python
"""
从 Binance Spot 下载 OHLCV 历史数据，存为 Parquet 文件。

用法:
    python fetch_ohlcv.py                          # 下载 SPCXBUSDT 1H 全量
    python fetch_ohlcv.py --symbol BTCUSDT --tf 1d # 自定义
"""
import argparse
import time
from datetime import datetime, timezone
from pathlib import Path

import ccxt
import pandas as pd

DATA_DIR = Path(__file__).parent.parent / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)


def fetch_all_ohlcv(
    symbol: str,
    timeframe: str,
    since_dt: datetime | None = None,
    exchange_id: str = "binance",
) -> pd.DataFrame:
    ex = getattr(ccxt, exchange_id)({"options": {"defaultType": "spot"}})
    ex.load_markets()

    since_ms = (
        int(since_dt.replace(tzinfo=timezone.utc).timestamp() * 1000)
        if since_dt
        else None
    )

    all_bars: list[list] = []
    limit = 1000

    while True:
        bars = ex.fetch_ohlcv(symbol, timeframe, since=since_ms, limit=limit)
        if not bars:
            break
        all_bars.extend(bars)
        if len(bars) < limit:
            break
        since_ms = bars[-1][0] + 1
        time.sleep(0.3)

    df = pd.DataFrame(all_bars, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["datetime"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df = df.set_index("datetime").sort_index()
    df = df[~df.index.duplicated(keep="last")]
    return df


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol",   default="SPCXBUSDT")
    parser.add_argument("--tf",       default="1h")
    parser.add_argument("--since",    default="2023-01-01",
                        help="开始日期 YYYY-MM-DD，默认 2023-01-01")
    parser.add_argument("--exchange", default="binance")
    args = parser.parse_args()

    since_dt = datetime.strptime(args.since, "%Y-%m-%d")
    print(f"下载 {args.symbol} {args.tf} from {args.since} ...")
    df = fetch_all_ohlcv(args.symbol, args.tf, since_dt, args.exchange)
    print(f"获取 {len(df)} 根K线，时间范围: {df.index[0]} ~ {df.index[-1]}")

    out = DATA_DIR / f"{args.symbol.lower()}_{args.tf}_raw.parquet"
    df.to_parquet(out)
    print(f"保存至: {out}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: 测试下载**

```bash
cd /Users/admin/dev/Trade/Trade-quant/research
python fetch_ohlcv.py --symbol SPCXBUSDT --tf 1h --since 2023-01-01
```

期望输出：
```
下载 SPCXBUSDT 1h from 2023-01-01 ...
获取 XXXX 根K线，时间范围: 2023-xx-xx ~ 2026-06-xx
保存至: .../Trade-quant/data/spcxbusdt_1h_raw.parquet
```

- [ ] **Step 4: 提交**

```bash
cd /Users/admin/dev/Trade
git add Trade-quant/research/requirements.txt Trade-quant/research/fetch_ohlcv.py
git commit -m "feat: add Binance OHLCV downloader for research"
```

---

## Task 6: 因子计算与 Parquet 存储

**Files:**
- Create: `Trade-quant/research/compute_factors.py`
- Create: `Trade-quant/research/factor_store.py`

- [ ] **Step 1: 创建因子存储封装**

创建 `/Users/admin/dev/Trade/Trade-quant/research/factor_store.py`：

```python
"""
Parquet 因子表的读写封装。
每个 symbol+timeframe 对应一个 Parquet 文件，schema 固定。
"""
from pathlib import Path
import pandas as pd

DATA_DIR = Path(__file__).parent.parent / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)


def factor_path(symbol: str, timeframe: str) -> Path:
    return DATA_DIR / f"{symbol.lower()}_{timeframe}_factors.parquet"


def save_factors(df: pd.DataFrame, symbol: str, timeframe: str) -> Path:
    path = factor_path(symbol, timeframe)
    df.to_parquet(path)
    return path


def load_factors(symbol: str, timeframe: str) -> pd.DataFrame:
    path = factor_path(symbol, timeframe)
    if not path.exists():
        raise FileNotFoundError(f"Factor file not found: {path}\n先运行 compute_factors.py")
    return pd.read_parquet(path)
```

- [ ] **Step 2: 创建因子计算脚本**

创建 `/Users/admin/dev/Trade/Trade-quant/research/compute_factors.py`：

```python
"""
从原始 OHLCV Parquet 计算量化因子，存为 factor Parquet。

计算的因子：
  - MA5, MA10, MA20 (收盘价简单均线)
  - trend: up / neutral / broken
  - ATR_14_pct: 14周期 ATR / 收盘价
  - VWAP_session: 当日13:30 UTC起的 session VWAP（1H粒度近似）
  - KDJ_K, KDJ_D, KDJ_J (9,3,3)
  - MACD_line, MACD_signal, MACD_hist (12,26,9)
  - vol_ratio: 当根成交量 / 20周期均量
  - buy_signal: 策略入场信号 (1=买, 0=不操作)
  - sell_signal: 策略出场信号 (1=卖, 0=不操作)

用法:
    python compute_factors.py                    # 处理 SPCXBUSDT 1h
    python compute_factors.py --symbol BTCUSDT
"""
import argparse
from pathlib import Path

import numpy as np
import pandas as pd

from factor_store import save_factors

DATA_DIR = Path(__file__).parent.parent / "data"


def load_raw(symbol: str, timeframe: str) -> pd.DataFrame:
    path = DATA_DIR / f"{symbol.lower()}_{timeframe}_raw.parquet"
    if not path.exists():
        raise FileNotFoundError(f"先运行 fetch_ohlcv.py 下载数据: {path}")
    return pd.read_parquet(path)


def compute_sma(series: pd.Series, n: int) -> pd.Series:
    return series.rolling(n).mean()


def compute_atr_pct(df: pd.DataFrame, period: int = 14) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"]  - prev_close).abs(),
    ], axis=1).max(axis=1)
    atr = tr.rolling(period).mean()
    return atr / df["close"]


def compute_kdj(df: pd.DataFrame, period: int = 9, smooth: int = 3) -> pd.DataFrame:
    alpha = 1.0 / smooth
    low_min  = df["low"].rolling(period).min()
    high_max = df["high"].rolling(period).max()
    rsv = (df["close"] - low_min) / (high_max - low_min).replace(0, np.nan) * 100
    rsv = rsv.fillna(50)

    k_vals, d_vals = [], []
    k = d = 50.0
    for rsv_i in rsv:
        k = (1 - alpha) * k + alpha * rsv_i
        d = (1 - alpha) * d + alpha * k
        k_vals.append(k)
        d_vals.append(d)

    k_s = pd.Series(k_vals, index=df.index)
    d_s = pd.Series(d_vals, index=df.index)
    j_s = 3 * k_s - 2 * d_s
    return pd.DataFrame({"KDJ_K": k_s, "KDJ_D": d_s, "KDJ_J": j_s})


def compute_macd(
    series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9
) -> pd.DataFrame:
    ema_fast   = series.ewm(span=fast, adjust=False).mean()
    ema_slow   = series.ewm(span=slow, adjust=False).mean()
    macd_line  = ema_fast - ema_slow
    macd_sig   = macd_line.ewm(span=signal, adjust=False).mean()
    macd_hist  = macd_line - macd_sig
    return pd.DataFrame({
        "MACD_line":   macd_line,
        "MACD_signal": macd_sig,
        "MACD_hist":   macd_hist,
    })


def compute_session_vwap(df: pd.DataFrame, session_hour_utc: int = 13) -> pd.Series:
    """每天 session_hour_utc:30 重置的 VWAP（逐行累计）。
    对于1H数据，用 13:00 UTC 所在 bar 作为 session 起点（近似）。
    """
    typical = (df["high"] + df["low"] + df["close"]) / 3
    tp_vol = typical * df["volume"]

    session_key = df.index.floor("D") + pd.Timedelta(hours=session_hour_utc)
    # 每个 bar 属于哪个 session（下午1点UTC开始）
    bar_session = df.index.map(lambda t: t.floor("D") + pd.Timedelta(hours=session_hour_utc)
                                if t.hour >= session_hour_utc else
                                t.floor("D") - pd.Timedelta(hours=24 - session_hour_utc))

    df_temp = df.copy()
    df_temp["_session"] = bar_session
    df_temp["_tp_vol"]  = tp_vol
    df_temp["_vol"]     = df["volume"]

    cumtp  = df_temp.groupby("_session")["_tp_vol"].cumsum()
    cumvol = df_temp.groupby("_session")["_vol"].cumsum()
    return (cumtp / cumvol.replace(0, np.nan)).rename("VWAP_session")


def add_signals(df: pd.DataFrame) -> pd.DataFrame:
    """基于因子生成策略信号标注（用于可视化，不做仓位状态机）。"""
    vwap_reclaim = (df["low"].shift(1) < df["VWAP_session"]) & (df["close"] > df["VWAP_session"])
    j_ok         = df["KDJ_J"] < 70
    vol_spike    = df["vol_ratio"] > 1.5
    uptrend      = df["trend"] == "up"
    above_vwap   = df["close"] > df["VWAP_session"]
    macd_pos     = df["MACD_hist"] > 0

    df["buy_signal"]  = (uptrend & above_vwap & vwap_reclaim & j_ok & vol_spike).astype(int)
    df["sell_signal"] = ((df["KDJ_J"] > 80) & (df["MACD_hist"] < 0)).astype(int)
    return df


def compute_all(symbol: str, timeframe: str) -> pd.DataFrame:
    raw = load_raw(symbol, timeframe)

    df = raw[["open", "high", "low", "close", "volume"]].copy()

    df["MA5"]  = compute_sma(df["close"], 5)
    df["MA10"] = compute_sma(df["close"], 10)
    df["MA20"] = compute_sma(df["close"], 20)

    df["trend"] = "neutral"
    df.loc[
        (df["MA5"] > df["MA10"]) & (df["MA10"] > df["MA20"]),
        "trend"
    ] = "up"
    df.loc[df["close"] < df["MA20"] * 0.99, "trend"] = "broken"

    df["ATR_14_pct"] = compute_atr_pct(df, 14)

    vwap = compute_session_vwap(df, session_hour_utc=13)
    df["VWAP_session"] = vwap

    kdj_df = compute_kdj(df)
    df = df.join(kdj_df)

    macd_df = compute_macd(df["close"])
    df = df.join(macd_df)

    avg_vol = df["volume"].rolling(20).mean()
    df["vol_ratio"] = df["volume"] / avg_vol.replace(0, np.nan)

    df = add_signals(df)

    return df.dropna(subset=["MA20"])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol",   default="SPCXBUSDT")
    parser.add_argument("--tf",       default="1h")
    args = parser.parse_args()

    print(f"计算因子: {args.symbol} {args.tf} ...")
    df = compute_all(args.symbol, args.tf)
    print(f"因子行数: {len(df)}，列: {list(df.columns)}")
    print(df.tail(3).to_string())

    path = save_factors(df, args.symbol, args.tf)
    print(f"保存至: {path}")

    buy_cnt  = df["buy_signal"].sum()
    sell_cnt = df["sell_signal"].sum()
    print(f"买入信号: {buy_cnt} 次，卖出信号: {sell_cnt} 次")


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: 运行因子计算**

```bash
cd /Users/admin/dev/Trade/Trade-quant/research
python compute_factors.py --symbol SPCXBUSDT --tf 1h
```

期望输出：
```
计算因子: SPCXBUSDT 1h ...
因子行数: XXXX，列: ['open', 'high', 'low', 'close', 'volume', 'MA5', 'MA10', 'MA20', 'trend', 'ATR_14_pct', 'VWAP_session', 'KDJ_K', 'KDJ_D', 'KDJ_J', 'MACD_line', 'MACD_signal', 'MACD_hist', 'vol_ratio', 'buy_signal', 'sell_signal']
买入信号: XX 次，卖出信号: XX 次
保存至: .../Trade-quant/data/spcxbusdt_1h_factors.parquet
```

- [ ] **Step 4: 验证 Parquet 可读**

```bash
python3 -c "
import sys; sys.path.insert(0, 'Trade-quant/research')
from factor_store import load_factors
df = load_factors('SPCXBUSDT', '1h')
print('列:', list(df.columns))
print('行数:', len(df))
print('时间范围:', df.index[0], '~', df.index[-1])
buy_days = df[df['buy_signal']==1].index
print('买入信号样例:', buy_days[:5].tolist())
"
```

- [ ] **Step 5: 提交**

```bash
cd /Users/admin/dev/Trade
git add Trade-quant/research/compute_factors.py Trade-quant/research/factor_store.py
git commit -m "feat: add factor computation pipeline (KDJ/MACD/ATR/VWAP)"
```

---

## Task 7: 研究分析脚本（买卖点 + 资金曲线可视化）

**Files:**
- Create: `Trade-quant/research/01_factor_analysis.py`

- [ ] **Step 1: 创建可视化分析脚本**

创建 `/Users/admin/dev/Trade/Trade-quant/research/01_factor_analysis.py`：

```python
"""
研究脚本：读取因子数据，绘制买卖点标注图和简化回测资金曲线。

用法:
    python 01_factor_analysis.py                   # 分析 SPCXBUSDT 1h
    python 01_factor_analysis.py --since 2024-01-01 --until 2024-12-31

说明:
    - 图1：K线 + MA5/10/20 + VWAP + 买卖信号标注
    - 图2：KDJ J 值
    - 图3：MACD 柱
    - 图4：简化信号回测资金曲线（入场=buy_signal,出场=sell_signal）
    - 图5：因子相关矩阵热图
"""
import argparse
import sys
from pathlib import Path

import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from factor_store import load_factors


def simulate_equity(df: pd.DataFrame, capital: float = 10_000.0) -> pd.Series:
    """极简信号回测：buy_signal=买，sell_signal=卖，不计滑点/手续费。"""
    equity = [capital]
    position = 0.0
    entry_price = 0.0

    for i in range(len(df)):
        row = df.iloc[i]
        close = row["close"]
        if position == 0 and row["buy_signal"] == 1:
            position = capital / close
            entry_price = close
        elif position > 0 and row["sell_signal"] == 1:
            capital = position * close
            position = 0.0
        if position > 0:
            equity.append(position * close)
        else:
            equity.append(capital)

    return pd.Series(equity[1:], index=df.index)


def plot_analysis(df: pd.DataFrame, symbol: str, since: str, until: str):
    df = df.loc[since:until].copy()
    if len(df) == 0:
        print(f"无数据在 {since}~{until} 区间")
        return

    buy_bars  = df[df["buy_signal"]  == 1]
    sell_bars = df[df["sell_signal"] == 1]
    equity    = simulate_equity(df)

    fig, axes = plt.subplots(5, 1, figsize=(16, 22), sharex=True)
    fig.suptitle(f"{symbol} 量化因子分析 ({since} ~ {until})", fontsize=14, fontweight="bold")

    # ── 图1：价格 + 均线 + 买卖点 ─────────────────────────
    ax1 = axes[0]
    ax1.plot(df.index, df["close"],        color="#333", lw=0.8, label="Close")
    ax1.plot(df.index, df["MA5"],          color="#2196F3", lw=0.8, alpha=0.7, label="MA5")
    ax1.plot(df.index, df["MA10"],         color="#FF9800", lw=0.8, alpha=0.7, label="MA10")
    ax1.plot(df.index, df["MA20"],         color="#9C27B0", lw=0.8, alpha=0.7, label="MA20")
    ax1.plot(df.index, df["VWAP_session"], color="#00BCD4", lw=0.8, alpha=0.6, label="VWAP", linestyle="--")
    ax1.scatter(buy_bars.index,  buy_bars["close"],  marker="^", color="#4CAF50", s=60, zorder=5, label=f"买入({len(buy_bars)})")
    ax1.scatter(sell_bars.index, sell_bars["close"], marker="v", color="#F44336", s=60, zorder=5, label=f"卖出({len(sell_bars)})")
    ax1.legend(loc="upper left", fontsize=8, ncol=3)
    ax1.set_ylabel("Price (USDT)")
    ax1.set_title("价格 + 均线 + 买卖点")
    ax1.grid(alpha=0.3)

    # ── 图2：KDJ ─────────────────────────────────────────
    ax2 = axes[1]
    ax2.plot(df.index, df["KDJ_K"], color="#2196F3", lw=0.8, label="K")
    ax2.plot(df.index, df["KDJ_D"], color="#FF9800", lw=0.8, label="D")
    ax2.plot(df.index, df["KDJ_J"], color="#9C27B0", lw=0.8, label="J")
    ax2.axhline(80, color="#F44336", lw=0.6, linestyle="--", alpha=0.7, label="超买80")
    ax2.axhline(20, color="#4CAF50", lw=0.6, linestyle="--", alpha=0.7, label="超卖20")
    ax2.legend(loc="upper left", fontsize=8)
    ax2.set_ylabel("KDJ")
    ax2.set_title("KDJ 指标")
    ax2.grid(alpha=0.3)

    # ── 图3：MACD ─────────────────────────────────────────
    ax3 = axes[2]
    colors = ["#4CAF50" if v >= 0 else "#F44336" for v in df["MACD_hist"]]
    ax3.bar(df.index, df["MACD_hist"], color=colors, alpha=0.7, label="MACD Hist")
    ax3.plot(df.index, df["MACD_line"],   color="#2196F3", lw=0.8, label="MACD")
    ax3.plot(df.index, df["MACD_signal"], color="#FF9800", lw=0.8, label="Signal")
    ax3.legend(loc="upper left", fontsize=8)
    ax3.set_ylabel("MACD")
    ax3.set_title("MACD 指标")
    ax3.grid(alpha=0.3)

    # ── 图4：资金曲线 ─────────────────────────────────────
    ax4 = axes[3]
    ax4.plot(df.index, equity, color="#2196F3", lw=1.0, label="策略资金曲线")
    ax4.axhline(10000, color="#999", lw=0.6, linestyle="--", alpha=0.7, label="初始资金 10000")
    total_ret = (equity.iloc[-1] / 10000 - 1) * 100
    drawdown = ((equity / equity.cummax()) - 1).min() * 100
    ax4.set_title(f"资金曲线  总收益: {total_ret:.1f}%  最大回撤: {drawdown:.1f}%")
    ax4.legend(loc="upper left", fontsize=8)
    ax4.set_ylabel("Equity (USDT)")
    ax4.grid(alpha=0.3)

    # ── 图5：成交量 ───────────────────────────────────────
    ax5 = axes[4]
    ax5.bar(df.index, df["vol_ratio"], color="#90CAF9", alpha=0.7)
    ax5.axhline(1.5, color="#FF9800", lw=0.8, linestyle="--", label="1.5x 量能阈值")
    ax5.set_ylabel("量能倍数")
    ax5.set_title("相对成交量")
    ax5.legend(loc="upper right", fontsize=8)
    ax5.grid(alpha=0.3)

    for ax in axes:
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
        ax.xaxis.set_major_locator(mdates.MonthLocator())
        plt.setp(ax.xaxis.get_majorticklabels(), rotation=30, ha="right", fontsize=7)

    plt.tight_layout()
    out_path = Path(__file__).parent.parent / "data" / f"{symbol.lower()}_factor_chart.png"
    plt.savefig(out_path, dpi=150, bbox_inches="tight")
    print(f"图表保存至: {out_path}")
    plt.show()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", default="SPCXBUSDT")
    parser.add_argument("--tf",     default="1h")
    parser.add_argument("--since",  default="2024-01-01")
    parser.add_argument("--until",  default="2026-06-20")
    args = parser.parse_args()

    df = load_factors(args.symbol, args.tf)
    print(f"载入因子数据: {len(df)} 行 ({df.index[0]} ~ {df.index[-1]})")
    plot_analysis(df, args.symbol, args.since, args.until)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: 运行并验证图表**

```bash
cd /Users/admin/dev/Trade/Trade-quant/research
python 01_factor_analysis.py --symbol SPCXBUSDT --tf 1h --since 2024-01-01
```

期望：
- 弹出 5 图的分析图表
- 图1 上有买卖点标记
- 图4 显示资金曲线走势
- 同时在 `Trade-quant/data/spcxbusdt_1h_factor_chart.png` 保存图片

- [ ] **Step 3: 提交**

```bash
cd /Users/admin/dev/Trade
git add Trade-quant/research/01_factor_analysis.py
git add Trade-quant/research/backtest_baseline.txt  # 如果 Task 4 Step 3 有记录
git commit -m "feat: add factor analysis and visualization script"
```

---

## 完成后的使用流程

```
研究循环（日常使用）：

1. 更新数据
   cd Trade-quant/research && python fetch_ohlcv.py

2. 重算因子
   python compute_factors.py

3. 调整参数、看图
   python 01_factor_analysis.py --since 2024-06-01

4. 参数满意后更新 QuantDinger 策略
   复制 strategies/spcxb_v1_script.py → 粘贴进 QuantDinger 编辑器
   → 运行回测 → 查看 QuantDinger 图表上的买卖点和资金曲线

5. 进入 Paper 模式观察实际信号
   QuantDinger → Strategy → Paper Trading
```

---

## 自查清单

- [x] QuantDinger Docker 启动（Task 1）
- [x] SPCXB 数据源验证（Task 2）
- [x] ScriptStrategy 编写并本地语法验证（Task 3）
- [x] QuantDinger 策略回测验证买卖点+资金曲线（Task 4）
- [x] 历史数据下载工具（Task 5）
- [x] 因子计算 + Parquet 存储（Task 6）
- [x] 可视化分析脚本（Task 7）

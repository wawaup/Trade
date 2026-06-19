# SPCXB 核心仓+T仓策略 v1
# QuantDinger ScriptStrategy — 整体粘贴进策略编辑器
#
# 推荐配置：
#   Symbol:    SPCXBUSDT（B-Stock上线后）/ 当前测试用 BTCUSDT
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
    min_atr      = ctx.param('min_atr_pct', 0.003)
    atr_sl_mult  = ctx.param('atr_sl_mult', 1.5)
    vol_mult     = ctx.param('vol_multiplier', 1.5)
    tp1          = ctx.param('tp1', 0.018)
    tp2          = ctx.param('tp2', 0.014)
    tp3          = ctx.param('tp3', 0.010)
    max_layers   = ctx.param('max_layers', 3)
    session_n    = ctx.param('session_hours', 13)
    vwap_buffer  = ctx.param('vwap_buffer', 0.001)

    bars = ctx.bars(200)
    if len(bars) < 30:
        return

    closes  = [b.close  for b in bars]
    highs   = [b.high   for b in bars]
    lows    = [b.low    for b in bars]
    volumes = [b.volume for b in bars]

    # ── 内联指标 ───────────────────────────────────────────

    def _sma(vals, n):
        if len(vals) < n or n <= 0:
            return None
        return sum(vals[-n:]) / n

    def _atr_pct(blist, period=14):
        if len(blist) < period + 1:
            return None
        trs = []
        for i in range(len(blist) - period, len(blist)):
            pc = blist[i - 1].close
            c  = blist[i]
            trs.append(max(c.high - c.low, abs(c.high - pc), abs(c.low - pc)))
        lc = blist[-1].close
        return (sum(trs) / period) / lc if lc > 0 else None

    def _session_vwap(blist, n):
        w = blist[-min(n, len(blist)):]
        tv = sum(b.volume for b in w)
        if tv <= 0:
            return bar.close
        return sum((b.high + b.low + b.close) / 3 * b.volume for b in w) / tv

    def _kdj(blist, period=9, smooth=3):
        if len(blist) < period:
            return None, None, None
        alpha = 1.0 / smooth
        k = d = 50.0
        for i in range(len(blist)):
            win = blist[max(0, i - period + 1):i + 1]
            lo = min(b.low for b in win)
            hi = max(b.high for b in win)
            rsv = 50.0 if hi == lo else (blist[i].close - lo) / (hi - lo) * 100
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

    # ── 计算指标值 ─────────────────────────────────────────
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

    vwap = _session_vwap(bars, session_n)
    kval, dval, jval = _kdj(bars[-30:])

    ema12 = _ema(closes[-50:], 12)
    ema26 = _ema(closes[-50:], 26)
    macd_positive = (ema12 is not None and ema26 is not None and ema12 > ema26)

    avg_vol   = sum(volumes[-20:]) / max(1, min(20, len(volumes)))
    vol_spike = bar.volume > avg_vol * vol_mult
    prev      = bars[-2] if len(bars) >= 2 else bar
    layers    = ctx.state.get('layers', 0)

    # ── 持仓管理（止损/止盈/加仓）──────────────────────────
    if ctx.position > 0:
        entry = ctx.position.get('long_entry', bar.close)
        if entry <= 0:
            entry = bar.close
        pnl = (bar.close - entry) / entry

        stop = daily_atr * atr_sl_mult
        if pnl <= -stop:
            ctx.close_position()
            ctx.state['layers'] = 0
            ctx.log(f"STOP pnl={pnl:.2%} <= -{stop:.2%}")
            return

        tp_targets = [tp1, tp2, tp3]
        tp = tp_targets[min(layers - 1, 2)] if layers > 0 else tp1
        if pnl >= tp:
            ctx.sell(reason=f"TP layer {layers} pnl={pnl:.2%}")
            ctx.state['layers'] = max(0, layers - 1)
            ctx.log(f"TP layer={layers} pnl={pnl:.2%}")
            return

        if layers < max_layers and trend == 'up' and vol_spike and pnl < 0.005:
            ctx.buy(reason=f"add layer {layers+1}")
            ctx.state['layers'] = layers + 1
            ctx.log(f"ADD layer={layers+1}")
        return

    # ── 入场逻辑 ───────────────────────────────────────────
    if trend != 'up':
        return
    if bar.close < vwap * (1 - vwap_buffer):
        return

    vwap_reclaim = prev.low < vwap and bar.close > vwap
    momentum     = macd_positive and vol_spike and bar.close > prev.close * 1.002
    j_ok         = jval is None or jval < 70

    if vwap_reclaim and j_ok and vol_spike:
        ctx.buy(reason="vwap_reclaim")
        ctx.state['layers'] = 1
        ctx.log(f"BUY vwap_reclaim j={jval:.1f if jval else 'N/A'}")
    elif momentum and j_ok:
        ctx.buy(reason="momentum")
        ctx.state['layers'] = 1
        ctx.log(f"BUY momentum macd={macd_positive}")

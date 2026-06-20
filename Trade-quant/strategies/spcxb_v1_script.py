# SPCXB 核心仓+T仓策略 v3 — 多周期趋势过滤版
# QuantDinger ScriptStrategy — 整体粘贴进策略编辑器
#
# 推荐配置：
#   Symbol:    SPCXBUSDT（B-Stock上线后）/ 当前测试用 BTCUSDT
#   Market:    Crypto / B-Stock
#   Exchange:  Binance (Spot)
#   Timeframe: 1H
#   初始资金:  10000 USDT
#
# 双轨仓位：
#   核心仓（70%）：日线趋势上行才建仓，日线MA20破位才减仓（1H小回调不动）
#   T  仓（20%）：VWAP/MACD 1H信号入场，ATR止损，分层止盈
#
# 双参数档位（stock_type 参数）：
#   "bluechip"  → vol_mult=1.2（蓝筹/低波动，量能门槛低）
#   "volatile"  → vol_mult=1.5（高波动，需更明显量能确认）
#
# 策略逻辑（多周期框架）：
#   日线层：最近24根1H收盘重采样为日线，计算日线MA5/MA20/MA50
#     → 日线上升：关注做多机会
#     → 日线下行：禁止新建核心仓
#   1H 层：MA5>MA10>MA20 + VWAP确认核心仓入场时机
#   T仓：KDJ J<70 + 量能放大 + VWAP回踩 或 MACD动量
#   止损：ATR动态止损，分层止盈 1.8%/1.4%/1.0%
#
# 注：
#   实盘 T仓 建议结合5min数据判断入场精确时机
#   当前回测用1H数据近似，实盘可在此基础上加5min VWAP/KDJ确认层

def on_init(ctx):
    ctx.state = {
        'core_open': False,
        'core_entry': 0.0,
        'core_ready': 0,          # 连续满足日线+1H入场条件的根数
        'exit_stage': 0,          # 0=满仓 1=减了50% 2=减了80% 3=全清
        'entry_stage': 0,         # 0=无重建 1/2/3=右侧分批重建中
        'above_recovery_cnt': 0,  # 日线恢复确认计数
        'sell_cd': 0,             # 减仓冷却计数
        't_layers': 0,
        't_entry_stop_pct': None,
    }

def on_bar(ctx, bar):
    # ── 参数 ──────────────────────────────────────────────
    stock_type      = ctx.param('stock_type', 'bluechip')
    min_atr         = ctx.param('min_atr_pct', 0.003)
    atr_sl_mult     = ctx.param('atr_sl_mult', 1.5)
    vol_mult        = ctx.param('bluechip_vol_mult', 1.2) if stock_type == 'bluechip' \
                      else ctx.param('volatile_vol_mult', 1.5)
    tp1             = ctx.param('tp1', 0.018)
    tp2             = ctx.param('tp2', 0.014)
    tp3             = ctx.param('tp3', 0.010)
    max_t_layers    = ctx.param('max_t_layers', 3)
    session_n       = ctx.param('session_hours', 13)
    vwap_buffer     = ctx.param('vwap_buffer', 0.001)
    core_pct        = ctx.param('core_pct', 0.70)
    t_pct           = ctx.param('t_pct', 0.20)
    core_entry_bars = ctx.param('core_entry_bars', 3)
    sell_cooldown   = ctx.param('sell_cooldown_bars', 8)
    exit_reset_bars = ctx.param('exit_reset_bars', 5)

    bars = ctx.bars(500)   # 需要足够多根1H棒来计算日线近似
    if len(bars) < 60:
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
        for i in range(1, len(blist)):
            pc = blist[i - 1].close
            c  = blist[i]
            trs.append(max(c.high - c.low, abs(c.high - pc), abs(c.low - pc)))
        if len(trs) < period:
            return None
        atr = sum(trs[:period]) / period
        for tr in trs[period:]:
            atr = (atr * (period - 1) + tr) / period
        lc = blist[-1].close
        return atr / lc if lc > 0 else None

    def _session_vwap(blist, n):
        w = blist[-min(n, len(blist)):]
        tv = sum(b.volume for b in w)
        if tv <= 0:
            return blist[-1].close
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
            if i >= period - 1:
                k = (1 - alpha) * k + alpha * rsv
                d = (1 - alpha) * d + alpha * k
        return k, d, 3 * k - 2 * d

    def _ema(vals, n):
        if len(vals) < n:
            return None
        e = sum(vals[:n]) / n
        alpha = 2.0 / (n + 1)
        for v in vals[n:]:
            e = alpha * v + (1 - alpha) * e
        return e

    # ── 计算1H指标 ─────────────────────────────────────────
    ma5  = _sma(closes, 5)
    ma10 = _sma(closes, 10)
    ma20 = _sma(closes, 20)
    if ma5 is None or ma10 is None or ma20 is None:
        return

    trend_up_1h = ma5 > ma10 > ma20
    trend = 'up' if trend_up_1h else ('broken' if closes[-1] < ma20 * 0.99 else 'neutral')

    daily_atr = _atr_pct(bars, 14)
    vwap      = _session_vwap(bars, session_n)
    kval, dval, jval = _kdj(bars[-100:])

    ema12 = _ema(closes[-60:], 12)
    ema26 = _ema(closes[-60:], 26)
    macd_positive = (ema12 is not None and ema26 is not None and ema12 > ema26)

    avg_vol    = sum(volumes[-20:]) / max(1, min(20, len(volumes)))
    vol_spike  = bar.volume > avg_vol * vol_mult
    prev       = bars[-2] if len(bars) >= 2 else bar
    above_vwap = bar.close > vwap * (1 - vwap_buffer)
    atr_ok     = daily_atr is not None and daily_atr >= min_atr

    # ── 日线近似趋势（从1H数据重采样）────────────────────────
    # 每24根1H K线 = 1天（近似）；取最近 50 天的日线收盘
    bars_per_day = 24
    daily_closes = []
    step = bars_per_day
    for d in range(50, 0, -1):
        idx = len(closes) - d * step
        if idx >= 0:
            daily_closes.append(closes[idx])
    # 日线MA
    def _d_sma(dc, n):
        return sum(dc[-n:]) / n if len(dc) >= n else None
    d_ma5  = _d_sma(daily_closes, 5)
    d_ma20 = _d_sma(daily_closes, 20)
    d_ma50 = _d_sma(daily_closes, 50) if len(daily_closes) >= 50 else None
    d_close = daily_closes[-1] if daily_closes else closes[-1]

    # 日线上升趋势：收盘 > d_MA20 且 d_MA5 > d_MA20 且 d_MA20 > d_MA50（或MA50未就绪则宽松）
    d_trend_up = (
        d_ma5 is not None and d_ma20 is not None and
        d_close > d_ma20 and d_ma5 > d_ma20 and
        (d_ma50 is None or d_ma20 > d_ma50)
    )
    # 日线下行：收盘跌破 d_MA50
    d_trend_dn = (d_ma50 is not None and d_close < d_ma50)

    # 核心仓进出基准（日线控制，不用1H MA）
    above_d_ma20 = (d_ma20 is not None and d_close > d_ma20 * 0.99)
    above_d_ma50 = (d_ma50 is None or d_close > d_ma50 * 0.99)
    core_recovery_ok = above_d_ma50 and d_trend_up

    # ── 读取状态 ───────────────────────────────────────────
    core_open          = ctx.state.get('core_open', False)
    core_entry         = ctx.state.get('core_entry', 0.0)
    core_ready         = ctx.state.get('core_ready', 0)
    exit_stage         = ctx.state.get('exit_stage', 0)
    entry_stage        = ctx.state.get('entry_stage', 0)
    above_recovery_cnt = ctx.state.get('above_recovery_cnt', 0)
    sell_cd            = ctx.state.get('sell_cd', 0)
    t_layers           = ctx.state.get('t_layers', 0)
    t_entry_stop       = ctx.state.get('t_entry_stop_pct')

    # ══ 核心仓 U型状态机 ═══════════════════════════════════════
    if sell_cd > 0:
        sell_cd -= 1
        ctx.state['sell_cd'] = sell_cd

    if core_open:
        ctx.state['core_ready'] = 0

        # ── 右侧恢复监测 ──────────────────────────────────────
        if exit_stage in (1, 2) and entry_stage == 0:
            if core_recovery_ok:
                above_recovery_cnt += 1
            else:
                above_recovery_cnt = 0
            ctx.state['above_recovery_cnt'] = above_recovery_cnt
            if above_recovery_cnt >= exit_reset_bars:
                entry_stage = exit_stage   # 触发右侧重建（不直接重置exit_stage）
                ctx.state['entry_stage']         = entry_stage
                ctx.state['above_recovery_cnt']  = 0

        # ── 减仓逻辑（日线MA触发，重建期间禁止）──────────────
        if sell_cd == 0 and entry_stage == 0:
            if exit_stage == 0 and not above_d_ma20:
                # SELL1：日线MA20破位，卖出50%
                ctx.sell(pct=0.50 * core_pct, reason="core_sell1_dMA20")
                ctx.state['exit_stage']          = 1
                ctx.state['sell_cd']             = sell_cooldown
                ctx.state['above_recovery_cnt']  = 0
                pnl = (bar.close - core_entry) / core_entry if core_entry > 0 else 0
                ctx.log(f"CORE_SELL1 日线MA20破位 -50%核心仓 pnl={pnl:.2%}")
                if t_layers > 0:
                    ctx.sell(pct=t_pct, reason="t_stop_联动")
                    ctx.state['t_layers'] = 0
                    ctx.state['t_entry_stop_pct'] = None
                return
            elif exit_stage == 1 and not above_d_ma50:
                # SELL2：日线MA50破位，再卖30%
                ctx.sell(pct=0.30 * core_pct, reason="core_sell2_dMA50")
                ctx.state['exit_stage']  = 2
                ctx.state['sell_cd']     = sell_cooldown
                ctx.log(f"CORE_SELL2 日线MA50破位 再-30%核心仓")
                return
            elif exit_stage == 2 and not above_d_ma50:
                # SELL3：全清剩余核心仓
                ctx.close_position()
                ctx.state['core_open']   = False
                ctx.state['core_entry']  = 0.0
                ctx.state['exit_stage']  = 3
                ctx.state['t_layers']    = 0
                ctx.state['t_entry_stop_pct'] = None
                pnl = (bar.close - core_entry) / core_entry if core_entry > 0 else 0
                ctx.log(f"CORE_SELL3 日线持续下行 全清 pnl={pnl:.2%}")
                return

    else:
        # ── 右侧重建 or 首次建仓 ──────────────────────────────
        if exit_stage == 3:
            # 全清后等待恢复
            if core_recovery_ok and trend_up_1h:
                entry_stage = 1
                ctx.state['entry_stage'] = 1
                ctx.state['exit_stage']  = 0
                ctx.state['core_ready']  = 0
            elif not core_recovery_ok and not d_trend_dn:
                pass  # 中性等待
            return

        if entry_stage > 0:
            # 右侧重建：每档需日线+1H同时确认
            if trend_up_1h and core_recovery_ok:
                core_ready += 1
                ctx.state['core_ready'] = core_ready
            else:
                ctx.state['core_ready'] = 0
                core_ready = 0

            if core_ready >= core_entry_bars:
                tranches = [0.50, 0.30, 0.20]
                buy_pct = tranches[min(entry_stage - 1, 2)] * core_pct
                ctx.buy(pct=buy_pct, reason=f"core_rebuy{entry_stage}")
                ctx.state['core_open']   = True
                ctx.state['core_entry']  = bar.close
                ctx.state['core_ready']  = 0
                ctx.log(f"CORE_REBUY{entry_stage} 右侧重建第{entry_stage}档 close={bar.close:.2f}")
                if entry_stage < 3:
                    ctx.state['entry_stage'] = entry_stage + 1
                else:
                    ctx.state['entry_stage'] = 0
                    ctx.state['exit_stage']  = 0  # 重建完成，恢复满仓状态
            return

        # 普通首次建仓：日线上升才允许
        if d_trend_dn:
            ctx.state['core_ready'] = 0
            return
        if trend_up_1h and above_vwap and d_trend_up:
            core_ready += 1
            ctx.state['core_ready'] = core_ready
        else:
            ctx.state['core_ready'] = 0
            core_ready = 0

        if core_ready >= core_entry_bars:
            ctx.buy(pct=core_pct, reason="core_buy")
            ctx.state['core_open']   = True
            ctx.state['core_entry']  = bar.close
            ctx.state['core_ready']  = 0
            ctx.state['exit_stage']  = 0
            ctx.log(f"CORE_BUY 日线+1H连续{core_entry_bars}根多头确认 close={bar.close:.2f}")
        return

    # 核心仓不在场，不操作T仓
    if not core_open:
        return

    # ══ T仓管理 ══════════════════════════════════════════════
    if ctx.position > 0 and t_layers > 0:
        # 使用核心仓均价近似T仓成本（平台限制，实盘建议分开账户）
        t_entry_approx = ctx.state.get('t_entry_price', bar.close)
        t_pnl = (bar.close - t_entry_approx) / t_entry_approx if t_entry_approx > 0 else 0

        stop = t_entry_stop or (daily_atr * atr_sl_mult if daily_atr else 0.035)
        if t_pnl <= -stop:
            # 只平T仓部分（pct=t_pct）
            ctx.sell(pct=t_pct, reason="t_stop")
            ctx.state['t_layers']         = 0
            ctx.state['t_entry_stop_pct'] = None
            ctx.log(f"T_STOP pnl={t_pnl:.2%} <= -{stop:.2%}")
            return

        tp_targets = [tp1, tp2, tp3]
        tp = tp_targets[min(t_layers - 1, 2)] if t_layers > 0 else tp1
        if t_pnl >= tp:
            ctx.sell(pct=t_pct, reason="t_tp")
            ctx.state['t_layers']         = 0
            ctx.state['t_entry_stop_pct'] = None
            ctx.log(f"T_TP layer={t_layers} pnl={t_pnl:.2%}")
            return

        if t_layers < max_t_layers and trend == 'up' and vol_spike and 0 < t_pnl < 0.01 and atr_ok:
            ctx.buy(pct=0.05, reason=f"t_add layer {t_layers+1}")
            ctx.state['t_layers'] = t_layers + 1
            ctx.log(f"T_ADD layer={t_layers+1}")
        return

    # ── T仓入场 ───────────────────────────────────────────
    if trend != 'up' or not above_vwap or not atr_ok:
        return

    vwap_reclaim = prev.low < vwap and bar.close > vwap
    momentum     = macd_positive and vol_spike and bar.close > prev.close * 1.002
    j_ok         = jval is None or jval < 70

    if vwap_reclaim and j_ok and vol_spike:
        ctx.buy(pct=t_pct, reason="t_vwap_reclaim")
        ctx.state['t_layers']         = 1
        ctx.state['t_entry_stop_pct'] = (daily_atr * atr_sl_mult) if daily_atr else 0.035
        ctx.state['t_entry_price']    = bar.close
        _j_str = f"{jval:.1f}" if jval is not None else "N/A"
        ctx.log(f"T_BUY vwap_reclaim j={_j_str} vol={bar.volume/avg_vol:.2f}x")
    elif momentum and j_ok:
        ctx.buy(pct=t_pct, reason="t_momentum")
        ctx.state['t_layers']         = 1
        ctx.state['t_entry_stop_pct'] = (daily_atr * atr_sl_mult) if daily_atr else 0.035
        ctx.state['t_entry_price']    = bar.close
        ctx.log(f"T_BUY momentum macd={macd_positive}")

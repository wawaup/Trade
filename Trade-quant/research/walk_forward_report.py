"""
Walk-Forward 交互式 HTML 报告生成器

对每个 OOS 时间窗口跑基准策略回测，生成单页 HTML：
  - 窗口切换 tab（每个 OOS 期一个 tab）
  - 标的切换 tab（5 只研究标的）
  - K 线图 + MA5/MA20 + 买卖三角标注（IS 背景灰色，OOS 正常色）
  - 净值对比曲线（策略 vs 持有）
  - 完整交易记录表

用法：
    python walk_forward_report.py
    python walk_forward_report.py --out my_report.html
"""

import argparse
import json
import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from backtest_engine import StrategyConfig, auto_classify, compute_grid, load_raw, compute_indicators, run_backtest, compute_market_regime
from walk_forward import WFConfig, _generate_windows

warnings.filterwarnings("ignore")

# ── 研究标的池 ─────────────────────────────────────────────────────────────
WF_SYMBOLS = [
    ("AVGO", "AVGO 博通",   "AI芯片"),
    ("MU",   "MU 美光",     "HBM存储"),
    ("AXTI", "AXTI",        "光通信CPO"),
    ("MSFT", "MSFT 微软",   "AI云平台"),
    ("TSLA", "TSLA 特斯拉", "消费科技"),
]

DEFAULT_OUT = Path(__file__).parent / "wf_report.html"

# ── 数据收集 ──────────────────────────────────────────────────────────────

def _ts(t) -> int:
    """Timestamp → UTC unix 秒（Lightweight Charts 格式）"""
    ts = pd.Timestamp(t)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    return int(ts.timestamp())


def collect_data(wf_cfg: WFConfig, max_windows: int = 0, d_trend_min_mas: int = 4) -> dict:
    windows = _generate_windows(wf_cfg)
    if max_windows > 0:
        windows = windows[:max_windows]
    print(f"  共生成 {len(windows)} 个 OOS 窗口\n  预加载数据...", flush=True)

    # 加载 SPY 日线数据，计算市场 regime（周收盘>MA20）
    spy_regime = None
    try:
        spy_path = Path(__file__).parent.parent / "data" / "spy_1d_raw.parquet"
        spy_df = pd.read_parquet(spy_path)
        spy_regime = compute_market_regime(spy_df)
        print(f"    SPY      市场 regime 已加载 (周线>MA20 过滤)", flush=True)
    except Exception as e:
        print(f"    SPY      regime 加载失败，跳过大盘过滤: {e}", flush=True)

    raw_cache: dict = {}
    df_cache:  dict = {}
    for sym, name, _ in WF_SYMBOLS:
        try:
            raw = load_raw(sym, "1h")
            raw_cache[sym] = raw
            df_cache[sym]  = compute_indicators(raw, market_regime=spy_regime)
            print(f"    {sym:8s}  {len(raw)} 根 K 线", flush=True)
        except Exception as e:
            print(f"    {sym:8s}  跳过: {e}", flush=True)

    all_windows = []

    for wi, (is_s, is_e, oos_s, oos_e) in enumerate(windows):
        print(f"\n  [{wi+1}/{len(windows)}] IS={is_s}~{is_e}  OOS={oos_s}~{oos_e}", flush=True)
        # K 线图展示：OOS 前 4 周作为 IS 背景（灰色），IS 数据分析用下方 is_s:is_e
        ctx_start = (pd.Timestamp(oos_s) - pd.DateOffset(weeks=4)).strftime("%Y-%m-%d")

        sym_results: dict = {}
        for sym, name, sector in WF_SYMBOLS:
            if sym not in df_cache:
                continue

            df_full  = df_cache[sym]
            raw_full = raw_cache[sym]
            df_oos   = df_full.loc[oos_s:oos_e]

            if len(df_oos) < 20:
                continue

            # 动态分类 + 网格参数（volatile_vol 专用）
            df_is  = df_full.loc[is_s:is_e]
            stype  = auto_classify(df_is)
            g_up, g_lo = compute_grid(df_is) if stype == "volatile_vol" else (0.0, 0.0)
            cfg = StrategyConfig(stock_type=stype, core_mode="auto",
                                 grid_upper=g_up, grid_lower=g_lo,
                                 entry_signal="d_ma",
                                 d_trend_min_mas=d_trend_min_mas)
            try:
                trades, eq_series = run_backtest(df_oos, cfg)
            except Exception as e:
                print(f"    {sym} 回测失败: {e}", flush=True)
                continue

            ini        = cfg.initial_capital
            hold_start = df_oos["close"].iloc[0]
            hold_ret   = (df_oos["close"].iloc[-1] / hold_start - 1) * cfg.core_pct * 100

            if not trades:
                # 无入场信号：净值保持 100，α = -hold_ret（没拿到涨幅就是机会成本）
                strat_ret = 0.0
                alpha     = strat_ret - hold_ret
                strat_mdd = 0.0
                eq_series = pd.Series(ini, index=df_oos.index)
                t_buys, t_exits, t_pnls, t_wr = [], [], [], 0.0
            else:
                # ── 指标 ──────────────────────────────────────────────────
                strat_ret = (trades[-1].equity / ini - 1) * 100
                alpha     = strat_ret - hold_ret
                dd        = (eq_series - eq_series.cummax()) / eq_series.cummax() * 100
                strat_mdd = round(float(dd.min()), 1)

                t_buys  = [t for t in trades if t.action == "T_BUY"]
                t_exits = [t for t in trades if t.action in ("T_TP", "T_STOP", "T_EOD")]
                t_pnls  = [t.pnl_pct for t in t_exits if t.pnl_pct is not None]
                t_wr    = (sum(1 for p in t_pnls if p > 0) / len(t_pnls) * 100) if t_pnls else 0

            # ── 日K OHLCV (IS背景 + OOS) ──────────────────────────────────
            oob_ts   = _ts(oos_s)
            raw_view = raw_full.loc[ctx_start:oos_e]
            daily_raw = raw_view[["open","high","low","close","volume"]].resample("1D").agg({
                "open": "first", "high": "max", "low": "min",
                "close": "last", "volume": "sum"
            }).dropna()
            ohlcv = []
            for idx, row in daily_raw.iterrows():
                ohlcv.append({
                    "time":       _ts(idx),
                    "open":       round(float(row["open"]),   4),
                    "high":       round(float(row["high"]),   4),
                    "low":        round(float(row["low"]),    4),
                    "close":      round(float(row["close"]),  4),
                    "volume":     int(row["volume"]),
                    "is_context": _ts(idx) < oob_ts,
                })

            # ── 日线MA（IS背景 + OOS）─────────────────────────────────────
            df_view = df_full.loc[ctx_start:oos_e]
            # 聚合日K收盘价计算日线MA（和 backtest_engine 中保持一致）
            d_close = df_view["close"].resample("1D").last().dropna()
            d_ma5_s  = d_close.rolling(5,  min_periods=4).mean()
            d_ma10_s = d_close.rolling(10, min_periods=8).mean()
            d_ma20_s = d_close.rolling(20, min_periods=15).mean()
            ma5  = [{"time": _ts(t), "value": round(float(v), 4)}
                    for t, v in d_ma5_s.dropna().items()]
            ma10 = [{"time": _ts(t), "value": round(float(v), 4)}
                    for t, v in d_ma10_s.dropna().items()]
            ma20 = [{"time": _ts(t), "value": round(float(v), 4)}
                    for t, v in d_ma20_s.dropna().items()]

            # MA60 / MA250：需要更长历史才能算准，从 df_full 完整历史算再截断
            d_close_full = df_full.loc[:oos_e]["close"].resample("1D").last().dropna()
            d_ma60_s  = d_close_full.rolling(60,  min_periods=40).mean()
            d_ma250_s = d_close_full.rolling(250, min_periods=200).mean()
            # 截断到展示范围
            ma60  = [{"time": _ts(t), "value": round(float(v), 4)}
                     for t, v in d_ma60_s.loc[ctx_start:oos_e].dropna().items()]
            ma250 = [{"time": _ts(t), "value": round(float(v), 4)}
                     for t, v in d_ma250_s.loc[ctx_start:oos_e].dropna().items()]

            # 周K / 月K：用全量历史（更大视野）
            def _ohlcv_resample(raw, freq, oos_ts):
                """将 1H 原始数据重采样为周/月 OHLCV，带 is_context 标记"""
                agg = raw[["open","high","low","close","volume"]].resample(
                    freq, closed="left", label="left"
                ).agg({"open":"first","high":"max","low":"min","close":"last","volume":"sum"}).dropna()
                result = []
                for idx, row in agg.iterrows():
                    result.append({
                        "time":       _ts(idx),
                        "open":       round(float(row["open"]),  4),
                        "high":       round(float(row["high"]),  4),
                        "low":        round(float(row["low"]),   4),
                        "close":      round(float(row["close"]), 4),
                        "volume":     int(row["volume"]),
                        "is_context": _ts(idx) < oos_ts,
                    })
                return result

            ohlcv_w = _ohlcv_resample(raw_full.loc[:oos_e], "W-MON", oob_ts)
            ohlcv_m = _ohlcv_resample(raw_full.loc[:oos_e], "MS",    oob_ts)

            # 周K MA
            w_close = raw_full.loc[:oos_e]["close"].resample("W-MON", closed="left", label="left").last().dropna()
            ma_w5  = [{"time":_ts(t),"value":round(float(v),4)} for t,v in w_close.rolling(5, min_periods=3).mean().dropna().items()]
            ma_w10 = [{"time":_ts(t),"value":round(float(v),4)} for t,v in w_close.rolling(10,min_periods=7).mean().dropna().items()]
            ma_w20 = [{"time":_ts(t),"value":round(float(v),4)} for t,v in w_close.rolling(20,min_periods=15).mean().dropna().items()]

            # 月K MA
            m_close = raw_full.loc[:oos_e]["close"].resample("MS").last().dropna()
            ma_m5  = [{"time":_ts(t),"value":round(float(v),4)} for t,v in m_close.rolling(5, min_periods=3).mean().dropna().items()]
            ma_m10 = [{"time":_ts(t),"value":round(float(v),4)} for t,v in m_close.rolling(10,min_periods=7).mean().dropna().items()]

            # ── 净值曲线（基准 100） ──────────────────────────────────────
            eq_pts  = [{"time": _ts(t), "value": round(v / ini * 100, 2)}
                       for t, v in eq_series.items()]
            hld_pts = []
            for t, c in df_oos["close"].items():
                hv = (1 - cfg.core_pct) * 100 + cfg.core_pct * 100 * (c / hold_start)
                hld_pts.append({"time": _ts(t), "value": round(hv, 2)})

            # ── 交易标记 ──────────────────────────────────────────────────
            BUY_ACTIONS  = {"CORE_BUY", "CORE_ADD", "T_BUY", "T_ADD"}
            SELL_ACTIONS = {"CORE_STOP", "CORE_CUT", "CORE_EOD", "T_TP", "T_STOP", "T_EOD"}
            LABEL_MAP    = {
                "CORE_BUY": "C↑", "CORE_ADD": "C+",
                "T_BUY": "T↑", "T_ADD": "T+",
                "CORE_STOP": "CS", "CORE_CUT": "C-", "CORE_EOD": "CE",
                "T_TP": "TP", "T_STOP": "TS", "T_EOD": "TE",
            }

            # 日K图表：标注时间需 floor 到当天 00:00，否则无法对齐日K柱
            def _ts_day(t):
                return _ts(pd.Timestamp(t).floor("1D"))

            markers = []
            for tr in trades:
                if tr.action in BUY_ACTIONS:
                    markers.append({
                        "time": _ts_day(tr.time), "position": "belowBar",
                        "color": "#34d399",   "shape": "arrowUp",
                        "text": LABEL_MAP.get(tr.action, "B"),
                    })
                elif tr.action in SELL_ACTIONS:
                    pnl_s = (f" {tr.pnl_pct*100:+.1f}%"
                             if tr.pnl_pct is not None else "")
                    markers.append({
                        "time": _ts_day(tr.time), "position": "aboveBar",
                        "color": "#f87171",   "shape": "arrowDown",
                        "text": LABEL_MAP.get(tr.action, "S") + pnl_s,
                    })

            # ── 交易日志 ──────────────────────────────────────────────────
            log = []
            for tr in trades:
                log.append({
                    "time":   str(tr.time)[:16],
                    "day_ts": _ts_day(tr.time),   # 用于 hover 对应日K
                    "action": tr.action,
                    "price":  round(float(tr.price), 2),
                    "size":   round(float(tr.size), 4),
                    "pnl":    (f"{tr.pnl_pct*100:+.2f}%"
                               if tr.pnl_pct is not None else "—"),
                    "reason": tr.reason[:60],
                })

            sym_results[sym] = {
                "sym": sym, "name": name, "sector": sector, "stype": stype,
                "metrics": {
                    "strat_return": round(strat_ret, 1),
                    "hold_return":  round(hold_ret,  1),
                    "alpha":        round(alpha,      1),
                    "strat_mdd":    strat_mdd,
                    "t_entries":    len(t_buys),
                    "t_winrate":    round(t_wr, 0),
                },
                "ohlcv":        ohlcv,
                "ma5":          ma5,
                "ma10":         ma10,
                "ma20":         ma20,
                "ma60":         ma60,
                "ma250":        ma250,
                "ohlcv_w":      ohlcv_w,
                "ohlcv_m":      ohlcv_m,
                "ma_w5":        ma_w5,
                "ma_w10":       ma_w10,
                "ma_w20":       ma_w20,
                "ma_m5":        ma_m5,
                "ma_m10":       ma_m10,
                "equity":       eq_pts,
                "hold":         hld_pts,
                "markers":      markers,
                "trade_log":    log,
                "oos_boundary": oob_ts,
            }

            no_trade_tag = "  ⚠ 无入场信号" if not trades else ""
            print(f"    {sym:8s} [{stype}]  strat={strat_ret:+.1f}%  hold={hold_ret:+.1f}%  "
                  f"α={alpha:+.1f}%  mdd={strat_mdd:.1f}%{no_trade_tag}", flush=True)

        all_windows.append({
            "id":       f"w{wi}",
            "label":    f"W{wi+1} {oos_s[:7]}～{oos_e[5:7]}",
            "is_start": is_s, "is_end": is_e,
            "oos_start": oos_s, "oos_end": oos_e,
            "params":   "baseline（当前策略）",
            "symbols":  sym_results,
        })

    return {
        "windows":  all_windows,
        "sym_order": [s[0] for s in WF_SYMBOLS],
    }


# ── HTML 生成 ─────────────────────────────────────────────────────────────

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Walk-Forward 策略分析报告</title>
<script src="https://unpkg.com/lightweight-charts@4.1.3/dist/lightweight-charts.standalone.production.js"></script>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'PingFang SC','Microsoft YaHei',sans-serif;background:#0f1117;color:#e2e8f0;font-size:13px}
h1{font-size:20px;font-weight:700;padding:20px 24px 4px;color:#f0f4ff}
.subtitle{padding:0 24px 16px;color:#8892a4;font-size:12px}

/* Window tabs */
.wtabs{display:flex;gap:4px;padding:0 24px 12px;flex-wrap:wrap}
.wtab{padding:6px 14px;border-radius:6px;cursor:pointer;background:#1e2230;
      color:#8892a4;border:1px solid #2d3348;font-size:12px;transition:.15s;white-space:nowrap}
.wtab.active{background:#3b55e6;color:#fff;border-color:#3b55e6}
.wtab:hover:not(.active){background:#252d40}

/* Window sections */
.wsec{display:none;padding:0 24px 32px}
.wsec.active{display:block}

/* Window header */
.whead{background:#1a1e2e;border:1px solid #2d3348;border-radius:8px;
       padding:12px 16px;margin-bottom:14px}
.whead-top{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px}
.whead-label{font-size:15px;font-weight:600;color:#c9d1e0}
.whead-params{font-size:11px;color:#8892a4;background:#0f1117;padding:4px 10px;border-radius:4px}
.whead-metrics{display:flex;gap:16px}
.wm{text-align:center}
.wm .val{font-size:18px;font-weight:700}
.wm .lbl{font-size:11px;color:#8892a4;margin-top:2px}
.pos{color:#34d399}.neg{color:#f87171}.neu{color:#93c5fd}

/* Symbol tabs */
.stabs{display:flex;gap:4px;margin-bottom:12px}
.stab{padding:5px 14px;border-radius:6px;cursor:pointer;background:#1e2230;
      color:#8892a4;border:1px solid #2d3348;font-size:12px;transition:.15s}
.stab.active{background:#1e3a5f;color:#93c5fd;border-color:#3b82f6}
.stab:hover:not(.active){background:#252d40}

/* Chart view toggle buttons */
.view-btns{display:flex;gap:4px;margin-bottom:6px}
.vbtn{padding:3px 10px;border-radius:4px;cursor:pointer;font-size:11px;border:1px solid #2d3348;background:#1e2230;color:#8892a4}
.vbtn.active{background:#1e3a5f;color:#93c5fd;border-color:#3b82f6}
.vbtn:hover:not(.active){background:#252d40}

/* Symbol panels */
.spanel{display:none}
.spanel.active{display:block}

/* Main layout */
.panel-grid{display:grid;grid-template-columns:1fr 220px;gap:14px;margin-bottom:12px}

/* Charts */
.charts-col{display:flex;flex-direction:column;gap:8px}
.kline-wrap{background:#12151f;border:1px solid #2d3348;border-radius:8px;
            padding:10px;position:relative}
.kline-wrap h4{font-size:12px;color:#8892a4;margin-bottom:6px}
.kline-legend{display:flex;gap:12px;font-size:11px;margin-bottom:4px}
.leg-item{display:flex;align-items:center;gap:4px}
.leg-dot{width:10px;height:2px;border-radius:1px}
.bottom-row{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-top:14px}
.eq-wrap{background:#12151f;border:1px solid #2d3348;border-radius:8px;padding:10px}
.eq-wrap h4{font-size:12px;color:#8892a4;margin-bottom:6px}

/* Metrics card */
.metrics-card{background:#1a1e2e;border:1px solid #2d3348;border-radius:8px;padding:14px}
.metrics-card h4{font-size:12px;color:#8892a4;margin-bottom:10px;text-transform:uppercase;letter-spacing:.5px}
.met-row{display:flex;justify-content:space-between;align-items:center;
         padding:5px 0;border-bottom:1px solid #1e2230}
.met-row:last-child{border-bottom:none}
.met-lbl{color:#8892a4;font-size:12px}
.met-val{font-size:14px;font-weight:600}
.badge{display:inline-block;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:600;margin-top:6px}
.badge-pos{background:#064e3b;color:#34d399}
.badge-neg{background:#450a0a;color:#f87171}

/* Trade log */
.tlog-wrap{background:#12151f;border:1px solid #2d3348;border-radius:8px;overflow:hidden}
.tlog-wrap h4{font-size:12px;color:#8892a4;padding:10px 14px;border-bottom:1px solid #2d3348}
.tlog{width:100%;border-collapse:collapse}
.tlog th{background:#1a1e2e;padding:7px 12px;text-align:left;font-size:11px;
         color:#8892a4;font-weight:500;border-bottom:1px solid #2d3348}
.tlog td{padding:6px 12px;font-size:12px;border-bottom:1px solid #1a1e2e}
.tlog tr:hover td{background:#1a1e2e}
.act-buy{color:#34d399;font-weight:600}
.act-sell{color:#f87171;font-weight:600}
.kline-tooltip{position:absolute;top:8px;left:8px;z-index:10;pointer-events:none;
  background:rgba(18,21,31,.88);border:1px solid #2d3348;border-radius:6px;
  padding:6px 10px;font-size:12px;line-height:1.7;white-space:nowrap;color:#c8d0e0}
.kline-tooltip .tt-date{color:#8892a4;font-size:11px;margin-bottom:2px}
.kline-tooltip .tt-ohlc span{margin-right:10px}
.kline-tooltip .tt-up{color:#34d399}.kline-tooltip .tt-dn{color:#f87171}
.kline-tooltip .tt-ma{color:#8892a4;font-size:11px}
.kline-tooltip .tt-ma span{margin-right:8px}
.kline-tooltip .tt-trade{margin-top:3px;font-size:11px;border-top:1px solid #2d3348;padding-top:3px}
.act-core{color:#93c5fd;font-weight:600}

/* No data */
.nodata{padding:40px;text-align:center;color:#8892a4;font-size:13px}
</style>
</head>
<body>
<h1>Walk-Forward 策略分析报告</h1>
<p class="subtitle">基准策略 · IS=6月 OOS=2月 步进=2月 · 研究标的: AVGO / MU / AXTI / MSFT / TSLA</p>

<div class="wtabs" id="wtabs"></div>
<div id="wsections"></div>

<script>
const DATA = __JSON_DATA__;

// ── Build DOM ─────────────────────────────────────────────────────────────
function buildUI() {
  const tabsEl = document.getElementById('wtabs');
  const secsEl = document.getElementById('wsections');

  DATA.windows.forEach((w, wi) => {
    // Window tab
    const t = document.createElement('button');
    t.className = 'wtab' + (wi === 0 ? ' active' : '');
    t.textContent = w.label;
    t.dataset.wid = w.id;
    t.onclick = () => switchWindow(w.id);
    tabsEl.appendChild(t);

    // Window section
    const sec = document.createElement('div');
    sec.className = 'wsec' + (wi === 0 ? ' active' : '');
    sec.id = 'wsec-' + w.id;

    // Summary metrics across symbols
    const syms = Object.values(w.symbols);
    const avgStr  = syms.length ? (syms.reduce((s,x)=>s+x.metrics.strat_return,0)/syms.length).toFixed(1) : '—';
    const avgHld  = syms.length ? (syms.reduce((s,x)=>s+x.metrics.hold_return,0)/syms.length).toFixed(1) : '—';
    const avgAlp  = syms.length ? (syms.reduce((s,x)=>s+x.metrics.alpha,0)/syms.length).toFixed(1) : '—';
    const alpNum  = parseFloat(avgAlp);

    sec.innerHTML = `
      <div class="whead">
        <div class="whead-top">
          <span class="whead-label">IS: ${w.is_start} ~ ${w.is_end} &nbsp;→&nbsp; OOS: ${w.oos_start} ~ ${w.oos_end}</span>
          <span class="whead-params">${w.params}</span>
        </div>
        <div class="whead-metrics">
          <div class="wm"><div class="val ${parseFloat(avgStr)>=0?'pos':'neg'}">${avgStr}%</div><div class="lbl">OOS 策略均值</div></div>
          <div class="wm"><div class="val neu">${avgHld}%</div><div class="lbl">OOS 持有均值</div></div>
          <div class="wm"><div class="val ${alpNum>=0?'pos':'neg'}">${alpNum>=0?'+':''}${avgAlp}%</div><div class="lbl">均 α</div></div>
          <div class="wm"><div class="val neu">${syms.length}</div><div class="lbl">有效标的</div></div>
        </div>
      </div>`;

    // Symbol tabs
    const stabs = document.createElement('div');
    stabs.className = 'stabs';
    DATA.sym_order.forEach((sym, si) => {
      if (!w.symbols[sym]) return;
      const st = document.createElement('button');
      const sd = w.symbols[sym];
      const a  = sd.metrics.alpha;
      const typeTag = {'large_vol':'📈L','small_vol':'🔵S','volatile_vol':'🌊V'}[sd.stype] || sd.stype;
      st.className = 'stab' + (si === 0 ? ' active' : '');
      st.textContent = `${sym} ${typeTag}  ${a>=0?'+':''}${a.toFixed(1)}%`;
      st.dataset.sym = sym;
      st.onclick = () => switchSymbol(w.id, sym);
      stabs.appendChild(st);
    });
    sec.appendChild(stabs);

    // Symbol panels
    DATA.sym_order.forEach((sym, si) => {
      if (!w.symbols[sym]) return;
      const sd  = w.symbols[sym];
      const m   = sd.metrics;
      const pid = `${w.id}-${sym}`;

      const sp = document.createElement('div');
      sp.className = 'spanel' + (si === 0 ? ' active' : '');
      sp.id = 'sp-' + pid;

      const alpCls = m.alpha >= 0 ? 'pos' : 'neg';
      const retCls = m.strat_return >= 0 ? 'pos' : 'neg';
      const mddCls = 'neg';
      const badgeCls = m.alpha >= 0 ? 'badge-pos' : 'badge-neg';
      const stypeLabel = {'large_vol':'顺势型 📈  金字塔开启','small_vol':'稳健型 🔵  一次建满','volatile_vol':'震荡型 🌊  保守建仓'}[sd.stype] || sd.stype;

      const logRows = sd.trade_log.map(tr => {
        let cls = 'act-core';
        if (tr.action.startsWith('T_'))    cls = tr.action==='T_TP'||tr.action==='T_STOP'?'act-sell':'act-buy';
        else if (tr.action==='CORE_BUY'||tr.action==='CORE_ADD') cls='act-buy';
        else if (tr.action==='CORE_STOP'||tr.action==='CORE_EOD') cls='act-sell';
        return `<tr><td>${tr.time}</td><td class="${cls}">${tr.action}</td>
          <td>${tr.price}</td><td class="${tr.pnl.startsWith('+') ?'pos':'neg'}">${tr.pnl}</td>
          <td style="color:#8892a4;max-width:300px;overflow:hidden">${tr.reason}</td></tr>`;
      }).join('');

      sp.innerHTML = `
        <div class="panel-grid">
          <div class="charts-col">
            <div class="kline-wrap">
              <div class="kline-legend">
                <span style="color:#8892a4;font-size:11px">${sd.name} · ${sd.sector}</span>
                <span class="leg-item"><span class="leg-dot" style="background:#f59e0b"></span>MA5</span>
                <span class="leg-item"><span class="leg-dot" style="background:#34d399"></span>MA10</span>
                <span class="leg-item"><span class="leg-dot" style="background:#8b5cf6"></span>MA20</span>
                <span class="leg-item"><span class="leg-dot" style="background:#fb923c"></span>MA60</span>
                <span class="leg-item"><span class="leg-dot" style="background:#f43f5e"></span>MA250</span>
                <span class="leg-item" style="color:#8892a4">▲ 买入</span>
                <span class="leg-item" style="color:#f87171">▼ 卖出</span>
                <span style="color:#3d4f6b;font-size:11px">| 灰色=IS背景</span>
              </div>
              <div class="view-btns" id="vbtns-${pid}">
                <button class="vbtn active" data-pid="${pid}" data-view="D" onclick="switchChartView('${pid}','D')">日K</button>
                <button class="vbtn"        data-pid="${pid}" data-view="W" onclick="switchChartView('${pid}','W')">周K</button>
                <button class="vbtn"        data-pid="${pid}" data-view="M" onclick="switchChartView('${pid}','M')">月K</button>
              </div>
              <div id="kline-${pid}" style="height:360px"></div>
            </div>
          </div>
          <div>
            <div class="metrics-card">
              <h4>OOS 指标</h4>
              <div class="met-row"><span class="met-lbl">策略收益</span>
                <span class="met-val ${retCls}">${m.strat_return>=0?'+':''}${m.strat_return}%</span></div>
              <div class="met-row"><span class="met-lbl">持有收益</span>
                <span class="met-val neu">${m.hold_return>=0?'+':''}${m.hold_return}%</span></div>
              <div class="met-row"><span class="met-lbl">超额 α</span>
                <span class="met-val ${alpCls}">${m.alpha>=0?'+':''}${m.alpha}%</span></div>
              <div class="met-row"><span class="met-lbl">策略最大回撤</span>
                <span class="met-val ${mddCls}">${m.strat_mdd}%</span></div>
              <div class="met-row"><span class="met-lbl">T 入场次数</span>
                <span class="met-val neu">${m.t_entries}</span></div>
              <div class="met-row"><span class="met-lbl">T 胜率</span>
                <span class="met-val neu">${m.t_winrate}%</span></div>
              <div style="margin-top:10px">
                <span class="badge ${badgeCls}">${m.alpha>=0?'主动管理有效':'不如直接持有'}</span>
              </div>
              <div style="margin-top:8px;font-size:11px;color:#8892a4;line-height:1.5">
                IS 分类<br><span style="color:#c9d1e0">${stypeLabel}</span>
              </div>
            </div>
          </div>
        </div>
        <div class="bottom-row">
          <div class="eq-wrap">
            <h4>净值对比（基准=100）<span style="margin-left:12px;font-size:11px;color:#34d399">— 策略</span>
              <span style="margin-left:8px;font-size:11px;color:#93c5fd">— 持有</span></h4>
            <div id="eq-${pid}" style="height:200px"></div>
          </div>
          <div class="tlog-wrap">
            <h4>交易记录（${sd.trade_log.length} 笔）</h4>
            <div style="overflow-x:auto;max-height:230px;overflow-y:auto">
              <table class="tlog">
                <thead><tr><th>时间</th><th>操作</th><th>价格</th><th>盈亏</th><th>原因</th></tr></thead>
                <tbody>${logRows || '<tr><td colspan="5" style="color:#8892a4;padding:20px;text-align:center">无交易记录</td></tr>'}</tbody>
              </table>
            </div>
          </div>
        </div>`;

      sec.appendChild(sp);
    });

    secsEl.appendChild(sec);
  });
}

// ── Navigation ────────────────────────────────────────────────────────────
const chartsInited = new Set();

function switchWindow(wid) {
  document.querySelectorAll('.wsec').forEach(el => el.classList.remove('active'));
  document.querySelectorAll('.wtab').forEach(el => el.classList.remove('active'));
  const sec = document.getElementById('wsec-' + wid);
  if (sec) sec.classList.add('active');
  const tab = document.querySelector(`.wtab[data-wid="${wid}"]`);
  if (tab) tab.classList.add('active');
  // Init charts for first visible symbol
  const w = DATA.windows.find(x => x.id === wid);
  if (w) {
    const firstSym = DATA.sym_order.find(s => w.symbols[s]);
    if (firstSym) ensureCharts(wid, firstSym);
  }
}

function switchSymbol(wid, sym) {
  const sec = document.getElementById('wsec-' + wid);
  if (!sec) return;
  sec.querySelectorAll('.spanel').forEach(el => el.classList.remove('active'));
  sec.querySelectorAll('.stab').forEach(el => el.classList.remove('active'));
  const sp = document.getElementById(`sp-${wid}-${sym}`);
  if (sp) sp.classList.add('active');
  const stab = sec.querySelector(`.stab[data-sym="${sym}"]`);
  if (stab) stab.classList.add('active');
  ensureCharts(wid, sym);
}

function ensureCharts(wid, sym) {
  const key = `${wid}-${sym}`;
  if (chartsInited.has(key)) return;
  chartsInited.add(key);
  const w  = DATA.windows.find(x => x.id === wid);
  const sd = w && w.symbols[sym];
  if (!sd) return;
  requestAnimationFrame(() => {
    createKlineChart(`kline-${wid}-${sym}`, sd);
    createEqChart(`eq-${wid}-${sym}`, sd);
  });
}

// ── K-line Chart ──────────────────────────────────────────────────────────
const _chartReg = {};   // pid → { chart, isCandles, oosCandles, volSeries, ma5, ma10, ma20, ma60, ma250 }

function createKlineChart(containerId, sd) {
  const el = document.getElementById(containerId);
  if (!el) return;

  const chart = LightweightCharts.createChart(el, {
    layout: { background: { color: '#12151f' }, textColor: '#8892a4' },
    grid:   { vertLines: { color: '#1a1f2e' }, horzLines: { color: '#1a1f2e' } },
    crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
    rightPriceScale: { borderColor: '#2d3348' },
    timeScale: { borderColor: '#2d3348', timeVisible: true, secondsVisible: false },
  });

  // IS 背景（灰色蜡烛）
  const isCandles = chart.addCandlestickSeries({
    upColor: '#2d3a4a', downColor: '#2d3a4a',
    borderUpColor: '#3d4f63', borderDownColor: '#3d4f63',
    wickUpColor: '#3d4f63', wickDownColor: '#3d4f63',
    lastValueVisible: false, priceLineVisible: false,
  });
  isCandles.setData(
    sd.ohlcv.filter(d => d.is_context).map(d => ({
      time: d.time, open: d.open, high: d.high, low: d.low, close: d.close
    }))
  );

  // OOS 蜡烛（正常色）
  const oosCandles = chart.addCandlestickSeries({
    upColor: '#34d399', downColor: '#f87171',
    borderUpColor: '#34d399', borderDownColor: '#f87171',
    wickUpColor: '#34d399', wickDownColor: '#f87171',
  });
  const oosData = sd.ohlcv.filter(d => !d.is_context);
  oosCandles.setData(
    oosData.map(d => ({ time: d.time, open: d.open, high: d.high, low: d.low, close: d.close }))
  );

  // 成交量 histogram（叠在主图底部 20%）
  const volSeries = chart.addHistogramSeries({
    color: '#3b55e620',
    priceFormat: { type: 'volume' },
    priceScaleId: 'vol',
    scaleMargins: { top: 0.82, bottom: 0 },
  });
  volSeries.setData(sd.ohlcv.map(d => ({
    time: d.time,
    value: d.volume,
    color: d.is_context ? '#2d3a4a30' : (d.close >= d.open ? '#34d39940' : '#f8717140'),
  })));

  // MA5
  const ma5Ser = chart.addLineSeries({ color: '#f59e0b', lineWidth: 1, lastValueVisible: false, priceLineVisible: false });
  if (sd.ma5 && sd.ma5.length) ma5Ser.setData(sd.ma5);

  // MA10
  const ma10Ser = chart.addLineSeries({ color: '#34d399', lineWidth: 1, lastValueVisible: false, priceLineVisible: false });
  if (sd.ma10 && sd.ma10.length) ma10Ser.setData(sd.ma10);

  // MA20
  const ma20Ser = chart.addLineSeries({ color: '#8b5cf6', lineWidth: 1, lastValueVisible: false, priceLineVisible: false });
  if (sd.ma20 && sd.ma20.length) ma20Ser.setData(sd.ma20);

  // MA60
  const ma60Series = chart.addLineSeries({ color: '#fb923c', lineWidth: 1, lastValueVisible: false, priceLineVisible: false });
  if (sd.ma60 && sd.ma60.length) ma60Series.setData(sd.ma60);

  // MA250
  const ma250Series = chart.addLineSeries({ color: '#f43f5e', lineWidth: 1, lastValueVisible: false, priceLineVisible: false });
  if (sd.ma250 && sd.ma250.length) ma250Series.setData(sd.ma250);

  // 买卖标记
  if (sd.markers.length) {
    oosCandles.setMarkers(sd.markers);
  }

  // ── Hover Tooltip ────────────────────────────────────────────────────────
  // 构建 time → MA 值快查表
  const ma5Map = {}, ma10Map = {}, ma20Map = {}, ma60Map = {}, ma250Map = {};
  (sd.ma5   || []).forEach(d => { ma5Map[d.time]   = d.value; });
  (sd.ma10  || []).forEach(d => { ma10Map[d.time]  = d.value; });
  (sd.ma20  || []).forEach(d => { ma20Map[d.time]  = d.value; });
  (sd.ma60  || []).forEach(d => { ma60Map[d.time]  = d.value; });
  (sd.ma250 || []).forEach(d => { ma250Map[d.time] = d.value; });
  // 构建 day_ts → [trades] 快查表
  const tradeMap = {};
  (sd.trade_log || []).forEach(tr => {
    const k = tr.day_ts;
    if (!tradeMap[k]) tradeMap[k] = [];
    tradeMap[k].push(tr);
  });
  // 构建 time → OHLCV（全部蜡烛含灰区）
  const ohlcvMap = {};
  (sd.ohlcv || []).forEach(d => { ohlcvMap[d.time] = d; });

  // 创建 tooltip DOM
  const tooltip = document.createElement('div');
  tooltip.className = 'kline-tooltip';
  tooltip.style.display = 'none';
  el.style.position = 'relative';
  el.appendChild(tooltip);

  const BUY_ACTIONS  = new Set(['CORE_BUY','CORE_ADD','T_BUY','T_ADD']);
  const SELL_ACTIONS = new Set(['CORE_STOP','CORE_CUT','CORE_EOD','T_TP','T_STOP','T_EOD']);

  chart.subscribeCrosshairMove(param => {
    if (!param.time || !param.point) { tooltip.style.display = 'none'; return; }
    const t   = param.time;
    const bar = ohlcvMap[t];
    if (!bar) { tooltip.style.display = 'none'; return; }

    const chg   = ((bar.close - bar.open) / bar.open * 100);
    const chgCls = chg >= 0 ? 'tt-up' : 'tt-dn';
    const chgStr = (chg >= 0 ? '+' : '') + chg.toFixed(2) + '%';

    // 日期行
    const d = new Date(t * 1000);
    const dateStr = d.toISOString().slice(0, 10);

    // OHLC 行
    const ohlcHtml = `<div class="tt-ohlc">
      <span>开<b>${bar.open}</b></span>
      <span>高<b>${bar.high}</b></span>
      <span>低<b>${bar.low}</b></span>
      <span>收<b>${bar.close}</b></span>
      <span class="${chgCls}"><b>${chgStr}</b></span>
    </div>`;

    // MA 行
    const m5   = ma5Map[t]   ? 'MA5:<b>'   + ma5Map[t].toFixed(2)   + '</b>' : '';
    const m10  = ma10Map[t]  ? 'MA10:<b>'  + ma10Map[t].toFixed(2)  + '</b>' : '';
    const m20  = ma20Map[t]  ? 'MA20:<b>'  + ma20Map[t].toFixed(2)  + '</b>' : '';
    const m60  = ma60Map[t]  ? 'MA60:<b>'  + ma60Map[t].toFixed(2)  + '</b>' : '';
    const m250 = ma250Map[t] ? 'MA250:<b>' + ma250Map[t].toFixed(2) + '</b>' : '';
    const maHtml = (m5 || m10 || m20 || m60 || m250)
      ? `<div class="tt-ma"><span>${m5}</span><span>${m10}</span><span>${m20}</span><span>${m60}</span><span>${m250}</span></div>`
      : '';

    // 交易行（当日所有操作）
    const trs = tradeMap[t] || [];
    const trHtml = trs.map(tr => {
      const isBuy  = BUY_ACTIONS.has(tr.action);
      const isSell = SELL_ACTIONS.has(tr.action);
      const cls = isBuy ? 'tt-up' : (isSell ? 'tt-dn' : '');
      const arrow = isBuy ? '▲' : (isSell ? '▼' : '●');
      const pnlPart = tr.pnl !== '—' ? ` <span class="${isSell && tr.pnl.startsWith('+') ? 'tt-up' : (isSell ? 'tt-dn' : '')}">${tr.pnl}</span>` : '';
      return `<div class="tt-trade"><span class="${cls}">${arrow} ${tr.action}</span> &nbsp;
        价:<b>$${tr.price}</b> &nbsp; 量:<b>${tr.size.toFixed(2)}</b>${pnlPart}</div>`;
    }).join('');

    tooltip.innerHTML = `<div class="tt-date">${dateStr}</div>${ohlcHtml}${maHtml}${trHtml}`;
    tooltip.style.display = 'block';
  });

  // Store series refs for switchChartView
  const _pid = containerId.replace('kline-', '');
  _chartReg[_pid] = { chart, isCandles, oosCandles, volSeries, ma5: ma5Ser, ma10: ma10Ser, ma20: ma20Ser, ma60: ma60Series, ma250: ma250Series };

  chart.timeScale().fitContent();
}

// ── Switch Chart View (D/W/M) ─────────────────────────────────────────────
function switchChartView(pid, view) {
  // Update button states
  document.querySelectorAll(`#vbtns-${pid} .vbtn`).forEach(b =>
    b.classList.toggle('active', b.dataset.view === view));

  const refs = _chartReg[pid];
  if (!refs) return;

  // Find sd from DATA
  let sd = null;
  for (const w of DATA.windows) {
    for (const sym of DATA.sym_order) {
      if (w.symbols[sym] && `${w.id}-${sym}` === pid) { sd = w.symbols[sym]; break; }
    }
    if (sd) break;
  }
  if (!sd) return;

  const ohlcv  = view === 'D' ? sd.ohlcv   : (view === 'W' ? sd.ohlcv_w  : sd.ohlcv_m);
  const maData = view === 'D'
    ? { ma5: sd.ma5,   ma10: sd.ma10,  ma20: sd.ma20,  ma60: sd.ma60,  ma250: sd.ma250 }
    : view === 'W'
    ? { ma5: sd.ma_w5, ma10: sd.ma_w10,ma20: sd.ma_w20,ma60: [],       ma250: [] }
    : { ma5: sd.ma_m5, ma10: sd.ma_m10,ma20: [],        ma60: [],       ma250: [] };

  const toBar = d => ({ time: d.time, open: d.open, high: d.high, low: d.low, close: d.close });
  refs.isCandles.setData(ohlcv.filter(d =>  d.is_context).map(toBar));
  refs.oosCandles.setData(ohlcv.filter(d => !d.is_context).map(toBar));
  refs.volSeries.setData(ohlcv.map(d => ({
    time: d.time, value: d.volume,
    color: d.is_context ? '#2d3a4a30' : (d.close >= d.open ? '#34d39940' : '#f8717140'),
  })));

  if (refs.ma5)   refs.ma5.setData(maData.ma5   || []);
  if (refs.ma10)  refs.ma10.setData(maData.ma10  || []);
  if (refs.ma20)  refs.ma20.setData(maData.ma20  || []);
  if (refs.ma60)  refs.ma60.setData(maData.ma60  || []);
  if (refs.ma250) refs.ma250.setData(maData.ma250|| []);

  // Re-apply markers floored to correct bar granularity
  function floorToWeek(ts) {
    const d = new Date(ts * 1000);
    const dow = d.getUTCDay();
    d.setUTCDate(d.getUTCDate() + (dow === 0 ? -6 : 1 - dow));
    d.setUTCHours(0, 0, 0, 0);
    return Math.floor(d.getTime() / 1000);
  }
  function floorToMonth(ts) {
    const d = new Date(ts * 1000);
    d.setUTCDate(1); d.setUTCHours(0, 0, 0, 0);
    return Math.floor(d.getTime() / 1000);
  }
  const floorFn = view === 'W' ? floorToWeek : (view === 'M' ? floorToMonth : null);
  if (floorFn && sd.markers) {
    refs.oosCandles.setMarkers(sd.markers.map(m => ({...m, time: floorFn(m.time)})));
  } else if (sd.markers) {
    refs.oosCandles.setMarkers(sd.markers);
  }

  refs.chart.timeScale().fitContent();
}

// ── Equity Chart (Lightweight Charts，与K线用同一库，无 adapter 依赖) ─────
function createEqChart(containerId, sd) {
  const el = document.getElementById(containerId);
  if (!el) return;

  const chart = LightweightCharts.createChart(el, {
    layout: { background: { color: '#12151f' }, textColor: '#8892a4' },
    grid:   { vertLines: { color: '#1a1f2e' }, horzLines: { color: '#1a1f2e' } },
    rightPriceScale: { borderColor: '#2d3348' },
    timeScale: { borderColor: '#2d3348', timeVisible: false, fixLeftEdge: true, fixRightEdge: true },
    crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
    handleScroll: false,
    handleScale:  false,
  });

  const fixedRange = () => ({ priceRange: { minValue: 80, maxValue: 140 } });

  const stratLine = chart.addLineSeries({
    color: '#34d399', lineWidth: 2,
    lastValueVisible: false, priceLineVisible: false,
    autoscaleInfoProvider: fixedRange,
  });
  stratLine.setData(sd.equity);

  // 每5单位一条参考横线（10的倍数显示轴标，其余虚线辅助）
  for (let lvl = 80; lvl <= 140; lvl += 5) {
    stratLine.createPriceLine({
      price: lvl,
      color: lvl % 10 === 0 ? '#2d3550' : '#1c2235',
      lineWidth: 1,
      lineStyle: lvl % 10 === 0 ? LightweightCharts.LineStyle.Solid : LightweightCharts.LineStyle.Dotted,
      axisLabelVisible: lvl % 10 === 0,
      title: '',
    });
  }

  const holdLine = chart.addLineSeries({
    color: '#93c5fd', lineWidth: 2,
    lastValueVisible: false, priceLineVisible: false,
    autoscaleInfoProvider: fixedRange,
  });
  holdLine.setData(sd.hold);

  chart.timeScale().fitContent();
}

// ── Init ──────────────────────────────────────────────────────────────────
buildUI();
// Init charts for first window + first symbol
if (DATA.windows.length) {
  const w0 = DATA.windows[0];
  const s0 = DATA.sym_order.find(s => w0.symbols[s]);
  if (s0) ensureCharts(w0.id, s0);
}
</script>
</body>
</html>
"""


def generate_report(data: dict, out_path: Path):
    json_str = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    html = HTML_TEMPLATE.replace("__JSON_DATA__", json_str)
    out_path.write_text(html, encoding="utf-8")
    print(f"\n  报告已生成: {out_path}  ({out_path.stat().st_size // 1024} KB)")


# ── CLI 入口 ──────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Walk-Forward HTML 报告生成器")
    parser.add_argument("--out",         type=str, default=str(DEFAULT_OUT))
    parser.add_argument("--is",          type=int, default=6,  dest="is_months")
    parser.add_argument("--oos",         type=int, default=2,  dest="oos_months")
    parser.add_argument("--step",        type=int, default=1,  dest="step_months")
    parser.add_argument("--max-windows",    type=int, default=0,  dest="max_windows",
                        help="只跑前N个窗口（0=全跑）")
    parser.add_argument("--d-trend-min-mas", type=int, default=4, dest="d_trend_min_mas",
                        help="趋势过滤档位 2=MA5+MA10 3=+MA20 4=+MA30(默认) 5=+MA60")
    args = parser.parse_args()

    print("\n" + "═" * 60)
    print("  Walk-Forward HTML 报告生成器")
    print(f"  IS={args.is_months}月  OOS={args.oos_months}月  步进={args.step_months}月")
    print(f"  趋势过滤档位: d_trend_min_mas={args.d_trend_min_mas}")
    if args.max_windows:
        print(f"  只生成前 {args.max_windows} 个窗口")
    print(f"  标的: {[s[0] for s in WF_SYMBOLS]}  (stock_type 动态分类)")
    print("═" * 60)

    cfg = WFConfig(is_months=args.is_months, oos_months=args.oos_months,
                   step_months=args.step_months)
    data = collect_data(cfg, max_windows=args.max_windows,
                        d_trend_min_mas=args.d_trend_min_mas)
    generate_report(data, Path(args.out))


if __name__ == "__main__":
    main()

"""
生成本地 HTML 回测报告。

用法：
    python generate_report.py               # 输出 report.html
    python generate_report.py --out my.html
"""
import argparse
import json
import math
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from compare_configs import (
    SYMBOLS, SYMBOLS_SERENITY, PERIODS, run_and_decompose,
)

OUT_DIR = Path(__file__).parent


def collect_data():
    all_data = {}
    for period_label, since, until in PERIODS:
        rows_main = []
        for symbol, stype, name, sector in SYMBOLS:
            print(f"  [{period_label}] {symbol} ...", flush=True)
            res = run_and_decompose(symbol, stype, since, until)
            rows_main.append({"symbol": symbol, "type": stype, "name": name, "sector": sector,
                              **({} if res is None else res)})
        rows_ser = []
        for symbol, stype, name, sector in SYMBOLS_SERENITY:
            print(f"  [{period_label}] serenity/{symbol} ...", flush=True)
            res = run_and_decompose(symbol, stype, since, until)
            rows_ser.append({"symbol": symbol, "type": stype, "name": name, "sector": sector,
                             **({} if res is None else res)})
        all_data[period_label] = {
            "since": since, "until": until or "2026-06",
            "rows": rows_main, "rows_serenity": rows_ser,
        }
    return all_data


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>策略回测报告</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-plugin-datalabels@2.2.0/dist/chartjs-plugin-datalabels.min.js"></script>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'PingFang SC','Microsoft YaHei',sans-serif;background:#0f1117;color:#e2e8f0;font-size:14px}
h1{font-size:22px;font-weight:700;padding:24px 24px 8px;color:#f0f4ff}
.subtitle{padding:0 24px 20px;color:#8892a4;font-size:13px}
.tabs{display:flex;gap:4px;padding:0 24px 16px}
.tab{padding:7px 18px;border-radius:6px;cursor:pointer;background:#1e2230;color:#8892a4;border:1px solid #2d3348;transition:.15s}
.tab.active{background:#3b55e6;color:#fff;border-color:#3b55e6}
.section{display:none;padding:0 24px 32px}
.section.active{display:block}
/* cards */
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:12px;margin-bottom:24px}
.card{background:#1a1e2e;border:1px solid #2d3348;border-radius:10px;padding:16px}
.card .label{font-size:12px;color:#8892a4;margin-bottom:6px}
.card .value{font-size:24px;font-weight:700}
.card .value.pos{color:#34d399}
.card .value.neg{color:#f87171}
.card .value.neu{color:#93c5fd}
/* table */
.tbl-wrap{overflow-x:auto;border-radius:10px;border:1px solid #2d3348;margin-bottom:28px}
table{width:100%;border-collapse:collapse}
thead th{background:#1a1e2e;padding:10px 14px;text-align:right;font-size:12px;color:#8892a4;white-space:nowrap;border-bottom:1px solid #2d3348}
thead th:first-child{text-align:left}
tbody tr:hover{background:#1e2332}
tbody td{padding:9px 14px;border-bottom:1px solid #1e2230;text-align:right;font-size:13px}
tbody td:first-child{text-align:left;font-weight:600}
.pos{color:#34d399}.neg{color:#f87171}.neu{color:#93c5fd}
.alpha-pos{color:#34d399;font-weight:700}
.alpha-neg{color:#f87171;font-weight:700}
/* charts */
.charts-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(480px,1fr));gap:20px;margin-bottom:28px}
.chart-card{background:#1a1e2e;border:1px solid #2d3348;border-radius:10px;padding:16px}
.chart-card h3{font-size:14px;font-weight:600;margin-bottom:12px;color:#c9d1e0}
.chart-card canvas{width:100%!important}
/* period badge */
.period-info{display:inline-block;background:#1e2332;border:1px solid #2d3348;border-radius:6px;padding:4px 12px;font-size:12px;color:#8892a4;margin-bottom:20px}
</style>
</head>
<body>
<h1>策略回测报告</h1>
<p class="subtitle">__SUBTITLE__</p>

<div class="tabs">
__TABS__
</div>

__SECTIONS__

<script>
const DATA = __DATA_JSON__;

// ── 渲染工具 ────────────────────────────────────────────────
function fmt(v, digits=1){
  if(v===undefined||v===null||v==='—'||v==='')return'—';
  const n=parseFloat(v);
  if(isNaN(n))return v;
  const s=n.toFixed(digits);
  return n>0?'+'+s:s;
}
function cls(v){
  const n=parseFloat(v);
  if(isNaN(n))return'';
  return n>0?'pos':n<0?'neg':'neu';
}
function alphaCls(v){
  const n=parseFloat(v);
  if(isNaN(n))return'';
  return n>0?'alpha-pos':'alpha-neg';
}

// ── 摘要卡片 ────────────────────────────────────────────────
function renderCards(period){
  const d=DATA[period];
  const rows=[...(d.rows||[]),...(d.rows_serenity||[])].filter(r=>r['strategy%']!==undefined);
  const avgAlpha=(rows.reduce((s,r)=>s+(r['alpha%']||0),0)/rows.length).toFixed(1);
  const beat=rows.filter(r=>(r['alpha%']||0)>0).length;
  const profitable=rows.filter(r=>(r['strategy%']||0)>0).length;
  const avgT=(rows.reduce((s,r)=>s+(r['t_pnl%']||0),0)/rows.length).toFixed(1);
  const tPos=rows.filter(r=>(r['t_pnl%']||0)>0).length;
  const since=d.since, until=d.until;

  return `
  <div class="cards">
    <div class="card"><div class="label">时间区间</div><div class="value neu" style="font-size:16px">${since} ~ ${until}</div></div>
    <div class="card"><div class="label">策略盈利标的</div><div class="value ${profitable>rows.length*0.6?'pos':'neg'}">${profitable} / ${rows.length}</div></div>
    <div class="card"><div class="label">平均超额α</div><div class="value ${cls(avgAlpha)}">${fmt(avgAlpha)}%</div></div>
    <div class="card"><div class="label">跑赢持有标的数</div><div class="value ${beat>rows.length/2?'pos':'neg'}">${beat} / ${rows.length}</div></div>
    <div class="card"><div class="label">T仓平均贡献</div><div class="value ${cls(avgT)}">${fmt(avgT)}%</div></div>
    <div class="card"><div class="label">T仓正贡献标的</div><div class="value ${tPos>=rows.length/2?'pos':'neg'}">${tPos} / ${rows.length}</div></div>
  </div>`;
}

// ── 对比主表 ────────────────────────────────────────────────
function buildTable(rows){
  let profitable=0,beat=0,valid=0;
  let html=`
  <div class="tbl-wrap"><table>
  <thead><tr>
    <th>标的</th><th>赛道</th>
    <th title="同仓位被动持有(小波80%/大波70%)">同仓持有%</th><th>策略%</th>
    <th title="策略% - 同仓持有%">超额α%</th><th>T贡献%</th>
    <th title="持有期间最大回撤">持有回撤%</th><th title="策略净值最大回撤">策略回撤%</th>
    <th>核心操作</th><th>T入场</th><th>T胜率</th><th>盈利</th><th>波动</th>
  </tr></thead><tbody>`;
  for(const r of rows){
    const err=r.err||r['错误'];
    if(err){html+=`<tr><td>${r.name||r.symbol}</td><td colspan="12" style="color:#8892a4">${err}</td></tr>`;continue;}
    const h=r['hold%'],s=r['strategy%'],a=r['alpha%'],tp=r['t_pnl%'];
    const hm=r['hold_mdd%'],sm=r['strat_mdd%'];
    const pnlIcon=s>0?'✅':'❌';
    const volLabel=r.type==='large_vol'?'<span style="color:#f87171">大</span>':'<span style="color:#60a5fa">小</span>';
    // 策略回撤比持有回撤小 = 风控有效（绿），反之红
    const mddCmp=sm!==undefined&&hm!==undefined?(sm>hm?'pos':'neg'):'';
    html+=`<tr>
      <td>${r.name||r.symbol} <span style="color:#6b7280;font-size:11px">${r.name&&r.name!==r.symbol?'('+r.symbol+')':''}</span></td>
      <td style="color:#8892a4;font-size:12px">${r.sector||''}</td>
      <td class="${cls(h)}">${fmt(h)}%</td>
      <td class="${cls(s)}">${fmt(s)}%</td>
      <td class="${alphaCls(a)}">${fmt(a)}%</td>
      <td class="${cls(tp)}">${fmt(tp)}%</td>
      <td class="neg">${hm!==undefined?fmt(hm)+'%':'—'}</td>
      <td class="${mddCmp}">${sm!==undefined?fmt(sm)+'%':'—'}</td>
      <td>${r.core_turns??'—'}</td>
      <td>${r.t_entries??'—'}</td>
      <td class="${cls((r.t_winrate||0)-50)}">${r.t_winrate!==undefined?r.t_winrate+'%':'—'}</td>
      <td style="text-align:center">${pnlIcon}</td>
      <td style="text-align:center">${volLabel}</td>
    </tr>`;
    valid++;
    if(s>0) profitable++;
    if(a>0) beat++;
  }
  html+=`</tbody></table></div>`;
  html+=`<p style="color:#8892a4;font-size:12px;padding:6px 4px">策略盈利 ${profitable}/${valid} · 跑赢持有 ${beat}/${valid}</p>`;
  return html;
}
function renderTable(period){
  const d=DATA[period];
  // 科技主线：去掉 Serenity 里已有的标的（以 Serenity 为准）
  const serSymbols=new Set((d.rows_serenity||[]).map(r=>r.symbol));
  const mainRows=(d.rows||[]).filter(r=>!serSymbols.has(r.symbol));
  let html=`<h2 style="color:#c9d1e0;font-size:15px;padding:16px 4px 8px">科技主线</h2>`;
  html+=buildTable(mainRows);
  if((d.rows_serenity||[]).length){
    html+=`<h2 style="color:#c9d1e0;font-size:15px;padding:24px 4px 8px">Serenity 主题池</h2>`;
    html+=buildTable(d.rows_serenity);
  }
  return html;
}

// ── 净值曲线图 ───────────────────────────────────────────────
let chartInstances={};
function destroyCharts(period){
  Object.keys(chartInstances).forEach(k=>{
    if(k.startsWith(period+'_')){chartInstances[k].destroy();delete chartInstances[k];}
  });
}
function renderCharts(period){
  const d=DATA[period];
  // 合并去重：Serenity 为准，科技主线去掉 Serenity 已有的
  const serSymbols=new Set((d.rows_serenity||[]).map(r=>r.symbol));
  const mainUniq=(d.rows||[]).filter(r=>!serSymbols.has(r.symbol));
  const allRows=[...mainUniq,...(d.rows_serenity||[])].filter(r=>r.eq_curve&&r.hold_curve);
  const container=document.getElementById('charts_'+period);
  if(!container)return;
  container.innerHTML='<div class="charts-grid">'+
    allRows.map(r=>{
      const label=r.name&&r.name!==r.symbol?`${r.name} (${r.symbol})`:`${r.symbol}`;
      return `<div class="chart-card"><h3>${label} — 净值对比（策略 vs 持有）</h3><canvas id="c_${period}_${r.symbol}"></canvas></div>`;
    }).join('')+
    '</div>';

  allRows.forEach(r=>{
    const id=`c_${period}_${r.symbol}`;
    const ctx=document.getElementById(id);
    if(!ctx)return;
    // 降采样：最多显示 500 个点
    const step=Math.max(1,Math.floor(r.times.length/500));
    const times=r.times.filter((_,i)=>i%step===0);
    const eq=r.eq_curve.filter((_,i)=>i%step===0);
    const hold=r.hold_curve.filter((_,i)=>i%step===0);

    chartInstances[`${period}_${r.symbol}`]=new Chart(ctx,{
      type:'line',
      data:{
        labels:times,
        datasets:[
          {label:'策略净值',data:eq,borderColor:'#3b82f6',borderWidth:1.5,pointRadius:0,tension:0.1,fill:false},
          {label:'全仓持有',data:hold,borderColor:'#f59e0b',borderWidth:1.5,pointRadius:0,tension:0.1,fill:false,borderDash:[4,3]}
        ]
      },
      options:{
        responsive:true,
        interaction:{mode:'index',intersect:false},
        plugins:{
          legend:{labels:{color:'#c9d1e0',font:{size:11}}},
          tooltip:{backgroundColor:'#1e2332',titleColor:'#c9d1e0',bodyColor:'#94a3b8'},
          datalabels:{display:false}
        },
        scales:{
          x:{display:true,ticks:{maxTicksLimit:6,color:'#6b7280',font:{size:10}},grid:{color:'#1e2332'}},
          y:{ticks:{color:'#6b7280',font:{size:10},callback:v=>v.toFixed(0)},grid:{color:'#1e2332'}}
        }
      }
    });
  });
}

// ── 跨期α对比图 ─────────────────────────────────────────────
function renderAlphaChart(){
  Chart.register(ChartDataLabels);
  const labels=DATA['全期'].rows.map(r=>r.name&&r.name!==r.symbol?r.name:r.symbol);
  const periods=['全期','2024H2','2025+'];
  const colors=['#3b82f6','#f59e0b','#34d399'];
  const datasets=periods.map((p,i)=>({
    label:p,
    data:DATA[p].rows.map(r=>r['alpha%']??null),
    backgroundColor:colors[i]+'99',
    borderColor:colors[i],
    borderWidth:1
  }));

  // 动态 Y 轴：取所有α值的实际范围，向外取整到最近整百
  const allAlphas=periods.flatMap(p=>DATA[p].rows.map(r=>r['alpha%']??null)).filter(v=>v!==null);
  const rawMin=Math.min(...allAlphas), rawMax=Math.max(...allAlphas);
  const yMin=Math.floor(rawMin/100)*100;
  const yMax=Math.ceil(rawMax/100)*100;

  const ctx=document.getElementById('alphaChart');
  if(!ctx)return;
  new Chart(ctx,{
    type:'bar',
    data:{labels:labels,datasets},
    options:{
      responsive:true,
      plugins:{
        legend:{labels:{color:'#c9d1e0',font:{size:11}}},
        tooltip:{
          backgroundColor:'#1e2332',titleColor:'#c9d1e0',bodyColor:'#94a3b8',
          callbacks:{
            label:ctx=>{
              const v=ctx.raw;
              return v!==null?` ${ctx.dataset.label}: ${v>0?'+':''}${v.toFixed(1)}%`:` ${ctx.dataset.label}: —`;
            }
          }
        },
        title:{display:true,text:'各标的超额α% (策略收益 − 同仓持有收益)',color:'#c9d1e0',font:{size:13}},
        datalabels:{
          anchor:'end',
          align:context=>{
            const v=context.dataset.data[context.dataIndex];
            return (v??0)>=0?'top':'bottom';
          },
          formatter:(v)=>v!==null?(v>0?'+':'')+v.toFixed(0)+'%':'',
          color:context=>{
            const v=context.dataset.data[context.dataIndex];
            return (v??0)>=0?'#34d399':'#f87171';
          },
          font:{size:9,weight:'bold'},
          clamp:false
        }
      },
      maintainAspectRatio:false,
      scales:{
        x:{ticks:{color:'#8892a4',font:{size:11}},grid:{color:'#1e2332'}},
        y:{
          min:yMin, max:yMax,
          ticks:{
            stepSize:50,
            autoSkip:false,
            maxTicksLimit:100,
            color:'#8892a4',
            font:{size:10},
            callback:v=>v+'%'
          },
          grid:{
            color:ctx=>ctx.tick.value===0?'#4a5568':'#1e2332',
            lineWidth:ctx=>ctx.tick.value===0?2:1
          }
        }
      }
    }
  });
}

// ── Tab 切换 ─────────────────────────────────────────────────
function switchTab(period){
  document.querySelectorAll('.tab').forEach(el=>{
    el.classList.toggle('active', el.dataset.period===period);
  });
  document.querySelectorAll('.section').forEach(el=>{
    const active=el.id==='sec_'+period;
    el.classList.toggle('active', active);
    if(active && !el.dataset.rendered){
      destroyCharts(period);
      renderCharts(period);
      el.dataset.rendered='1';
    }
  });
}

// ── 初始化 ───────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded',()=>{
  // 渲染卡片和表格（静态内容）
  ['全期','2024H2','2025+'].forEach(p=>{
    const sec=document.getElementById('sec_'+p);
    if(!sec)return;
    sec.insertAdjacentHTML('afterbegin',
      renderCards(p)+renderTable(p)
    );
  });
  // 跨期α图
  renderAlphaChart();
  // 默认显示全期
  switchTab('全期');
});
</script>
</body>
</html>
"""


def build_html(data: dict) -> str:
    periods = [p for p, _, _ in PERIODS]

    # tabs
    tabs_html = "\n".join(
        f'<div class="tab{"  active" if i==0 else ""}" data-period="{p}" onclick="switchTab(\'{p}\')">{p}</div>'
        for i, p in enumerate(periods)
    )

    # sections
    sections = []
    for i, p in enumerate(periods):
        active = " active" if i == 0 else ""
        alpha_chart = (
            '<div class="chart-card" style="margin-bottom:24px">'
            '<div style="position:relative;height:560px">'
            '<canvas id="alphaChart"></canvas>'
            '</div></div>'
            if p == "全期" else ""
        )
        sections.append(
            f'<div class="section{active}" id="sec_{p}">'
            f'{alpha_chart}'
            f'<div id="charts_{p}"></div>'
            f'</div>'
        )

    sections_html = "\n".join(sections)
    data_json = json.dumps(data, ensure_ascii=False, default=str)

    gen_time = datetime.now().strftime("%Y-%m-%d %H:%M")
    subtitle = (
        f"日线过滤 MA5&gt;10&gt;20&gt;30 · 2H双根信号建仓（不主动减仓）· "
        f"T仓 VWAP/MACD入场 + 2H连续2根跌破MA5出场 · "
        f"小波核心80% / 大波70% · T仓20% · "
        f"生成时间: {gen_time}"
    )

    return (HTML_TEMPLATE
            .replace("__TABS__", tabs_html)
            .replace("__SECTIONS__", sections_html)
            .replace("__DATA_JSON__", data_json)
            .replace("__SUBTITLE__", subtitle))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="report.html")
    args = parser.parse_args()

    print("运行回测，收集数据...")
    data = collect_data()

    out_path = OUT_DIR / args.out
    html = build_html(data)
    out_path.write_text(html, encoding="utf-8")
    print(f"\n✓ 报告已生成：{out_path}")
    print(f"  用浏览器打开：open {out_path}")


if __name__ == "__main__":
    main()

import html
from pathlib import Path

from tradebot.metrics import BacktestMetrics
from tradebot.research import ResearchSummary


def render_html_report(summary: ResearchSummary, aggregate_metrics: BacktestMetrics) -> str:
    rows_html = "\n".join(
        f"<tr><td>{html.escape(row.symbol)}</td><td>{html.escape(row.theme)}</td>"
        f"<td>{row.metrics.total_return_pct:.2%}</td><td>{row.metrics.max_drawdown_pct:.2%}</td>"
        f"<td>{row.metrics.win_rate:.2%}</td><td>{row.metrics.profit_factor:.2f}</td></tr>"
        for row in summary.rows
    )
    sensitivity_html = "\n".join(
        f"<tr><td>{row.parameter_value:.3f}</td><td>{row.metrics.total_return_pct:.2%}</td>"
        f"<td>{row.metrics.max_drawdown_pct:.2%}</td></tr>"
        for row in summary.sensitivity
    )
    stress_html = "\n".join(
        f"<tr><td>{html.escape(row.name)}</td><td>{row.metrics.total_return_pct:.2%}</td>"
        f"<td>{row.metrics.max_drawdown_pct:.2%}</td><td>{len(row.result.trades)}</td></tr>"
        for row in summary.stress
    )
    logs_html = "\n".join(html.escape(line) for line in summary.terminal_logs)
    curve_points = ",".join(str(idx) for idx, _ in enumerate(summary.rows))

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>{html.escape(summary.title)}</title>
  <style>
    body {{ margin: 0; background: #0f1115; color: #e8e8e8; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
    .layout {{ display: grid; grid-template-columns: 280px 1fr; min-height: 100vh; }}
    aside {{ background: #171b22; border-right: 1px solid #2a303b; padding: 20px; overflow: auto; }}
    main {{ padding: 24px; }}
    details {{ border: 1px solid #303846; border-radius: 8px; padding: 10px; margin: 10px 0; background: #11151c; }}
    summary {{ cursor: pointer; color: #8bd3ff; font-weight: 700; }}
    .cards {{ display: grid; grid-template-columns: repeat(4, minmax(140px, 1fr)); gap: 12px; }}
    .card {{ background: #171b22; border: 1px solid #2a303b; border-radius: 8px; padding: 14px; }}
    .value {{ font-size: 24px; color: #91f2b3; }}
    table {{ width: 100%; border-collapse: collapse; margin: 16px 0; background: #151922; }}
    th, td {{ border-bottom: 1px solid #2a303b; padding: 10px; text-align: left; }}
    th {{ color: #8bd3ff; }}
    pre {{ background: #05070a; border: 1px solid #28303a; border-radius: 8px; padding: 14px; overflow: auto; color: #9df59d; }}
    .chart {{ height: 180px; background: linear-gradient(180deg, #182333, #10141b); border: 1px solid #2a303b; border-radius: 8px; padding: 12px; }}
  </style>
</head>
<body>
  <div class="layout">
    <aside>
      <h2>金融概念逐字说明</h2>
      <details open><summary>Sharpe / 夏普比率</summary><p>衡量每承担一单位波动，策略获得多少超额收益。越高越好，但短样本可能失真。</p></details>
      <details><summary>Sortino / 索提诺比率</summary><p>只关注下跌波动的风险收益指标，比夏普更重视亏损波动。</p></details>
      <details><summary>Max Drawdown / 最大回撤</summary><p>从资金曲线高点到后续低点的最大跌幅，用来衡量最痛的一段亏损。</p></details>
      <details><summary>VWAP / 成交量加权均价</summary><p>用成交量加权后的日内平均成交价，常用来判断当前价格是否偏离日内公平价。</p></details>
      <details><summary>ATR / 平均真实波幅</summary><p>衡量标的最近波动幅度。ATR 高说明适合做 T 的空间更大，但风险也更高。</p></details>
      <details><summary>bps / 基点</summary><p>1 bps = 0.01%。30 bps 表示 0.30%。常用来描述滑点、点差和手续费。</p></details>
      <details><summary>Profit Factor / 盈亏比</summary><p>总盈利金额除以总亏损金额。大于 1 表示盈利交易覆盖了亏损交易。</p></details>
    </aside>
    <main>
      <h1>{html.escape(summary.title)}</h1>
      <section class="cards">
        <div class="card"><div>总收益</div><div class="value">{aggregate_metrics.total_return_pct:.2%}</div></div>
        <div class="card"><div>最大回撤</div><div class="value">{aggregate_metrics.max_drawdown_pct:.2%}</div></div>
        <div class="card"><div>胜率</div><div class="value">{aggregate_metrics.win_rate:.2%}</div></div>
        <div class="card"><div>夏普比率</div><div class="value">{aggregate_metrics.sharpe:.2f}</div></div>
      </section>
      <h2>样本外测试</h2>
      <p>当前报告框架支持样本内/样本外切分；接入真实 CSV 后可用固定参数分别跑两段数据。</p>
      <h2>多标的横向测试</h2>
      <table><thead><tr><th>标的</th><th>板块</th><th>收益</th><th>最大回撤</th><th>胜率</th><th>盈亏比</th></tr></thead><tbody>{rows_html}</tbody></table>
      <h2>参数高原测试</h2>
      <table><thead><tr><th>VWAP回踩阈值</th><th>收益</th><th>最大回撤</th></tr></thead><tbody>{sensitivity_html}</tbody></table>
      <h2>压力测试</h2>
      <table><thead><tr><th>场景</th><th>收益</th><th>最大回撤</th><th>交易数</th></tr></thead><tbody>{stress_html}</tbody></table>
      <h2>策略曲线</h2>
      <div class="chart">资金曲线占位点：{curve_points}</div>
      <h2>Terminal 风格执行 Log</h2>
      <pre>{logs_html}</pre>
    </main>
  </div>
</body>
</html>"""


def write_html_report(path: Path, summary: ResearchSummary, aggregate_metrics: BacktestMetrics) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_html_report(summary, aggregate_metrics), encoding="utf-8")


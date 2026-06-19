/* ============================================================
   Formatters
   ============================================================ */
const fmtPct = (value) => `${(value * 100).toFixed(2)}%`;
const fmtMoney = (value) => Number.isFinite(value) && value !== 0 ? `$${value.toFixed(2)}` : "-";
const fmtMoneyPlain = (value) => Number.isFinite(value) ? value.toFixed(2) : "-";
const fmtTime = (ts) => {
  if (!ts) return "-";
  const d = new Date(ts < 1e12 ? ts * 1000 : ts);
  return d.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
};
const fmtDuration = (ms) => {
  if (!ms || ms < 0) return "-";
  const m = Math.round(ms / 60000);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  const rm = m % 60;
  return rm > 0 ? `${h}h ${rm}m` : `${h}h`;
};

/* ============================================================
   Constants
   ============================================================ */
const SYMBOL_COLORS = ["#86b9ff", "#45d39a", "#fbbf24", "#c084fc", "#fb7185", "#34d399"];
const AUTH_TOKEN_KEY = "tradeApiToken";

/* ============================================================
   State
   ============================================================ */
let latestAllocation = null;
let backtestChart = null;   // lazy-init on IDE page show
let liveChart = null;       // lazy-init on live page show
let equityChartRef = null;  // { chart, seriesMap: { [symbol]: lineSeries } }
let weekIntradayChartRef = null;
let activeSymbol = "SPCX";
let activeResolution = "1h";
let weekResolution = "15m";

// Review page state
let reviewAllTrades = [];     // all flat trades from latest backtest (all assets combined)
let reviewAssetDetails = [];  // per-asset detail from latest backtest
let reviewWeeks = [];         // [{label, dateRange, startSec, endSec}]
let reviewWeekIndex = 0;      // currently shown week (0 = oldest)
let reviewSymbols = [];       // symbols from latest backtest run
let reviewChartSymbol = "";   // which symbol is shown in the intraday chart
let reviewSource = "Synthetic";

/* ============================================================
   Auth
   ============================================================ */
function getAuthToken() {
  return window.localStorage.getItem(AUTH_TOKEN_KEY) || "";
}

function setAuthToken(token) {
  if (token) {
    window.localStorage.setItem(AUTH_TOKEN_KEY, token);
  } else {
    window.localStorage.removeItem(AUTH_TOKEN_KEY);
  }
}

function showAuthScreen(message = "") {
  const screen = document.getElementById("authScreen");
  const msg = document.getElementById("authMessage");
  if (screen) screen.classList.remove("hidden");
  if (msg) {
    msg.textContent = message;
    msg.className = message ? "form-message negative" : "form-message";
  }
}

function hideAuthScreen() {
  document.getElementById("authScreen")?.classList.add("hidden");
}

async function apiFetch(url, options = {}) {
  const headers = new Headers(options.headers || {});
  const token = getAuthToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);
  const response = await fetch(url, { ...options, headers });
  if (response.status === 401 || response.status === 503) {
    setAuthToken("");
    showAuthScreen(response.status === 503 ? "请先在 .env 配置管理员账号、密码和 API Token。" : "登录已失效，请重新登录。");
  }
  return response;
}

async function submitAuthLogin(event) {
  event.preventDefault();
  const message = document.getElementById("authMessage");
  const username = document.getElementById("authUsername").value.trim();
  const password = document.getElementById("authPassword").value;
  message.textContent = "";
  message.className = "form-message";
  const response = await fetch("/api/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password }),
  });
  const body = await response.json();
  if (!response.ok) {
    message.textContent = body.error || "登录失败。";
    message.className = "form-message negative";
    return;
  }
  setAuthToken(body.token);
  hideAuthScreen();
  await refreshAllData();
}

/* ============================================================
   Navigation
   ============================================================ */
function switchPage(targetId) {
  document.querySelectorAll(".qd-page").forEach((page) => {
    page.classList.toggle("active", page.id === targetId);
  });
  document.querySelectorAll("[data-page-target]").forEach((button) => {
    button.classList.toggle("active", button.dataset.pageTarget === targetId);
  });
  // Lazy-initialize charts when their page first becomes visible
  if (targetId === "indicatorIdePage" && !backtestChart) {
    backtestChart = initTradingChart("backtestChart", 300);
    loadKlines(activeSymbol, activeResolution);
  }
  if (targetId === "strategyLivePage" && !liveChart) {
    liveChart = initTradingChart("liveChart", 360);
    loadKlines(activeSymbol, activeResolution);
  }
}

function toggleSidebar() {
  document.querySelector(".qd-app-shell").classList.toggle("sidebar-collapsed");
}


function clsFor(value) {
  if (value < 0) return "negative";
  if (value > 0) return "positive";
  return "";
}

/* glossary removed */

/* ============================================================
   IDE page – backtest summary / allocation
   ============================================================ */
function renderSummary(summary) {
  const root = document.getElementById("backtestSummary");
  const metrics = [
    ["总收益", fmtPct(summary.totalReturnPct)],
    ["最大回撤", fmtPct(summary.maxDrawdownPct)],
    ["胜率", fmtPct(summary.winRate)],
    ["盈亏比", summary.profitFactor.toFixed(2)],
    ["全局T仓上限", fmtMoney(summary.globalTExposureCap)],
  ];
  root.innerHTML = metrics.map(([label, value]) => `
    <div class="metric"><span>${label}</span><strong>${value}</strong></div>
  `).join("");
  renderStrategyPerformance(summary);
}

function renderAssets(assets) {
  document.getElementById("assetRows").innerHTML = assets.map((row) => `
    <tr>
      <td>${row.symbol}</td>
      <td>${row.theme}</td>
      <td class="${clsFor(row.returnPct)}">${fmtPct(row.returnPct)}</td>
      <td class="${clsFor(row.maxDrawdownPct)}">${fmtPct(row.maxDrawdownPct)}</td>
      <td>${fmtPct(row.winRate)}</td>
      <td>${row.trades}</td>
    </tr>
  `).join("");
}

function renderSensitivity(rows) {
  const max = Math.max(...rows.map((row) => Math.abs(row.returnPct)), 0.01);
  document.getElementById("sensitivityBars").innerHTML = rows.map((row) => `
    <div class="bar-row">
      <span>${(row.parameter * 100).toFixed(1)}%</span>
      <div class="bar-track"><div class="bar-fill" style="width:${Math.max(6, Math.abs(row.returnPct) / max * 100)}%"></div></div>
      <strong class="${clsFor(row.returnPct)}">${fmtPct(row.returnPct)}</strong>
    </div>
  `).join("");
}

function renderStress(rows) {
  document.getElementById("stressList").innerHTML = rows.map((row) => `
    <div class="stress-item">
      <strong>${row.name}</strong>
      <div>收益 <span class="${clsFor(row.returnPct)}">${fmtPct(row.returnPct)}</span> /
           最大回撤 <span class="${clsFor(row.maxDrawdownPct)}">${fmtPct(row.maxDrawdownPct)}</span> /
           交易 ${row.trades}</div>
    </div>
  `).join("");
}

function renderAllocationForm(allocation) {
  latestAllocation = allocation;
  document.getElementById("totalAccountQuote").value = allocation.totalAccountQuote;
  document.getElementById("allocationRows").innerHTML = allocation.symbols.map((row) => `
    <div class="allocation-row" data-symbol="${row.symbol}">
      <strong>${row.symbol}</strong>
      <label>
        <span>总仓位%</span>
        <input name="totalPct" type="number" min="0" max="100" step="1" value="${(row.totalPct * 100).toFixed(0)}">
      </label>
      <label>
        <span>T仓%</span>
        <input name="tPct" type="number" min="0" max="100" step="1" value="${(row.tPct * 100).toFixed(0)}">
      </label>
    </div>
  `).join("");
}

function validateAllocationForm() {
  const message = document.getElementById("allocationMessage");
  const rows = Array.from(document.querySelectorAll(".allocation-row"));
  const inputs = Array.from(document.querySelectorAll("#allocationForm input"));
  inputs.forEach((input) => input.classList.remove("error"));
  message.textContent = "";
  message.className = "form-message";

  const totalAccount = document.getElementById("totalAccountQuote");
  const totalValue = Number(totalAccount.value);
  let valid = Number.isFinite(totalValue) && totalValue > 0;
  if (!valid) totalAccount.classList.add("error");

  const totalPctSum = rows.reduce((sum, row) => sum + Number(row.querySelector("[name='totalPct']").value), 0);
  if (totalPctSum > 100) {
    rows.forEach((row) => row.querySelector("[name='totalPct']").classList.add("error"));
    valid = false;
  }
  rows.forEach((row) => {
    const totalPctInput = row.querySelector("[name='totalPct']");
    const tPctInput = row.querySelector("[name='tPct']");
    const totalPct = Number(totalPctInput.value);
    const tPct = Number(tPctInput.value);
    if (!Number.isFinite(totalPct) || totalPct < 0 || totalPct > 100) { totalPctInput.classList.add("error"); valid = false; }
    if (!Number.isFinite(tPct) || tPct < 0 || tPct > 100 || tPct > totalPct) { tPctInput.classList.add("error"); valid = false; }
  });
  if (!valid) {
    message.textContent = "请检查仓位：总仓位合计不能超过100%，且每只股票T仓%不能大于该股票总仓位%。";
    message.className = "form-message negative";
  }
  return valid;
}

async function saveAllocation(event) {
  event.preventDefault();
  if (!validateAllocationForm()) return;
  const symbols = Array.from(document.querySelectorAll(".allocation-row")).map((row) => ({
    symbol: row.dataset.symbol,
    totalPct: Number(row.querySelector("[name='totalPct']").value) / 100,
    tPct: Number(row.querySelector("[name='tPct']").value) / 100,
  }));
  const payload = { totalAccountQuote: Number(document.getElementById("totalAccountQuote").value), symbols };
  const response = await apiFetch("/api/allocation", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
  });
  const body = await response.json();
  const message = document.getElementById("allocationMessage");
  if (!response.ok) {
    message.textContent = `保存失败：${body.error}`;
    message.className = "form-message negative";
    return;
  }
  latestAllocation = body.allocation;
  renderAllocationForm(body.allocation);
  message.textContent = "仓位比例已保存";
  message.className = "form-message positive";
  await Promise.all([loadStaticState(), refreshLiveState()]);
}

/* ============================================================
   Chart initialisation (trackpad-safe)
   ============================================================ */
function initTradingChart(containerId, height = 300) {
  const container = document.getElementById(containerId);
  if (!container) return null;
  container.innerHTML = "";
  if (!window.LightweightCharts) {
    container.innerHTML = '<div class="chart-empty-state">图表库未加载，请检查网络后刷新。</div>';
    return null;
  }
  const chart = LightweightCharts.createChart(container, {
    autoSize: true,
    layout: {
      background: { color: "transparent" },
      textColor: "#d7dde7",
    },
    grid: {
      vertLines: { color: "rgba(84, 99, 121, 0.2)" },
      horzLines: { color: "rgba(84, 99, 121, 0.2)" },
    },
    rightPriceScale: { borderColor: "rgba(84, 99, 121, 0.4)" },
    timeScale: {
      borderColor: "rgba(84, 99, 121, 0.4)",
      timeVisible: true,
      secondsVisible: false,
      fixLeftEdge: true,
      fixRightEdge: true,
    },
    crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
    handleScale: { mouseWheel: false, pinch: true },
    handleScroll: { mouseWheel: true, pressedMouseMove: true, horzTouchDrag: true, vertTouchDrag: false },
  });
  const candles = chart.addCandlestickSeries({
    upColor: "#45d39a", downColor: "#e56b67",
    borderVisible: false,
    wickUpColor: "#45d39a", wickDownColor: "#e56b67",
  });
  const vwap = chart.addLineSeries({
    color: "#86b9ff", lineWidth: 1.5, priceLineVisible: false,
  });
  return { chart, candles, vwap };
}

function initLineChart(containerId, height = 260, color = "#86b9ff") {
  const container = document.getElementById(containerId);
  if (!container) return null;
  container.innerHTML = "";
  if (!window.LightweightCharts) {
    container.innerHTML = '<div class="chart-empty-state">图表库未加载。</div>';
    return null;
  }
  const chart = LightweightCharts.createChart(container, {
    autoSize: true,
    layout: { background: { color: "transparent" }, textColor: "#d7dde7" },
    grid: {
      vertLines: { color: "rgba(84, 99, 121, 0.15)" },
      horzLines: { color: "rgba(84, 99, 121, 0.15)" },
    },
    rightPriceScale: { borderColor: "rgba(84, 99, 121, 0.4)" },
    timeScale: {
      borderColor: "rgba(84, 99, 121, 0.4)",
      timeVisible: true,
      secondsVisible: false,
      fixLeftEdge: true,
      fixRightEdge: true,
    },
    crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
    handleScale: { mouseWheel: false, pinch: true },
    handleScroll: { mouseWheel: true, pressedMouseMove: true, horzTouchDrag: true, vertTouchDrag: false },
  });
  const lineSeries = chart.addLineSeries({
    color,
    lineWidth: 2,
    priceLineVisible: false,
    lastValueVisible: true,
  });
  return { chart, lineSeries };
}

function renderChart(chartRef, data, resolution) {
  if (!chartRef || !data) return;
  const candles = data.candles.map(([time, open, high, low, close]) => ({ time, open, high, low, close }));
  chartRef.candles.setData(candles);
  chartRef.vwap.setData(data.vwap.map(([time, value]) => ({ time, value })));

  const ts = chartRef.chart.timeScale();
  ts.fitContent();

  // Default visible window = 22 bars (22 trading days for 1D, or equivalent intraday window)
  const n = candles.length;
  const TARGET_BARS = 22;
  if (n > TARGET_BARS) {
    ts.setVisibleLogicalRange({ from: n - TARGET_BARS - 1, to: n });
  }
}

// Resample trade-level equity curve to one point per trading day (last value of day)
function resampleToDaily(equityCurve) {
  const sorted = equityCurve.filter((p) => p.time > 0).sort((a, b) => a.time - b.time);
  const dayMap = new Map();
  for (const p of sorted) {
    const dayStart = Math.floor(p.time / 86400) * 86400; // normalize to UTC midnight
    dayMap.set(dayStart, { time: dayStart, value: p.value });
  }
  return [...dayMap.values()].sort((a, b) => a.time - b.time);
}

/* ============================================================
   Review page – equity curve (multi-symbol)
   ============================================================ */
function renderMultiEquityCurve(assetDetails, checkedSymbols) {
  const container = document.getElementById("equityChart");
  if (!container) return;
  container.innerHTML = "";

  // Destroy old chart and create fresh one
  if (equityChartRef && equityChartRef.chart) {
    try { equityChartRef.chart.remove(); } catch (_) {}
  }

  if (!window.LightweightCharts) {
    container.innerHTML = '<div class="chart-empty-state">图表库未加载。</div>';
    equityChartRef = null;
    return;
  }

  const chart = LightweightCharts.createChart(container, {
    autoSize: true,
    layout: { background: { color: "transparent" }, textColor: "#d7dde7" },
    grid: {
      vertLines: { color: "rgba(84, 99, 121, 0.15)" },
      horzLines: { color: "rgba(84, 99, 121, 0.15)" },
    },
    rightPriceScale: { borderColor: "rgba(84, 99, 121, 0.4)" },
    timeScale: {
      borderColor: "rgba(84, 99, 121, 0.4)",
      timeVisible: true,
      secondsVisible: false,
      fixLeftEdge: true,
      fixRightEdge: true,
    },
    crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
    handleScale: { mouseWheel: false, pinch: true },
    handleScroll: { mouseWheel: true, pressedMouseMove: true, horzTouchDrag: true, vertTouchDrag: false },
  });

  const seriesMap = {};
  const checkedSet = new Set(checkedSymbols);

  assetDetails.forEach((detail, i) => {
    const color = SYMBOL_COLORS[i % SYMBOL_COLORS.length];
    const series = chart.addLineSeries({
      color,
      lineWidth: 2,
      priceLineVisible: false,
      lastValueVisible: true,
      title: detail.symbol,
    });
    const pts = resampleToDaily(detail.equityCurve || []);
    if (pts.length > 0) series.setData(pts);
    series.applyOptions({ visible: checkedSet.has(detail.symbol) });
    seriesMap[detail.symbol] = series;
  });

  equityChartRef = { chart, seriesMap };
  chart.timeScale().fitContent();
  updateEquityLegend(assetDetails);
}

function updateEquitySeriesVisibility(checkedSymbols) {
  if (!equityChartRef || !equityChartRef.seriesMap) return;
  const checkedSet = new Set(checkedSymbols);
  for (const [symbol, series] of Object.entries(equityChartRef.seriesMap)) {
    series.applyOptions({ visible: checkedSet.has(symbol) });
  }
}

function updateEquityLegend(assetDetails) {
  const legend = document.getElementById("equityLegend");
  if (!legend) return;
  legend.innerHTML = assetDetails.map((d, i) => {
    const color = SYMBOL_COLORS[i % SYMBOL_COLORS.length];
    return `<span class="legend-sym"><span class="legend-color-dot" style="background:${color}"></span>${d.symbol}</span>`;
  }).join("");
}

/* ============================================================
   Review page – week computation
   ============================================================ */
function computeWeeks(trades) {
  if (!trades || trades.length === 0) return [];
  const timestamps = trades.map((t) => Math.floor(t.openTime / 1000)).filter((t) => t > 0);
  if (timestamps.length === 0) return [];

  const minTs = Math.min(...timestamps);
  const maxTs = Math.max(...timestamps);

  // find Monday of week containing minTs
  const startDate = new Date(minTs * 1000);
  const dayOfWeek = startDate.getDay(); // 0=Sun
  const daysToMon = dayOfWeek === 0 ? -6 : 1 - dayOfWeek;
  startDate.setDate(startDate.getDate() + daysToMon);
  startDate.setHours(0, 0, 0, 0);

  const weeks = [];
  let weekStart = startDate.getTime() / 1000;
  while (weekStart <= maxTs + 86400 * 7) {
    const weekEnd = weekStart + 86400 * 7 - 1;
    const startD = new Date(weekStart * 1000);
    const endD = new Date(Math.min(weekEnd, maxTs + 86400) * 1000);
    const fmt = (d) => `${d.getMonth() + 1}/${d.getDate()}`;
    weeks.push({
      label: `第 ${weeks.length + 1} 周`,
      dateRange: `${fmt(startD)} – ${fmt(endD)}`,
      startSec: weekStart,
      endSec: weekEnd,
    });
    weekStart += 86400 * 7;
    if (weeks.length > 200) break;
  }
  return weeks;
}

function updateWeekNav() {
  const prevBtn = document.getElementById("weekPrevBtn");
  const nextBtn = document.getElementById("weekNextBtn");
  const label = document.getElementById("weekLabel");
  const dateRange = document.getElementById("weekDateRange");
  const counter = document.getElementById("weekCounter");
  const n = reviewWeeks.length;
  if (n === 0) {
    prevBtn.disabled = true;
    nextBtn.disabled = true;
    label.textContent = "--";
    dateRange.textContent = "运行分析后启用";
    counter.textContent = "0 / 0 周";
    return;
  }
  prevBtn.disabled = reviewWeekIndex <= 0;
  nextBtn.disabled = reviewWeekIndex >= n - 1;
  const w = reviewWeeks[reviewWeekIndex];
  label.textContent = w.label;
  dateRange.textContent = w.dateRange;
  counter.textContent = `${reviewWeekIndex + 1} / ${n} 周`;
}

function renderWeekSymbolTabs() {
  const bar = document.getElementById("weekSymbolBar");
  if (!bar) return;
  if (reviewSymbols.length === 0) { bar.style.display = "none"; return; }
  bar.style.display = "";
  bar.innerHTML = reviewSymbols.map((sym) => `
    <button type="button" class="week-symbol-tab ${sym === reviewChartSymbol ? "active" : ""}"
            data-week-symbol="${sym}">${sym}</button>
  `).join("");
  bar.querySelectorAll("[data-week-symbol]").forEach((btn) => {
    btn.addEventListener("click", () => {
      reviewChartSymbol = btn.dataset.weekSymbol;
      renderWeekSymbolTabs();
      weekIntradayChartRef = null;
      loadWeekIntraday();
    });
  });
}

async function loadWeekIntraday() {
  if (reviewWeeks.length === 0 || !reviewChartSymbol) return;
  const w = reviewWeeks[reviewWeekIndex];
  const symbol = reviewChartSymbol;

  document.getElementById("weekChartTitle").textContent =
    `${symbol} · ${w.label} ${w.dateRange} · 做T买卖点`;

  const params = new URLSearchParams({
    symbol,
    resolution: weekResolution,
    source: reviewSource,
    startTime: Math.floor(w.startSec),
    endTime: Math.floor(w.endSec),
  });
  const resp = await apiFetch(`/api/klines?${params}`);
  if (!resp.ok) return;
  const data = await resp.json();

  const container = document.getElementById("weekIntradayChart");
  container.innerHTML = "";

  if (!weekIntradayChartRef) {
    weekIntradayChartRef = initTradingChart("weekIntradayChart", 280);
  }
  if (!weekIntradayChartRef) return;

  renderChart(weekIntradayChartRef, data);

  // Overlay buy/sell markers for this symbol's trades in this week
  const weekTrades = getWeekTrades().filter((t) => t.symbol === symbol);
  const markers = weekTrades
    .filter((t) => t.openTime > 0)
    .map((t) => ({
      time: Math.floor(t.openTime / 1000),
      position: t.side === "BUY" ? "belowBar" : "aboveBar",
      color: t.side === "BUY" ? "#45d39a" : "#e56b67",
      shape: t.side === "BUY" ? "arrowUp" : "arrowDown",
      text: t.side === "BUY" ? "买" : "卖",
      size: 1,
    }))
    .sort((a, b) => a.time - b.time);
  weekIntradayChartRef.candles.setMarkers(markers);
}

function getWeekTrades() {
  if (reviewWeeks.length === 0) return [];
  const w = reviewWeeks[reviewWeekIndex];
  return reviewAllTrades.filter((t) => {
    const ts = Math.floor(t.openTime / 1000);
    return ts >= w.startSec && ts <= w.endSec;
  });
}

/* ============================================================
   Review page – T-trade pairing & table
   ============================================================ */
function pairTTrades(trades) {
  // Simple greedy pairing: each BUY matched with the next SELL for same symbol
  const pairs = [];
  const openBuys = {};
  for (const t of [...trades].sort((a, b) => a.openTime - b.openTime)) {
    if (t.side === "BUY") {
      if (!openBuys[t.symbol]) openBuys[t.symbol] = [];
      openBuys[t.symbol].push(t);
    } else if (t.side === "SELL") {
      const buys = openBuys[t.symbol] || [];
      if (buys.length > 0) {
        const buy = buys.shift();
        const pnl = t.quote - buy.quote - t.fee - buy.fee;
        const pnlPct = pnl / buy.quote;
        const durationMs = t.openTime - buy.openTime;
        pairs.push({ symbol: t.symbol || buy.symbol, buy, sell: t, pnl, pnlPct, durationMs });
      } else {
        pairs.push({ symbol: t.symbol, buy: null, sell: t, pnl: null, pnlPct: null, durationMs: null });
      }
    }
  }
  // Remaining open buys (no matching sell yet)
  for (const [symbol, buys] of Object.entries(openBuys)) {
    for (const buy of buys) {
      pairs.push({ symbol, buy, sell: null, pnl: null, pnlPct: null, durationMs: null });
    }
  }
  return pairs.sort((a, b) => {
    const ta = a.buy ? a.buy.openTime : (a.sell ? a.sell.openTime : 0);
    const tb = b.buy ? b.buy.openTime : (b.sell ? b.sell.openTime : 0);
    return ta - tb;
  });
}

function renderTTrades(trades) {
  const tbody = document.getElementById("tTradeRows");
  if (!tbody) return;
  if (!trades || trades.length === 0) {
    tbody.innerHTML = '<tr><td colspan="10" class="empty-cell">本周无做T记录</td></tr>';
    document.getElementById("weekTradeSummary").textContent = "0 笔";
    document.getElementById("weekTradeTitle").textContent =
      reviewWeeks.length > 0 ? `${reviewWeeks[reviewWeekIndex].label} · 做T记录` : "本周做T记录";
    return;
  }
  const pairs = pairTTrades(trades);
  const totalPnl = pairs.reduce((sum, p) => sum + (p.pnl || 0), 0);
  const winCount = pairs.filter((p) => p.pnl != null && p.pnl > 0).length;
  const completePairs = pairs.filter((p) => p.sell !== null);

  document.getElementById("weekTradeSummary").textContent =
    `${completePairs.length} 笔已平 · 盈亏合计 ${totalPnl >= 0 ? "+" : ""}${totalPnl.toFixed(2)} USDT · 胜率 ${completePairs.length > 0 ? ((winCount / completePairs.length) * 100).toFixed(0) : "--"}%`;
  if (reviewWeeks.length > 0) {
    document.getElementById("weekTradeTitle").textContent = `${reviewWeeks[reviewWeekIndex].label} · 做T记录`;
  }

  tbody.innerHTML = pairs.map((p) => {
    const pnlCls = p.pnl == null ? "" : p.pnl >= 0 ? "positive" : "negative";
    return `
    <tr>
      <td>${p.symbol || "-"}</td>
      <td>${p.buy ? fmtTime(p.buy.openTime) : "持仓中"}</td>
      <td>${p.buy ? p.buy.price.toFixed(4) : "-"}</td>
      <td>${p.sell ? fmtTime(p.sell.openTime) : '<span class="muted-text">未平仓</span>'}</td>
      <td>${p.sell ? p.sell.price.toFixed(4) : "-"}</td>
      <td>${p.buy ? p.buy.qty.toFixed(4) : "-"}</td>
      <td class="${pnlCls}">${p.pnl != null ? (p.pnl >= 0 ? "+" : "") + p.pnl.toFixed(2) : "-"}</td>
      <td class="${pnlCls}">${p.pnlPct != null ? fmtPct(p.pnlPct) : "-"}</td>
      <td>${p.durationMs != null ? fmtDuration(p.durationMs) : "-"}</td>
      <td class="muted-text">${(p.sell || p.buy || {}).reason || "-"}</td>
    </tr>`;
  }).join("");
}

function renderWeekView() {
  const weekTrades = getWeekTrades();
  renderTTrades(weekTrades);
  updateWeekNav();
  loadWeekIntraday();
}

/* ============================================================
   Review page – run analysis
   ============================================================ */
function collectReviewControls() {
  const source = document.getElementById("reviewDataSource")?.value || "Synthetic";
  const slippage = Number(document.getElementById("reviewSlippage")?.value || 30);
  const spread = Number(document.getElementById("reviewSpread")?.value || 20);
  const symbols = Array.from(document.querySelectorAll("[name='reviewSymbol']:checked")).map((el) => el.value);
  return { source, slippage, spread, symbols };
}

async function runReviewAnalysis() {
  const btn = document.getElementById("runReviewButton");
  const status = document.getElementById("reviewStatus");
  btn.disabled = true;
  status.textContent = "运行中…";
  status.className = "review-status-text muted-text";

  const ctrl = collectReviewControls();
  reviewSource = ctrl.source;
  reviewSymbols = ctrl.symbols;

  try {
    const resp = await apiFetch("/api/backtest/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        symbols: ctrl.symbols,
        source: ctrl.source,
        slippageBps: ctrl.slippage,
        spreadBps: ctrl.spread,
      }),
    });
    const body = await resp.json();
    if (!resp.ok) {
      const hint = resp.status === 502 && isBinanceSource(ctrl.source)
        ? "Binance 不支持美股代码，请切换至 Synthetic 或 CSV"
        : body.error;
      status.textContent = `失败：${hint}`;
      status.className = "review-status-text negative";
      return;
    }

    const bt = body.backtest;
    renderReviewKpi(bt.summary);
    renderReviewAssets(bt.assets);

    // Merge all asset trades (with symbol annotation)
    reviewAssetDetails = bt.assetDetails || [];
    reviewAllTrades = [];
    for (const detail of reviewAssetDetails) {
      for (const t of (detail.trades || [])) {
        reviewAllTrades.push({ ...t, symbol: detail.symbol });
      }
    }
    if (reviewAllTrades.length === 0 && bt.primaryTrades) {
      reviewAllTrades = bt.primaryTrades.map((t) => ({ ...t, symbol: ctrl.symbols[0] || "?" }));
    }

    // Multi-symbol equity curves
    const checkedSymbols = ctrl.symbols;
    renderMultiEquityCurve(reviewAssetDetails, checkedSymbols);

    reviewWeeks = computeWeeks(reviewAllTrades);
    // Start at most recent week
    reviewWeekIndex = Math.max(0, reviewWeeks.length - 1);

    // Set default intraday symbol and rebuild symbol tabs
    reviewChartSymbol = reviewSymbols[0] || "";
    renderWeekSymbolTabs();

    // Reset intraday chart for new run
    weekIntradayChartRef = null;

    renderWeekView();

    status.textContent = `完成 · ${new Date().toLocaleTimeString("zh-CN")} · ${bt.resultId}`;
    status.className = "review-status-text positive";
    document.getElementById("updatedAt").textContent = new Date().toLocaleTimeString("zh-CN");
  } catch (err) {
    status.textContent = `错误：${err.message}`;
    status.className = "review-status-text negative";
    console.error(err);
  } finally {
    btn.disabled = false;
  }
}

function renderReviewKpi(summary) {
  const set = (id, val, cls) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.textContent = val;
    if (cls) el.className = cls;
  };
  set("kpiTotalReturn", fmtPct(summary.totalReturnPct || 0),
      (summary.totalReturnPct || 0) >= 0 ? "positive" : "negative");
  set("kpiMaxDD", fmtPct(summary.maxDrawdownPct || 0), "warning");
  set("kpiWinRate", fmtPct(summary.winRate || 0), "");
  set("kpiPF", Number.isFinite(summary.profitFactor) ? summary.profitFactor.toFixed(2) : "--", "");
  set("kpiCoreReturn", fmtPct(summary.coreOnlyReturnPct || 0), "");
  set("kpiAlpha", summary.strategyVsCoreOnlyAlpha != null
      ? (summary.strategyVsCoreOnlyAlpha >= 0 ? "+" : "") + fmtPct(summary.strategyVsCoreOnlyAlpha)
      : "--",
      (summary.strategyVsCoreOnlyAlpha || 0) >= 0 ? "positive" : "negative");
}

function renderReviewAssets(assets) {
  document.getElementById("reviewAssetRows").innerHTML = assets.map((row) => `
    <tr>
      <td><strong>${row.symbol}</strong></td>
      <td class="muted-text">${row.theme}</td>
      <td class="${clsFor(row.returnPct)}">${fmtPct(row.returnPct)}</td>
      <td class="${clsFor(row.maxDrawdownPct)}">${fmtPct(row.maxDrawdownPct)}</td>
      <td>${fmtPct(row.winRate)}</td>
      <td>${Number.isFinite(row.profitFactor) ? row.profitFactor.toFixed(2) : "--"}</td>
      <td>${row.trades}</td>
      <td class="muted-text">$${(row.feesPaid || 0).toFixed(2)}</td>
    </tr>
  `).join("") || '<tr><td colspan="8" class="empty-cell">暂无数据</td></tr>';
}

/* ============================================================
   IDE page – klines / backtest
   ============================================================ */
function collectDataSourceControls() {
  const source = document.getElementById("dataSourceSelect")?.value || "Synthetic";
  const dailyPath = document.getElementById("csvDailyPath")?.value.trim() || "";
  const intradayPath = document.getElementById("csvIntradayPath")?.value.trim() || "";
  const payload = { source };
  if (dailyPath) payload.dailyPath = dailyPath;
  if (intradayPath) payload.intradayPath = intradayPath;
  return payload;
}

async function loadKlines(symbol = activeSymbol, resolution = activeResolution) {
  activeSymbol = symbol;
  activeResolution = resolution;
  const dataSource = collectDataSourceControls();
  const params = new URLSearchParams({ symbol, resolution, source: dataSource.source });
  if (dataSource.dailyPath) params.set("dailyPath", dataSource.dailyPath);
  if (dataSource.intradayPath) params.set("intradayPath", dataSource.intradayPath);

  // Update symbol tab active state
  document.querySelectorAll("[data-chart-symbol]").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.chartSymbol === symbol);
  });
  // Update resolution tab active state
  document.querySelectorAll("[data-resolution]").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.resolution === resolution);
  });

  const response = await apiFetch(`/api/klines?${params.toString()}`);
  const data = await response.json();
  renderChart(backtestChart, data, resolution);
  renderChart(liveChart, data, resolution);
}

function collectBacktestControls() {
  return {
    ...collectDataSourceControls(),
    symbols: Array.from(document.querySelectorAll("[name='backtestSymbol']:checked")).map((input) => input.value),
    slippageBps: Number(document.getElementById("slippageBps").value),
    spreadBps: Number(document.getElementById("spreadBps").value),
  };
}

function isBinanceSource(source) {
  return (source || "").toLowerCase().includes("binance");
}

function looksLikeUsStock(symbol) {
  // Simple heuristic: no USDT/BTC/ETH/BNB suffix → US stock
  return !/USDT|USDC|BTC|ETH|BNB|BUSD/i.test(symbol);
}

function warnBinanceUsStock(source, symbols, warningElId) {
  const el = document.getElementById(warningElId);
  if (!el) return;
  if (isBinanceSource(source) && symbols.some(looksLikeUsStock)) {
    el.textContent = "⚠ Binance 不支持美股代码（SPCX/TSLA/NVDA 等），请切换至 Synthetic 或 CSV。";
    el.className = "form-message warning";
    el.style.display = "";
  } else {
    el.style.display = "none";
  }
}

async function runBacktestFromControls() {
  const payload = collectBacktestControls();
  warnBinanceUsStock(payload.source, payload.symbols, "dataSourceWarning");

  const statusEl = document.getElementById("connectionStatus");
  statusEl.textContent = "运行中…";

  const response = await apiFetch("/api/backtest/run", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
  });
  const body = await response.json();
  if (!response.ok) {
    const hint = response.status === 502 && isBinanceSource(payload.source)
      ? "Binance 不支持此标的，请切换 Synthetic 或 CSV 数据源"
      : body.error;
    statusEl.textContent = `回测失败：${hint}`;
    return;
  }
  statusEl.textContent = "已连接";
  renderSummary(body.backtest.summary);
  renderAssets(body.backtest.assets);
  const symbol = payload.symbols[0] || "SPCX";
  await loadKlines(symbol);
  document.getElementById("updatedAt").textContent = new Date().toLocaleTimeString("zh-CN");
  await loadPlatformData();
}

function switchFactorTab(panelName) {
  document.querySelectorAll("[data-factor-target]").forEach((b) => b.classList.toggle("active", b.dataset.factorTarget === panelName));
  document.querySelectorAll("[data-factor-panel]").forEach((p) => p.classList.toggle("active", p.dataset.factorPanel === panelName));
}

/* ============================================================
   Live state
   ============================================================ */
function renderDataSources(payload) {
  const root = document.getElementById("dataSourceCards");
  if (!root) return;
  root.innerHTML = (payload.sources || []).map((source) => `
    <article class="source-card">
      <strong>${source.id}</strong>
      <span>${source.label}</span>
      <em>${source.requiresNetwork ? "需要联网" : "本地可用"}</em>
    </article>
  `).join("");
}

function renderPaperOrders(payload) {
  const roots = [
    document.getElementById("paperOrderRows"),
    document.getElementById("paperOrderRowsFull"),
  ].filter(Boolean);
  if (!roots.length) return;
  const orders = payload.orders || [];
  const html = orders.length ? orders.map((order) => `
    <tr>
      <td>${order.symbol || "-"}</td>
      <td>${order.side || "-"}</td>
      <td>${order.sourceSignal || order.source_signal || "-"}</td>
      <td>${order.status || "pending"}</td>
      <td>${order.reason || "-"}</td>
    </tr>
  `).join("") : `<tr><td colspan="5">暂无 Paper 订单。</td></tr>`;
  roots.forEach((root) => { root.innerHTML = html; });
}

function renderQuickTradeAccount(payload) {
  const account = payload.account || {};
  const priceBox = document.querySelector(".qd-price-box");
  if (!priceBox) return;
  const positions = Object.keys(account.positions || {});
  priceBox.innerHTML = `
    <strong>${positions[0] || "NVDA"}</strong>
    <span>${fmtMoney(account.cash || 0)}</span>
  `;
}

function renderStrategyList(live) {
  const list = document.querySelector(".qd-strategy-list");
  if (!list || !live.positions) return;
  const header = list.querySelector(".section-head")?.outerHTML || "";
  const rows = live.positions.map((row, index) => `
    <div class="qd-strategy-group ${index === 0 ? "active" : ""}">
      <span>Trade T Bucket</span>
      <strong>${row.symbol}</strong>
      <em>${row.status} · ${row.layers} layers · spread ${fmtPct(row.spreadPct)}</em>
    </div>
  `).join("");
  list.innerHTML = header + rows;
}

function renderStrategyPerformance(summary = {}) {
  const root = document.getElementById("strategyPerformance");
  if (!root) return;
  const metrics = [
    ["总收益", fmtPct(summary.totalReturnPct || 0), "positive"],
    ["最大回撤", fmtPct(summary.maxDrawdownPct || 0), "warning"],
    ["胜率", fmtPct(summary.winRate || 0), ""],
    ["盈亏比", Number.isFinite(summary.profitFactor) ? summary.profitFactor.toFixed(2) : "-", ""],
  ];
  root.innerHTML = metrics.map(([label, value, cls]) => `
    <div class="metric"><span>${label}</span><strong class="${cls}">${value}</strong></div>
  `).join("");
}

function renderResultHistory(payload) {
  const root = document.getElementById("resultHistoryRows");
  if (!root) return;
  const results = payload.results || [];
  root.innerHTML = results.length ? results.map((result) => `
    <tr>
      <td>${result.resultId || result.result_id || "-"}</td>
      <td>${result.createdAt || "-"}</td>
      <td>${result.engineVersion || result.engine_version || "-"}</td>
      <td>${result.summary ? fmtPct(result.summary.totalReturnPct || 0) : "-"}</td>
    </tr>
  `).join("") : `<tr><td colspan="4">暂无保存的回测结果。</td></tr>`;
}

function renderRiskLights(live) {
  const safeSpread = live.positions.every((row) => row.spreadPct <= 0.005);
  const dangerRisk = live.positions.some((row) => row.risk === "危险");
  const activeT = live.positions.some((row) => row.layers > 0);
  const items = [
    ["点差过滤", safeSpread ? "SAFE" : "WIDE SPREAD", safeSpread ? "" : "danger"],
    ["插针保护", dangerRisk ? "DANGER" : "READY", dangerRisk ? "danger" : ""],
    ["全局T仓", activeT ? "ACTIVE" : "IDLE", activeT ? "warn" : ""],
    ["虚拟钱包", "ISOLATED", ""],
  ];
  document.getElementById("riskLights").innerHTML = items.map(([label, status, cls]) => `
    <div class="risk-light ${cls}"><strong>${label}</strong><span>${status}</span></div>
  `).join("");
}

function renderLive(live) {
  document.getElementById("tradeMode").textContent = live.mode;
  document.getElementById("liveRows").innerHTML = live.positions.map((row) => `
    <tr>
      <td>${row.symbol}</td>
      <td>${row.status}</td>
      <td>${fmtMoney(row.lastPrice)}</td>
      <td>${row.layers}</td>
      <td>${fmtMoney(row.symbolBudget)}</td>
      <td>${fmtMoney(row.tBudget)}</td>
      <td class="${row.spreadPct > 0.005 ? "negative" : ""}">${fmtPct(row.spreadPct)}</td>
      <td class="${row.risk === "正常" ? "positive" : "warning"}">${row.risk}</td>
    </tr>
  `).join("");
  document.getElementById("guardrails").innerHTML = live.guardrails.map((item) => `<li>${item}</li>`).join("");
  const term = document.getElementById("terminalLog");
  term.textContent = live.logs.join("\n");
  term.scrollTop = term.scrollHeight;
  renderRiskLights(live);
  renderStrategyList(live);
}

/* ============================================================
   Data loaders
   ============================================================ */
async function loadStaticState() {
  const response = await apiFetch("/api/state/static");
  const state = await response.json();
  renderSummary(state.backtest.summary);
  renderAssets(state.backtest.assets);
  renderSensitivity(state.backtest.sensitivity);
  renderStress(state.backtest.stress);
  renderAllocationForm(state.allocation);
  document.getElementById("connectionStatus").textContent = "已连接";
  document.getElementById("updatedAt").textContent = new Date().toLocaleTimeString("zh-CN");
}

async function refreshLiveState() {
  try {
    const response = await apiFetch("/api/state/live");
    const state = await response.json();
    renderLive(state.live);
    document.getElementById("connectionStatus").textContent = "已连接";
    document.getElementById("updatedAt").textContent = new Date().toLocaleTimeString("zh-CN");
  } catch (error) {
    document.getElementById("connectionStatus").textContent = "连接失败";
    console.error(error);
  }
}

async function loadPlatformData() {
  const [sourcesRes, paperRes, resultsRes] = await Promise.all([
    apiFetch("/api/data-sources"),
    apiFetch("/api/paper/orders"),
    apiFetch("/api/backtest/results"),
  ]);
  const paperPayload = await paperRes.json();
  renderDataSources(await sourcesRes.json());
  renderPaperOrders(paperPayload);
  renderQuickTradeAccount(paperPayload);
  renderResultHistory(await resultsRes.json());
}

async function refreshAllData() {
  document.getElementById("connectionStatus").textContent = "刷新中";
  const tasks = [loadStaticState(), refreshLiveState(), loadPlatformData()];
  if (backtestChart || liveChart) tasks.push(loadKlines(activeSymbol, activeResolution));
  await Promise.all(tasks);
}

async function submitPaperOrder(side) {
  const amountInput = document.getElementById("paperOrderAmount");
  const message = document.getElementById("paperOrderMessage");
  const quoteAmount = Number(amountInput.value);
  amountInput.classList.remove("error");
  message.textContent = "";
  message.className = "form-message";
  if (!Number.isFinite(quoteAmount) || quoteAmount <= 0) {
    amountInput.classList.add("error");
    message.textContent = "请输入大于 0 的模拟下单金额。";
    message.className = "form-message negative";
    return;
  }
  const response = await apiFetch("/api/paper/orders", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ symbol: activeSymbol, side, quoteAmount, orderType: "market" }),
  });
  const body = await response.json();
  if (!response.ok) {
    message.textContent = `模拟下单失败：${body.error}`;
    message.className = "form-message negative";
    return;
  }
  message.textContent = body.order.status === "filled" ? "模拟订单已成交" : `模拟订单被拒绝：${body.order.reason}`;
  message.className = body.order.status === "filled" ? "form-message positive" : "form-message negative";
  await loadPlatformData();
}

/* ============================================================
   Boot
   ============================================================ */
async function boot() {
  document.getElementById("authForm")?.addEventListener("submit", submitAuthLogin);

  // Page navigation
  document.querySelectorAll("[data-page-target]").forEach((button) => {
    button.addEventListener("click", () => switchPage(button.dataset.pageTarget));
  });

  // Sidebar
  document.getElementById("menuButton").addEventListener("click", toggleSidebar);
  document.getElementById("refreshButton").addEventListener("click", refreshAllData);

  // Review page
  document.getElementById("runReviewButton").addEventListener("click", runReviewAnalysis);
  document.getElementById("weekPrevBtn").addEventListener("click", () => {
    if (reviewWeekIndex > 0) { reviewWeekIndex--; renderWeekView(); }
  });
  document.getElementById("weekNextBtn").addEventListener("click", () => {
    if (reviewWeekIndex < reviewWeeks.length - 1) { reviewWeekIndex++; renderWeekView(); }
  });
  document.querySelectorAll("[data-week-res]").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll("[data-week-res]").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      weekResolution = btn.dataset.weekRes;
      weekIntradayChartRef = null;
      loadWeekIntraday();
    });
  });

  // Symbol tabs (IDE chart switcher)
  document.querySelectorAll("[data-chart-symbol]").forEach((btn) => {
    btn.addEventListener("click", () => {
      loadKlines(btn.dataset.chartSymbol, activeResolution);
    });
  });

  // Binance warning on source change
  document.getElementById("dataSourceSelect")?.addEventListener("change", (e) => {
    const symbols = Array.from(document.querySelectorAll("[name='backtestSymbol']:checked")).map((el) => el.value);
    warnBinanceUsStock(e.target.value, symbols, "dataSourceWarning");
  });

  // Review equity curve: checkbox toggling
  document.querySelectorAll("[name='reviewSymbol']").forEach((cb) => {
    cb.addEventListener("change", () => {
      const checked = Array.from(document.querySelectorAll("[name='reviewSymbol']:checked")).map((el) => el.value);
      updateEquitySeriesVisibility(checked);
    });
  });

  // IDE page controls
  document.getElementById("allocationForm").addEventListener("submit", saveAllocation);
  document.querySelectorAll("[data-factor-target]").forEach((button) => {
    button.addEventListener("click", () => switchFactorTab(button.dataset.factorTarget));
  });
  document.querySelectorAll("[data-resolution]").forEach((button) => {
    button.addEventListener("click", () => {
      loadKlines(activeSymbol, button.dataset.resolution);
    });
  });
  document.querySelectorAll("[data-order-type]:not(:disabled)").forEach((button) => {
    button.addEventListener("click", () => {
      document.querySelectorAll("[data-order-type]").forEach((b) => b.classList.toggle("active", b === button));
    });
  });
  document.getElementById("runBacktestButton").addEventListener("click", runBacktestFromControls);
  document.querySelectorAll("[data-paper-side]").forEach((button) => {
    button.addEventListener("click", () => submitPaperOrder(button.dataset.paperSide));
  });

  // Code slab toggle (default collapsed)
  document.getElementById("codeSlabToggle")?.addEventListener("click", () => {
    const wrap = document.getElementById("codeSlabWrap");
    const btn = document.getElementById("codeSlabToggle");
    if (!wrap || !btn) return;
    const collapsed = wrap.classList.toggle("collapsed");
    btn.textContent = collapsed ? "展开脚本 ▸" : "收起脚本 ▾";
    btn.setAttribute("aria-expanded", String(!collapsed));
  });

  // Charts are lazy-initialized when their page first becomes visible (see switchPage)

  if (!getAuthToken()) {
    showAuthScreen();
    return;
  }
  hideAuthScreen();

  // Load data
  await loadStaticState();
  await refreshLiveState();
  await loadPlatformData();

  setInterval(refreshLiveState, 5000);
}

boot().catch((error) => {
  document.getElementById("connectionStatus").textContent = "连接失败";
  console.error(error);
});

const fmtPct = (value) => `${(value * 100).toFixed(2)}%`;
const fmtMoney = (value) => Number.isFinite(value) && value > 0 ? `$${value.toFixed(2)}` : "-";

let latestAllocation = null;
let backtestChart = null;
let liveChart = null;
let activeSymbol = "NVDA";
let activeResolution = "1m";

function switchPage(targetId) {
  document.querySelectorAll(".page, .qd-page").forEach((page) => {
    page.classList.toggle("active", page.id === targetId);
  });
  document.querySelectorAll("[data-page-target]").forEach((button) => {
    button.classList.toggle("active", button.dataset.pageTarget === targetId);
  });
}

function switchWorkspace(targetId) {
  switchPage(targetId);
}

function toggleSidebar() {
  document.querySelector(".qd-app-shell").classList.toggle("sidebar-collapsed");
}

function toggleKnowledgeCard(event) {
  event.stopPropagation();
  document.getElementById("knowledgeCard").classList.toggle("collapsed");
}

function collapseKnowledgeCard() {
  document.getElementById("knowledgeCard").classList.add("collapsed");
}

function clsFor(value) {
  if (value < 0) return "negative";
  if (value > 0) return "positive";
  return "";
}

function renderGlossary(items) {
  const root = document.getElementById("glossary");
  root.innerHTML = items.map((item, idx) => `
    <article class="knowledge-item">
      <div class="knowledge-term">${idx + 1}. ${item.term}</div>
      <p>${item.body}</p>
    </article>
  `).join("");
}

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
      <div>收益 <span class="${clsFor(row.returnPct)}">${fmtPct(row.returnPct)}</span> / 最大回撤 <span class="${clsFor(row.maxDrawdownPct)}">${fmtPct(row.maxDrawdownPct)}</span> / 交易 ${row.trades}</div>
    </div>
  `).join("");
}

function renderDataSources(payload) {
  const root = document.getElementById("dataSourceCards");
  if (!root) return;
  const sources = payload.sources || [];
  root.innerHTML = sources.map((source) => `
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
    <div class="risk-light ${cls}">
      <strong>${label}</strong>
      <span>${status}</span>
    </div>
  `).join("");
}

function initTradingChart(containerId) {
  const container = document.getElementById(containerId);
  if (!container) return null;
  container.innerHTML = "";
  if (!window.LightweightCharts) {
    container.innerHTML = '<div class="empty-panel">图表库未加载，请检查网络后刷新。</div>';
    return null;
  }
  const chart = LightweightCharts.createChart(container, {
    width: container.clientWidth,
    height: container.clientHeight || (containerId === "liveChart" ? 460 : 300),
    layout: {
      background: { color: "transparent" },
      textColor: "#d7dde7",
    },
    grid: {
      vertLines: { color: "rgba(84, 99, 121, 0.25)" },
      horzLines: { color: "rgba(84, 99, 121, 0.25)" },
    },
    rightPriceScale: { borderColor: "rgba(84, 99, 121, 0.5)" },
    timeScale: { borderColor: "rgba(84, 99, 121, 0.5)", timeVisible: true },
    crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
  });
  const candles = chart.addCandlestickSeries({
    upColor: "#45d39a",
    downColor: "#e56b67",
    borderVisible: false,
    wickUpColor: "#45d39a",
    wickDownColor: "#e56b67",
  });
  const vwap = chart.addLineSeries({
    color: "#86b9ff",
    lineWidth: 2,
    priceLineVisible: false,
  });
  const resize = () => chart.applyOptions({ width: container.clientWidth });
  window.addEventListener("resize", resize);
  return { chart, candles, vwap };
}

function renderChartPlaceholder() {
  document.getElementById("backtestChart").dataset.ready = "true";
  document.getElementById("liveChart").dataset.ready = "true";
}

function renderChart(chartRef, data) {
  if (!chartRef || !data) return;
  chartRef.candles.setData(data.candles.map(([time, open, high, low, close]) => ({ time, open, high, low, close })));
  chartRef.vwap.setData(data.vwap.map(([time, value]) => ({ time, value })));
  chartRef.chart.timeScale().fitContent();
}

async function loadKlines(symbol = activeSymbol, resolution = activeResolution) {
  activeSymbol = symbol;
  activeResolution = resolution;
  const response = await fetch(`/api/klines?symbol=${encodeURIComponent(symbol)}&resolution=${encodeURIComponent(resolution)}`);
  const data = await response.json();
  renderChart(backtestChart, data);
  renderChart(liveChart, data);
  renderChartPlaceholder();
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
    if (!Number.isFinite(totalPct) || totalPct < 0 || totalPct > 100) {
      totalPctInput.classList.add("error");
      valid = false;
    }
    if (!Number.isFinite(tPct) || tPct < 0 || tPct > 100 || tPct > totalPct) {
      tPctInput.classList.add("error");
      valid = false;
    }
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
  const payload = {
    totalAccountQuote: Number(document.getElementById("totalAccountQuote").value),
    symbols,
  };
  const response = await fetch("/api/allocation", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
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

function collectBacktestControls() {
  return {
    symbols: Array.from(document.querySelectorAll("[name='backtestSymbol']:checked")).map((input) => input.value),
    sampleSplit: document.querySelector("#sampleSplit .active")?.dataset.sample || "in",
    slippageBps: Number(document.getElementById("slippageBps").value),
    spreadBps: Number(document.getElementById("spreadBps").value),
  };
}

async function runBacktestFromControls() {
  const payload = collectBacktestControls();
  const response = await fetch("/api/backtest/run", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const body = await response.json();
  if (!response.ok) {
    document.getElementById("connectionStatus").textContent = `回测失败：${body.error}`;
    return;
  }
  renderSummary(body.backtest.summary);
  renderAssets(body.backtest.assets);
  const symbol = payload.symbols[0] || "NVDA";
  await loadKlines(symbol);
  document.getElementById("updatedAt").textContent = new Date().toLocaleTimeString("zh-CN");
  await loadPlatformData();
}

function switchFactorTab(panelName) {
  document.querySelectorAll("[data-factor-target]").forEach((button) => {
    button.classList.toggle("active", button.dataset.factorTarget === panelName);
  });
  document.querySelectorAll("[data-factor-panel]").forEach((panel) => {
    panel.classList.toggle("active", panel.dataset.factorPanel === panelName);
  });
}

function switchOrderType(orderType) {
  document.querySelectorAll("[data-order-type]").forEach((button) => {
    button.classList.toggle("active", button.dataset.orderType === orderType);
  });
}

function bindControlPanel() {
  document.querySelectorAll("[name='backtestSymbol'], #slippageBps, #spreadBps").forEach((control) => {
    control.addEventListener("change", runBacktestFromControls);
  });
  document.querySelectorAll("#sampleSplit button").forEach((button) => {
    button.addEventListener("click", () => {
      document.querySelectorAll("#sampleSplit button").forEach((item) => item.classList.remove("active"));
      button.classList.add("active");
      runBacktestFromControls();
    });
  });
  document.querySelectorAll("[data-factor-target]").forEach((button) => {
    button.addEventListener("click", () => switchFactorTab(button.dataset.factorTarget));
  });
  document.querySelectorAll("[data-resolution]").forEach((button) => {
    button.addEventListener("click", () => {
      document.querySelectorAll("[data-resolution]").forEach((item) => item.classList.remove("active"));
      button.classList.add("active");
      loadKlines(activeSymbol, button.dataset.resolution);
    });
  });
  document.querySelectorAll("[data-order-type]:not(:disabled)").forEach((button) => {
    button.addEventListener("click", () => switchOrderType(button.dataset.orderType));
  });
  document.getElementById("runBacktestButton").addEventListener("click", runBacktestFromControls);
}

async function loadStaticState() {
  const response = await fetch("/api/state/static");
  const state = await response.json();
  renderGlossary(state.glossary);
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
    const response = await fetch("/api/state/live");
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
    fetch("/api/data-sources"),
    fetch("/api/paper/orders"),
    fetch("/api/backtest/results"),
  ]);
  const paperPayload = await paperRes.json();
  renderDataSources(await sourcesRes.json());
  renderPaperOrders(paperPayload);
  renderQuickTradeAccount(paperPayload);
  renderResultHistory(await resultsRes.json());
}

async function refreshAllData() {
  document.getElementById("connectionStatus").textContent = "刷新中";
  await Promise.all([loadStaticState(), refreshLiveState(), loadPlatformData(), loadKlines(activeSymbol, activeResolution)]);
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
  const response = await fetch("/api/paper/orders", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      symbol: activeSymbol,
      side,
      quoteAmount,
      orderType: "market",
    }),
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

async function boot() {
  document.querySelectorAll("[data-page-target]").forEach((button) => {
    button.addEventListener("click", () => switchWorkspace(button.dataset.pageTarget));
  });
  document.getElementById("knowledgeToggle").addEventListener("click", toggleKnowledgeCard);
  document.getElementById("menuButton").addEventListener("click", toggleSidebar);
  document.getElementById("refreshButton").addEventListener("click", refreshAllData);
  document.querySelectorAll("[data-paper-side]").forEach((button) => {
    button.addEventListener("click", () => submitPaperOrder(button.dataset.paperSide));
  });
  document.addEventListener("click", (event) => {
    const card = document.getElementById("knowledgeCard");
    if (!card.contains(event.target)) collapseKnowledgeCard();
  });
  document.getElementById("allocationForm").addEventListener("submit", saveAllocation);
  bindControlPanel();
  backtestChart = initTradingChart("backtestChart");
  liveChart = initTradingChart("liveChart");
  await loadStaticState();
  await refreshLiveState();
  await loadPlatformData();
  await loadKlines("NVDA");
  setInterval(refreshLiveState, 2000);
}

boot().catch((error) => {
  document.getElementById("connectionStatus").textContent = "连接失败";
  console.error(error);
});

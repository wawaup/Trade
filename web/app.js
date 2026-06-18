const fmtPct = (value) => `${(value * 100).toFixed(2)}%`;
const fmtMoney = (value) => value ? `$${value.toFixed(2)}` : "-";
let latestAllocation = null;

function switchPage(targetId) {
  document.querySelectorAll(".page").forEach((page) => {
    page.classList.toggle("active", page.id === targetId);
  });
  document.querySelectorAll("[data-page-target]").forEach((button) => {
    button.classList.toggle("active", button.dataset.pageTarget === targetId);
  });
}

document.querySelectorAll("[data-page-target]").forEach((button) => {
  button.addEventListener("click", () => switchPage(button.dataset.pageTarget));
});

function toggleKnowledgeCard() {
  document.getElementById("knowledgeCard").classList.toggle("collapsed");
}

document.getElementById("knowledgeToggle").addEventListener("click", toggleKnowledgeCard);

function collapseKnowledgeCard() {
  document.getElementById("knowledgeCard").classList.add("collapsed");
}

document.addEventListener("click", (event) => {
  const card = document.getElementById("knowledgeCard");
  if (!card.contains(event.target)) {
    collapseKnowledgeCard();
  }
});

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

function renderRiskLights(live) {
  const safeSpread = live.positions.every((row) => row.spreadPct <= 0.005);
  const activeT = live.positions.some((row) => row.layers > 0);
  const items = [
    ["点差过滤", safeSpread ? "SAFE" : "WIDE SPREAD", safeSpread ? "" : "warn"],
    ["插针保护", "READY", ""],
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

function renderChartPlaceholder() {
  document.getElementById("backtestChart").dataset.ready = "true";
  document.getElementById("liveChart").dataset.ready = "true";
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
  document.getElementById("terminalLog").textContent = live.logs.join("\n");
  renderRiskLights(live);
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

async function saveAllocation(event) {
  event.preventDefault();
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
  message.textContent = "仓位比例已保存";
  message.className = "form-message positive";
  await refreshState();
}

document.getElementById("allocationForm").addEventListener("submit", saveAllocation);

async function refreshState() {
  try {
    const response = await fetch("/api/state");
    const state = await response.json();
    renderGlossary(state.glossary);
    renderSummary(state.backtest.summary);
    renderAssets(state.backtest.assets);
    renderSensitivity(state.backtest.sensitivity);
    renderStress(state.backtest.stress);
    renderLive(state.live);
    renderAllocationForm(state.allocation);
    renderChartPlaceholder();
    document.getElementById("connectionStatus").textContent = "已连接";
    document.getElementById("updatedAt").textContent = new Date().toLocaleTimeString("zh-CN");
  } catch (error) {
    document.getElementById("connectionStatus").textContent = "连接失败";
    console.error(error);
  }
}

refreshState();
setInterval(refreshState, 5000);

const fmtPct = (value) => `${(value * 100).toFixed(2)}%`;
const fmtMoney = (value) => value ? `$${value.toFixed(2)}` : "-";

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

function renderLive(live) {
  document.getElementById("tradeMode").textContent = live.mode;
  document.getElementById("liveRows").innerHTML = live.positions.map((row) => `
    <tr>
      <td>${row.symbol}</td>
      <td>${row.status}</td>
      <td>${fmtMoney(row.lastPrice)}</td>
      <td>${row.layers}</td>
      <td class="${row.spreadPct > 0.005 ? "negative" : ""}">${fmtPct(row.spreadPct)}</td>
      <td class="${row.risk === "正常" ? "positive" : "warning"}">${row.risk}</td>
    </tr>
  `).join("");
  document.getElementById("guardrails").innerHTML = live.guardrails.map((item) => `<li>${item}</li>`).join("");
  document.getElementById("terminalLog").textContent = live.logs.join("\n");
}

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
    document.getElementById("connectionStatus").textContent = "已连接";
    document.getElementById("updatedAt").textContent = new Date().toLocaleTimeString("zh-CN");
  } catch (error) {
    document.getElementById("connectionStatus").textContent = "连接失败";
    console.error(error);
  }
}

refreshState();
setInterval(refreshState, 5000);

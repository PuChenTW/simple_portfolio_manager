const state = { portfolios: [], selectedId: null, requestId: 0 };

const COLORS = ["#12746e", "#e1904b", "#4d72b8", "#9b6cb4", "#579c70", "#c06672", "#697886"];
const elements = {
  dashboard: document.querySelector("#dashboard"),
  select: document.querySelector("#portfolio-select"),
  refresh: document.querySelector("#refresh-button"),
  status: document.querySelector("#status"),
  valuationTime: document.querySelector("#valuation-time"),
  totalValue: document.querySelector("#total-value"),
  currency: document.querySelector("#portfolio-currency"),
  securitiesValue: document.querySelector("#securities-value"),
  cashValue: document.querySelector("#cash-value"),
  cashWeight: document.querySelector("#cash-weight"),
  totalPnl: document.querySelector("#total-pnl"),
  totalPnlDetail: document.querySelector("#total-pnl-detail"),
  realizedPnl: document.querySelector("#realized-pnl"),
  unrealizedPnl: document.querySelector("#unrealized-pnl"),
  allocationBar: document.querySelector("#allocation-bar"),
  allocationLegend: document.querySelector("#allocation-legend"),
  allocationCaption: document.querySelector("#allocation-caption"),
  warnings: document.querySelector("#warnings"),
  positionsCount: document.querySelector("#positions-count"),
  positionsBody: document.querySelector("#positions-body"),
  positionsWrapper: document.querySelector("#positions-table-wrapper"),
  emptyPositions: document.querySelector("#empty-positions"),
};

class ApiError extends Error {
  constructor(message) {
    super(message);
    this.name = "ApiError";
  }
}

async function fetchJson(url) {
  const response = await fetch(url, { headers: { Accept: "application/json" } });
  if (response.ok) return response.json();

  let message = "無法取得資料，請稍後再試。";
  try {
    const error = await response.json();
    if (error.code && error.message) message = `${error.code}：${error.message}`;
  } catch {
    // Keep the generic error when the response is not a JSON API error.
  }
  throw new ApiError(message);
}

function clear(node) {
  node.replaceChildren();
}

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function isNegative(value) {
  return typeof value === "string" && value.startsWith("-");
}

function formatDecimal(value, maximumFractionDigits = 8) {
  if (value === null || value === undefined || value === "") return "—";
  const source = String(value);
  const matches = source.match(/^(-?)(\d+)(?:\.(\d+))?$/);
  if (!matches) return source;
  const [, sign, whole, fraction] = matches;
  const grouped = whole.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  const displayedFraction = fraction
    ?.slice(0, maximumFractionDigits)
    .replace(/0+$/, "");
  return `${sign}${grouped}${displayedFraction ? `.${displayedFraction}` : ""}`;
}

function formatMoney(value, currency) {
  return `${currency} ${formatDecimal(value, 4)}`;
}

function formatPercent(value) {
  return value === null || value === undefined ? "—" : `${formatDecimal(value, 2)}%`;
}

function formatTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) return value;
  return new Intl.DateTimeFormat("zh-TW", {
    dateStyle: "medium",
    timeStyle: "short",
    hour12: false,
  }).format(date);
}

function setMoneyValue(node, value, currency) {
  node.textContent = formatMoney(value, currency);
}

function setPnlValue(node, value, currency) {
  setMoneyValue(node, value, currency);
  node.classList.toggle("value--positive", !isNegative(value) && value !== "0");
  node.classList.toggle("value--negative", isNegative(value));
}

function setBusy(busy, message) {
  elements.select.disabled = busy || state.portfolios.length === 0;
  elements.refresh.disabled = busy || !state.selectedId;
  if (message) showStatus(message);
}

function showStatus(message, error = false, retry = false) {
  clear(elements.status);
  if (!message) return;
  const container = element("div", `status__message${error ? " status__message--error" : ""}`);
  container.append(element("span", "", message));
  if (retry) {
    const button = element("button", "status__action", "再次載入");
    button.type = "button";
    button.addEventListener("click", loadDashboard);
    container.append(button);
  }
  elements.status.append(container);
}

function populatePortfolioSelect() {
  clear(elements.select);
  for (const portfolio of state.portfolios) {
    const option = document.createElement("option");
    option.value = portfolio.id;
    option.textContent = `${portfolio.name} (${portfolio.base_currency})`;
    option.selected = portfolio.id === state.selectedId;
    elements.select.append(option);
  }
}

function safeWidth(value) {
  const source = String(value ?? "0");
  return /^\d+(?:\.\d+)?$/.test(source) ? `${source}%` : "0%";
}

function renderAllocation(summary) {
  clear(elements.allocationBar);
  clear(elements.allocationLegend);
  const allocations = [
    ...summary.positions.map((position) => ({
      label: position.ticker,
      value: position.market_value,
      weight: position.weight_percent,
    })),
    { label: "現金", value: summary.cash_value, weight: summary.cash.weight_percent },
  ].filter((item) => item.weight !== null && item.weight !== "0");

  elements.allocationCaption.textContent = `${allocations.length} 項資產`;
  elements.allocationBar.setAttribute(
    "aria-label",
    `資產配置：${allocations.map((item) => `${item.label} ${formatPercent(item.weight)}`).join("，")}`
  );

  allocations.forEach((item, index) => {
    const color = COLORS[index % COLORS.length];
    const segment = element("div", "allocation-segment");
    segment.style.setProperty("--allocation-color", color);
    segment.style.setProperty("--allocation-weight", safeWidth(item.weight));
    elements.allocationBar.append(segment);

    const entry = element("li");
    const dot = element("span", "legend-dot");
    dot.style.setProperty("--allocation-color", color);
    entry.append(dot, element("span", "legend-label", item.label), element("span", "legend-value", formatPercent(item.weight)));
    elements.allocationLegend.append(entry);
  });
}

function renderWarnings(summary) {
  clear(elements.warnings);
  const messages = [...summary.warnings];
  const staleTickers = summary.positions.filter((position) => position.price_stale).map((position) => position.ticker);
  if (staleTickers.length) messages.unshift(`以下標的使用快取報價：${staleTickers.join("、")}。`);
  if (!messages.length) {
    elements.warnings.hidden = true;
    return;
  }
  messages.forEach((message) => elements.warnings.append(element("div", "warning", message)));
  elements.warnings.hidden = false;
}

function createCell(content, className = "") {
  const cell = element("td", className);
  if (content instanceof Node) cell.append(content);
  else cell.textContent = content;
  return cell;
}

function renderPositions(summary) {
  clear(elements.positionsBody);
  const positions = summary.positions;
  elements.positionsCount.textContent = `${positions.length} 項持倉`;
  elements.emptyPositions.hidden = positions.length !== 0;
  elements.positionsWrapper.hidden = positions.length === 0;

  positions.forEach((position) => {
    const row = document.createElement("tr");
    const instrument = element("div", "instrument");
    instrument.append(element("span", "instrument__ticker", position.ticker), element("span", "instrument__name", position.name));
    if (position.price_stale) instrument.append(element("span", "stale-badge", "快取報價"));
    row.append(createCell(instrument));
    row.append(createCell(formatDecimal(position.quantity), "number"));
    row.append(createCell(formatMoney(position.average_cost, summary.portfolio.base_currency), "number"));
    row.append(createCell(formatMoney(position.current_price, summary.portfolio.base_currency), "number"));
    row.append(createCell(formatMoney(position.market_value, summary.portfolio.base_currency), "number"));

    const pnl = element("div", `pnl${isNegative(position.total_pnl) ? " value--negative" : position.total_pnl === "0" ? "" : " value--positive"}`);
    pnl.append(element("span", "", formatMoney(position.total_pnl, summary.portfolio.base_currency)));
    pnl.append(element("small", "", `未實現 ${formatMoney(position.unrealized_pnl, summary.portfolio.base_currency)}`));
    row.append(createCell(pnl, "number"));
    const returnValue = element("span", isNegative(position.unrealized_pnl_percent) ? "value--negative" : "value--positive", formatPercent(position.unrealized_pnl_percent));
    row.append(createCell(returnValue, "number"));
    row.append(createCell(formatPercent(position.weight_percent), "number"));

    const tags = element("div", "tag-list");
    if (position.tags.length) position.tags.forEach((tag) => tags.append(element("span", "tag", tag)));
    else tags.append(element("span", "empty-tag", "—"));
    row.append(createCell(tags));
    elements.positionsBody.append(row);
  });
}

function renderSummary(summary) {
  const currency = summary.portfolio.base_currency;
  elements.dashboard.hidden = false;
  elements.valuationTime.textContent = `估值時間：${formatTime(summary.valuation_as_of)}`;
  elements.currency.textContent = `基準貨幣：${currency}`;
  setMoneyValue(elements.totalValue, summary.total_value, currency);
  setMoneyValue(elements.securitiesValue, summary.securities_value, currency);
  setMoneyValue(elements.cashValue, summary.cash_value, currency);
  elements.cashWeight.textContent = `配置 ${formatPercent(summary.cash.weight_percent)}`;
  setPnlValue(elements.totalPnl, summary.total_pnl, currency);
  elements.totalPnlDetail.textContent = `已實現 ${formatMoney(summary.realized_pnl, currency)} · 未實現 ${formatMoney(summary.unrealized_pnl, currency)}`;
  setPnlValue(elements.realizedPnl, summary.realized_pnl, currency);
  setPnlValue(elements.unrealizedPnl, summary.unrealized_pnl, currency);
  renderAllocation(summary);
  renderWarnings(summary);
  renderPositions(summary);
}

async function loadSummary() {
  if (!state.selectedId) return;
  const requestId = ++state.requestId;
  setBusy(true, "正在更新投資組合估值…");
  try {
    const summary = await fetchJson(`/api/v1/portfolios/${encodeURIComponent(state.selectedId)}/summary`);
    if (requestId !== state.requestId) return;
    renderSummary(summary);
    showStatus("");
  } catch (error) {
    if (requestId !== state.requestId) return;
    elements.dashboard.hidden = true;
    elements.valuationTime.textContent = "無法取得投資組合估值";
    showStatus(error.message, true, true);
  } finally {
    if (requestId === state.requestId) setBusy(false);
  }
}

async function loadDashboard() {
  const requestId = ++state.requestId;
  setBusy(true, "正在載入投資組合…");
  try {
    const portfolios = await fetchJson("/api/v1/portfolios");
    if (requestId !== state.requestId) return;
    state.portfolios = portfolios;
    if (!portfolios.length) {
      state.selectedId = null;
      populatePortfolioSelect();
      elements.dashboard.hidden = true;
      elements.valuationTime.textContent = "尚未建立投資組合";
      showStatus("尚未建立投資組合。請先透過 API 建立資料後再回到此頁。", false);
      setBusy(false);
      return;
    }
    if (!portfolios.some((portfolio) => portfolio.id === state.selectedId)) state.selectedId = portfolios[0].id;
    populatePortfolioSelect();
    await loadSummary();
  } catch (error) {
    if (requestId !== state.requestId) return;
    state.portfolios = [];
    state.selectedId = null;
    elements.dashboard.hidden = true;
    elements.valuationTime.textContent = "無法載入投資組合";
    showStatus(error.message, true, true);
    setBusy(false);
  }
}

elements.select.addEventListener("change", () => {
  state.selectedId = elements.select.value;
  loadSummary();
});
elements.refresh.addEventListener("click", loadSummary);
loadDashboard();

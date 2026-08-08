const state = {
  portfolios: [],
  selectedId: null,
  requestId: 0,
  tab: "overview",
  // Each tab loads on first view and is cached per portfolio, so switching back is instant
  // and a tab nobody opens costs nothing.
  loaded: {},
  groupId: null,
  // "all" | "ytd" | "1y" | "custom". Defaults to the portfolio's full history rather than a
  // fixed lookback, since a 30-day default made an inception-to-date question unanswerable
  // without hand-editing query params.
  perfRangeMode: "all",
  perfCustomStart: null,
  perfCustomEnd: null,
  // The journal pages independently of its tab: paging through it must not re-fetch the
  // summary, the performance series, and one instrument profile per position.
  journalOffset: 0,
  // Which events are showing their legs, by event id. Kept across a page load so the panel
  // does not silently collapse everything when the user pages back and forth.
  journalExpanded: new Set(),
};

const JOURNAL_PAGE_SIZE = 25;

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
  tabs: document.querySelectorAll(".tab"),
  panels: document.querySelectorAll("[data-panel]"),
  // Performance
  rangeOptions: document.querySelectorAll(".range-picker__option"),
  rangeCustomInputs: document.querySelector("#range-custom-inputs"),
  rangeStart: document.querySelector("#range-start"),
  rangeEnd: document.querySelector("#range-end"),
  rangeApply: document.querySelector("#range-apply"),
  perfEnding: document.querySelector("#perf-ending"),
  perfRange: document.querySelector("#perf-range"),
  perfTwr: document.querySelector("#perf-twr"),
  perfXirr: document.querySelector("#perf-xirr"),
  perfXirrDetail: document.querySelector("#perf-xirr-detail"),
  perfFlows: document.querySelector("#perf-flows"),
  perfFlowsDetail: document.querySelector("#perf-flows-detail"),
  perfCoverage: document.querySelector("#perf-coverage"),
  perfMethod: document.querySelector("#perf-method"),
  perfVersion: document.querySelector("#perf-version"),
  navChart: document.querySelector("#nav-chart"),
  navCaption: document.querySelector("#nav-caption"),
  navEmpty: document.querySelector("#nav-empty"),
  // Quality
  qEvents: document.querySelector("#q-events"),
  qUnruled: document.querySelector("#q-unruled"),
  qClassified: document.querySelector("#q-classified"),
  qCoverage: document.querySelector("#q-coverage"),
  qCoverageDetail: document.querySelector("#q-coverage-detail"),
  qualityWarnings: document.querySelector("#quality-warnings"),
  classBody: document.querySelector("#class-body"),
  issuerDialog: document.querySelector("#issuer-dialog"),
  issuerForm: document.querySelector("#issuer-form"),
  issuerDialogTicker: document.querySelector("#issuer-dialog-ticker"),
  issuerLegalName: document.querySelector("#issuer-legal-name"),
  issuerDisplayName: document.querySelector("#issuer-display-name"),
  issuerCountry: document.querySelector("#issuer-country"),
  issuerDialogError: document.querySelector("#issuer-dialog-error"),
  issuerCancel: document.querySelector("#issuer-cancel"),
  issuerSubmit: document.querySelector("#issuer-submit"),
  journalBody: document.querySelector("#journal-body"),
  journalCount: document.querySelector("#journal-count"),
  journalPage: document.querySelector("#journal-page"),
  journalPrev: document.querySelector("#journal-prev"),
  journalNext: document.querySelector("#journal-next"),
  // Consolidation
  groupMissing: document.querySelector("#group-missing"),
  groupContent: document.querySelector("#group-content"),
  cTotal: document.querySelector("#c-total"),
  cCurrency: document.querySelector("#c-currency"),
  cSecurities: document.querySelector("#c-securities"),
  cCash: document.querySelector("#c-cash"),
  cCashDetail: document.querySelector("#c-cash-detail"),
  cCoverage: document.querySelector("#c-coverage"),
  cCoverageDetail: document.querySelector("#c-coverage-detail"),
  cWarnings: document.querySelector("#c-warnings"),
  fxList: document.querySelector("#fx-list"),
  issuerPanel: document.querySelector("#issuer-panel"),
  issuerList: document.querySelector("#issuer-list"),
  cPositionsBody: document.querySelector("#c-positions-body"),
  cPositionsCount: document.querySelector("#c-positions-count"),
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

async function putJson(url, body) {
  const response = await fetch(url, {
    method: "PUT",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify(body),
  });
  if (response.ok) return response.json();

  let message = "無法儲存變更，請稍後再試。";
  try {
    const error = await response.json();
    if (error.code && error.message) message = `${error.code}：${error.message}`;
  } catch {
    // Keep the generic error when the response is not a JSON API error.
  }
  throw new ApiError(message);
}

function newRequestId() {
  return crypto.randomUUID();
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
  return `${currency} ${formatDecimal(value, 2)}`;
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
    const summary = await fetchJson(`api/v1/portfolios/${encodeURIComponent(state.selectedId)}/summary`);
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
    const portfolios = await fetchJson("api/v1/portfolios");
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


/* Tabs --------------------------------------------------------------------- */

function selectTab(name) {
  state.tab = name;
  elements.tabs.forEach((tab) => {
    const active = tab.dataset.tab === name;
    tab.classList.toggle("tab--active", active);
    tab.setAttribute("aria-selected", String(active));
  });
  elements.panels.forEach((panel) => {
    panel.hidden = panel.dataset.panel !== name;
  });
  loadTab(name);
}

function loadTab(name) {
  if (!state.selectedId && name !== "consolidated") return;
  const key = `${name}:${state.selectedId}`;
  if (state.loaded[key]) return;
  state.loaded[key] = true;
  const loaders = {
    performance: loadPerformance,
    quality: loadQuality,
    consolidated: loadConsolidated,
  };
  const panel = document.querySelector(`[data-panel="${name}"]`);
  loaders[name]?.().catch((error) => {
    // Clear the flag so the next visit retries, and put the reason inside the panel that
    // failed. A blank panel with the message elsewhere reads as "no data" rather than "error".
    state.loaded[key] = false;
    if (!panel) return;
    const notice = element("div", "warnings");
    const message = element("div", "warning", `無法載入此頁：${error.message}`);
    const retry = element("button", "status__action", "重試");
    retry.type = "button";
    retry.addEventListener("click", () => {
      notice.remove();
      loadTab(name);
    });
    message.append(retry);
    notice.append(message);
    panel.prepend(notice);
  });
}

function isoDaysAgo(days) {
  const date = new Date();
  date.setUTCDate(date.getUTCDate() - days);
  return date.toISOString().slice(0, 10);
}

function today() {
  return new Date().toISOString().slice(0, 10);
}

function startOfYear() {
  return `${new Date().getUTCFullYear()}-01-01`;
}

// "all" probes from a fixed, far-back anchor rather than the portfolio row's created_at --
// that timestamp is when the row was inserted into this app, not when the investor's history
// actually began, and backdated first transactions are the common case. resolveHistoryRange()
// narrows the probe down to the real first/last snapshot, so an overly wide anchor costs one
// query, not a truncated "all".
const EPOCH_START = "2000-01-01";

function perfWindowStart() {
  switch (state.perfRangeMode) {
    case "ytd":
      return startOfYear();
    case "1y":
      return isoDaysAgo(365);
    case "custom":
      return state.perfCustomStart || startOfYear();
    case "all":
    default:
      return EPOCH_START;
  }
}

function perfWindowEnd() {
  return state.perfRangeMode === "custom" && state.perfCustomEnd ? state.perfCustomEnd : today();
}

// The API speaks English; this dashboard speaks Chinese. Patterns are matched loosely so a
// reworded warning still translates, and anything unrecognised is shown verbatim rather than
// swallowed -- a hidden warning is worse than an untranslated one.
const WARNING_PATTERNS = [
  [/(\d+) dates in this period have no snapshot/, (n) => `此區間有 ${n} 日沒有估值快照，跨越缺口的子期間會被直接鏈結，可能低估波動並使報酬失真。`],
  [/(\d+) dates in this range have no snapshot/, (n) => `此區間有 ${n} 日沒有估值快照，僅列出缺漏日期，不會內插補值。`],
  [/(\d+) snapshots are partial/, (n) => `${n} 個快照為部分估值：當日有標的無法取得價格，已排除在總額外。`],
];

function translateWarning(message) {
  for (const [pattern, build] of WARNING_PATTERNS) {
    const match = message.match(pattern);
    if (match) return build(match[1]);
  }
  return message;
}

function renderMessages(container, messages) {
  clear(container);
  if (!messages.length) {
    container.hidden = true;
    return;
  }
  messages.forEach((message) => container.append(element("div", "warning", translateWarning(message))));
  container.hidden = false;
}

/* Performance -------------------------------------------------------------- */

const SVG_NS = "http://www.w3.org/2000/svg";

function svg(tag, attributes = {}) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, String(value));
  return node;
}

function renderNavChart(snapshots, currency) {
  clear(elements.navChart);
  elements.navEmpty.hidden = snapshots.length > 0;
  elements.navChart.hidden = snapshots.length === 0;
  if (snapshots.length < 2) return;

  const width = 900;
  const height = 260;
  const pad = { top: 16, right: 16, bottom: 28, left: 74 };
  const plotWidth = width - pad.left - pad.right;
  const plotHeight = height - pad.top - pad.bottom;

  const values = snapshots.map((item) => Number(item.total_value));
  const min = Math.min(...values);
  const max = Math.max(...values);
  // A flat series would divide by zero; give it a nominal band so the line sits mid-chart.
  const span = max - min || Math.abs(max) || 1;
  const low = min - span * 0.08;
  const high = max + span * 0.08;

  const x = (index) =>
    pad.left + (snapshots.length === 1 ? plotWidth / 2 : (index / (snapshots.length - 1)) * plotWidth);
  const y = (value) => pad.top + plotHeight - ((value - low) / (high - low)) * plotHeight;

  const root = svg("svg", {
    viewBox: `0 0 ${width} ${height}`,
    role: "img",
    "aria-label": `每日淨值走勢，${snapshots.length} 個交易日`,
  });

  // Horizontal gridlines with value labels.
  for (let step = 0; step <= 3; step += 1) {
    const value = low + ((high - low) * step) / 3;
    const gridY = y(value);
    root.append(svg("line", { class: "chart__grid", x1: pad.left, x2: width - pad.right, y1: gridY, y2: gridY }));
    const label = svg("text", { class: "chart__axis", x: pad.left - 8, y: gridY + 4, "text-anchor": "end" });
    label.textContent = formatDecimal(String(Math.round(value)), 0);
    root.append(label);
  }

  const line = snapshots.map((item, index) => `${index === 0 ? "M" : "L"}${x(index)},${y(Number(item.total_value))}`).join(" ");
  const base = pad.top + plotHeight;
  root.append(svg("path", { class: "chart__area", d: `${line} L${x(snapshots.length - 1)},${base} L${x(0)},${base} Z` }));
  root.append(svg("path", { class: "chart__line", d: line }));

  // Date labels at both ends, plus the middle when there is room.
  const ticks = snapshots.length > 6 ? [0, Math.floor((snapshots.length - 1) / 2), snapshots.length - 1] : [0, snapshots.length - 1];
  ticks.forEach((index) => {
    const label = svg("text", {
      class: "chart__axis",
      x: x(index),
      y: height - 8,
      "text-anchor": index === 0 ? "start" : index === snapshots.length - 1 ? "end" : "middle",
    });
    label.textContent = snapshots[index].valuation_date.slice(5);
    root.append(label);
  });

  const tooltip = svg("g", { class: "chart__tooltip" });
  tooltip.setAttribute("opacity", "0");
  const tooltipBox = svg("rect", { width: 168, height: 42 });
  const tooltipDate = svg("text", { x: 10, y: 17 });
  const tooltipValue = svg("text", { x: 10, y: 33 });
  tooltip.append(tooltipBox, tooltipDate, tooltipValue);

  snapshots.forEach((item, index) => {
    const pointX = x(index);
    const pointY = y(Number(item.total_value));
    const partial = item.status === "partial";

    const hit = svg("circle", { class: "chart__hit", cx: pointX, cy: pointY, r: 14, tabindex: "0" });
    const show = () => {
      tooltipDate.textContent = `${item.valuation_date}${partial ? "（部分估值）" : ""}`;
      tooltipValue.textContent = `${currency} ${formatDecimal(item.total_value, 2)}`;
      const boxX = Math.min(Math.max(pointX - 84, pad.left), width - pad.right - 168);
      tooltip.setAttribute("transform", `translate(${boxX}, ${Math.max(pointY - 52, 2)})`);
      tooltip.setAttribute("opacity", "1");
    };
    const hide = () => tooltip.setAttribute("opacity", "0");
    hit.addEventListener("mouseenter", show);
    hit.addEventListener("focus", show);
    hit.addEventListener("mouseleave", hide);
    hit.addEventListener("blur", hide);

    root.append(hit);
    root.append(svg("circle", { class: `chart__point${partial ? " chart__point--partial" : ""}`, cx: pointX, cy: pointY, r: 3 }));
  });

  root.append(tooltip);
  elements.navChart.append(root);
}

function renderPerformance(performance, history, currency) {
  setMoneyValue(elements.perfEnding, performance.ending_value, currency);
  elements.perfRange.textContent = `${performance.start_date} 至 ${performance.end_date}`;

  elements.perfTwr.textContent = formatPercent(performance.twr_percent);
  elements.perfTwr.classList.toggle("value--positive", !isNegative(performance.twr_percent) && performance.twr_percent !== null);
  elements.perfTwr.classList.toggle("value--negative", isNegative(performance.twr_percent));

  elements.perfXirr.textContent = formatPercent(performance.xirr_percent);
  elements.perfXirr.classList.toggle("value--positive", !isNegative(performance.xirr_percent) && performance.xirr_percent !== null);
  elements.perfXirr.classList.toggle("value--negative", isNegative(performance.xirr_percent));
  elements.perfXirrDetail.textContent = performance.xirr_unavailable_reason || "年化，含投入時點";

  const net = Number(performance.external_inflows) + Number(performance.external_outflows);
  setMoneyValue(elements.perfFlows, String(net), currency);
  elements.perfFlowsDetail.textContent = `流入 ${formatMoney(performance.external_inflows, currency)} · 流出 ${formatMoney(performance.external_outflows, currency)}`;

  const messages = [...performance.coverage.warnings];
  renderMessages(elements.perfCoverage, messages);
  elements.perfCoverage.hidden = !messages.length;

  clear(elements.perfMethod);
  const entries = [
    ["TWR 方法", `${performance.twr_method_description}（${performance.twr_method}）`],
    ["XIRR 方法", `${performance.xirr_method_description}（${performance.xirr_method}）`],
  ];
  entries.forEach(([term, description]) => {
    elements.perfMethod.append(element("dt", "", term), element("dd", "", description));
  });
  elements.perfVersion.textContent = `計算版本 ${performance.calculation_version}`;

  const snapshots = history.snapshots;
  const partial = snapshots.filter((item) => item.status === "partial").length;
  elements.navCaption.textContent = snapshots.length
    ? `${snapshots.length} 個估值日${history.missing_dates.length ? ` · ${history.missing_dates.length} 日無快照` : ""}${partial ? ` · ${partial} 日部分估值` : ""}`
    : "尚無快照";
  renderNavChart(snapshots, currency);
}

// Narrow the window to the snapshots that exist, so a reported gap is a real gap rather than
// an artifact of asking for dates before the portfolio had any history. The wide probe itself
// spans the selected range (all history by default), never a fixed lookback.
async function resolveHistoryRange(id) {
  const wide = `start_date=${perfWindowStart()}&end_date=${perfWindowEnd()}`;
  const probe = await fetchJson(`api/v1/portfolios/${id}/nav-history?${wide}`);
  if (!probe.snapshots.length) return { range: wide, history: probe };
  const start = probe.snapshots[0].valuation_date;
  const end = probe.snapshots[probe.snapshots.length - 1].valuation_date;
  const range = `start_date=${start}&end_date=${end}`;
  return { range, history: await fetchJson(`api/v1/portfolios/${id}/nav-history?${range}`) };
}

function updateRangePickerUI() {
  elements.rangeOptions.forEach((button) => {
    button.classList.toggle("range-picker__option--active", button.dataset.range === state.perfRangeMode);
  });
  elements.rangeCustomInputs.hidden = state.perfRangeMode !== "custom";
}

async function loadPerformance() {
  updateRangePickerUI();
  const portfolio = state.portfolios.find((item) => item.id === state.selectedId);
  const currency = portfolio?.base_currency ?? "";
  const id = encodeURIComponent(state.selectedId);
  const { range, history } = await resolveHistoryRange(id);
  const performance = await fetchJson(`api/v1/portfolios/${id}/performance?${range}`);
  renderPerformance(performance, history, currency);
}

/* Data quality ------------------------------------------------------------- */

const PROVENANCE_LABELS = {
  manual_override: ["人工覆寫", "badge--manual"],
  verified_internal: ["內部確認", "badge--provider"],
  provider: ["資料商", "badge--provider"],
  derived: ["推導", "badge--provider"],
  unclassified: ["未分類", "badge--none"],
};

const FLOW_LABELS = {
  external: ["外部資金", "badge--external"],
  internal: ["投資組合活動", "badge--internal"],
  unknown: ["未分類", "badge--unknown"],
};

function badge(label, className) {
  return element("span", `badge ${className}`, label);
}

function provenanceBadge(value) {
  const [label, className] = PROVENANCE_LABELS[value] ?? [value ?? "—", "badge--none"];
  return badge(label, className);
}

function renderClassifications(profiles) {
  clear(elements.classBody);
  let classified = 0;
  profiles.forEach((profile) => {
    const assetClass = profile.classification?.asset_class;
    if (assetClass?.value && assetClass.value !== "unclassified") classified += 1;

    const row = document.createElement("tr");
    const instrument = element("div", "instrument");
    instrument.append(element("span", "instrument__ticker", profile.ticker));
    if (profile.name) instrument.append(element("span", "instrument__name", profile.name));
    row.append(createCell(instrument));
    row.append(createCell(assetClass?.value ?? "—"));
    row.append(createCell(profile.classification?.security_type?.value ?? "—"));
    row.append(createCell(profile.issuer?.display_name || profile.issuer?.legal_name || "—"));
    row.append(createCell(provenanceBadge(assetClass?.provenance)));

    const editButton = element("button", "button--link", profile.issuer ? "變更" : "對應發行人");
    editButton.type = "button";
    editButton.addEventListener("click", () => openIssuerDialog(profile));
    row.append(createCell(editButton));

    elements.classBody.append(row);
  });
  elements.qClassified.textContent = `${classified} / ${profiles.length}`;
}

let issuerDialogReference = null;

function openIssuerDialog(profile) {
  issuerDialogReference = profile.instrument_id;
  elements.issuerDialogTicker.textContent = profile.name
    ? `${profile.ticker} · ${profile.name}`
    : profile.ticker;
  elements.issuerLegalName.value = profile.issuer?.legal_name ?? "";
  elements.issuerDisplayName.value = profile.issuer?.display_name ?? "";
  elements.issuerCountry.value = profile.issuer?.country_of_domicile ?? "";
  elements.issuerDialogError.hidden = true;
  elements.issuerDialogError.textContent = "";
  elements.issuerSubmit.disabled = false;
  elements.issuerSubmit.textContent = "儲存對應";
  elements.issuerDialog.showModal();
  elements.issuerLegalName.focus();
}

async function submitIssuerMapping() {
  const legalName = elements.issuerLegalName.value.trim();
  if (!legalName) return;

  elements.issuerSubmit.disabled = true;
  elements.issuerSubmit.textContent = "儲存中…";
  elements.issuerDialogError.hidden = true;

  try {
    await putJson(`api/v1/instruments/${encodeURIComponent(issuerDialogReference)}/issuer`, {
      request_id: newRequestId(),
      legal_name: legalName,
      display_name: elements.issuerDisplayName.value.trim() || null,
      country_of_domicile: elements.issuerCountry.value.trim().toUpperCase() || null,
    });
    elements.issuerDialog.close();
    delete state.loaded[`quality:${state.selectedId}`];
    await loadQuality();
  } catch (error) {
    elements.issuerDialogError.textContent =
      error instanceof ApiError ? error.message : "無法儲存變更，請稍後再試。";
    elements.issuerDialogError.hidden = false;
    elements.issuerSubmit.disabled = false;
    elements.issuerSubmit.textContent = "儲存對應";
  }
}

elements.issuerForm.addEventListener("submit", (event) => {
  event.preventDefault();
  submitIssuerMapping();
});
elements.issuerCancel.addEventListener("click", () => elements.issuerDialog.close());

// A badge that asks for action must be reserved for events where action is possible. Every event
// now enters through the journal, so the only open question left is a flow nobody can classify.
function statusBadge(event) {
  if (event.flow_classification === "unknown") return badge("待確認", "badge--unknown");
  return badge("已入帳", "badge--internal");
}

// Leg vocabulary, kept beside FLOW_LABELS so the two read the same way. A leg type nobody
// translated still shows its raw value rather than vanishing.
const LEG_TYPE_LABELS = {
  security: "證券",
  cash: "現金",
  fee: "手續費",
  tax: "稅金",
  income: "收益",
  receivable: "應收",
  other: "其他",
};

// A capitalized fee or tax carries no amount_delta -- the money is already inside the security
// leg -- so its figure lives in the leg metadata. Showing a fee row with every field blank
// would read as "no fee", which is the opposite of what the ledger recorded.
function legAmount(leg) {
  if (leg.amount_delta !== null && leg.amount_delta !== undefined) {
    return formatMoney(leg.amount_delta, leg.currency);
  }
  if (!leg.metadata) return "—";
  try {
    const { amount } = JSON.parse(leg.metadata);
    return amount === undefined ? "—" : `${formatMoney(amount, leg.currency)}（已計入成本）`;
  } catch {
    return "—";
  }
}

// A leg's amounts are in the leg's own currency, not the portfolio's: a USD fee on a TWD
// portfolio must not be relabelled TWD.
function renderLegs(legs) {
  const list = element("div", "legs");
  if (!legs || !legs.length) {
    list.append(element("p", "muted", "這筆事件沒有分錄。"));
    return list;
  }
  legs.forEach((leg) => {
    const row = element("div", "legs__row");
    // A placeholder holds the column open on wide screens and is hidden once the row stacks.
    const field = (className, text) => {
      const node = element("span", className, text);
      if (text === "—") node.classList.add("legs__empty");
      return node;
    };
    row.append(element("span", "legs__type", LEG_TYPE_LABELS[leg.leg_type] ?? leg.leg_type));
    row.append(field("legs__ticker", leg.ticker ?? "—"));
    row.append(field("legs__number", formatDecimal(leg.quantity_delta)));
    row.append(
      field(
        "legs__number",
        leg.unit_price === null || leg.unit_price === undefined
          ? "—"
          : formatMoney(leg.unit_price, leg.currency)
      )
    );
    const amount = field("legs__number", legAmount(leg));
    if (isNegative(leg.amount_delta)) amount.classList.add("value--negative");
    row.append(amount);
    list.append(row);
  });
  return list;
}

function toggleJournalRow(eventId) {
  if (state.journalExpanded.has(eventId)) state.journalExpanded.delete(eventId);
  else state.journalExpanded.add(eventId);
  renderJournal(state.journalPage);
}

function renderJournal(page) {
  // Held so an expand/collapse can re-render without re-fetching the page.
  state.journalPage = page;
  clear(elements.journalBody);

  const pageCount = Math.max(1, Math.ceil(page.total / page.limit));
  const current = Math.floor(page.offset / page.limit) + 1;
  elements.journalCount.textContent = `共 ${page.total} 筆`;
  elements.journalPage.textContent = `第 ${current} / ${pageCount} 頁`;
  elements.journalPrev.disabled = page.offset === 0;
  elements.journalNext.disabled = page.offset + page.items.length >= page.total;

  page.items.forEach((event) => {
    const expanded = state.journalExpanded.has(event.id);
    const row = document.createElement("tr");

    const toggle = element("button", "button--link", expanded ? "▾" : "▸");
    toggle.type = "button";
    toggle.setAttribute("aria-expanded", String(expanded));
    toggle.setAttribute("aria-label", expanded ? "收合分錄" : "展開分錄");
    toggle.addEventListener("click", () => toggleJournalRow(event.id));
    row.append(createCell(toggle));

    row.append(createCell(formatTime(event.occurred_at)));
    row.append(createCell(event.event_type));
    const [label, className] = FLOW_LABELS[event.flow_classification] ?? ["—", "badge--none"];
    const flow = element("div", "instrument");
    flow.append(badge(label, className));
    row.append(createCell(flow));
    row.append(createCell(event.source_reference || event.source || "—"));
    row.append(createCell(statusBadge(event)));
    elements.journalBody.append(row);

    if (!expanded) return;
    const detail = document.createElement("tr");
    const cell = createCell(renderLegs(event.legs), "legs-cell");
    cell.colSpan = 6;
    detail.append(cell);
    elements.journalBody.append(detail);
  });
  elements.qEvents.textContent = String(page.total);
}

async function loadJournal(offset) {
  const id = encodeURIComponent(state.selectedId);
  const requestId = ++state.requestId;
  const page = await fetchJson(
    `api/v1/portfolios/${id}/transactions` +
      `?limit=${JOURNAL_PAGE_SIZE}&offset=${offset}&include_legs=true`
  );
  if (requestId !== state.requestId) return;
  state.journalOffset = offset;
  renderJournal(page);
}

function pageJournal(delta) {
  const next = state.journalOffset + delta * JOURNAL_PAGE_SIZE;
  if (next < 0) return;
  loadJournal(next).catch((error) => {
    showStatus(error.message ?? "無法取得帳務事件，請稍後再試。", true);
  });
}

elements.journalPrev.addEventListener("click", () => pageJournal(-1));
elements.journalNext.addEventListener("click", () => pageJournal(1));

async function loadQuality() {
  const id = encodeURIComponent(state.selectedId);
  const { range, history } = await resolveHistoryRange(id);
  const [summary, journal, performance] = await Promise.all([
    fetchJson(`api/v1/portfolios/${id}/summary`),
    fetchJson(
      `api/v1/portfolios/${id}/transactions` +
        `?limit=${JOURNAL_PAGE_SIZE}&offset=${state.journalOffset}&include_legs=true`
    ),
    fetchJson(`api/v1/portfolios/${id}/performance?${range}`),
  ]);

  renderJournal(journal);
  elements.qUnruled.textContent = String(performance.coverage.unclassified_flow_events);
  const built = history.snapshots.length;
  const missing = history.missing_dates.length;
  elements.qCoverage.textContent = `${built} / ${built + missing}`;
  elements.qCoverageDetail.textContent = missing ? `${missing} 日尚未建立快照` : "區間內每日皆有快照";

  const profiles = await Promise.all(
    summary.positions.map((position) =>
      fetchJson(`api/v1/instruments/${encodeURIComponent(position.ticker)}/profile`).catch(() => null)
    )
  );
  renderClassifications(profiles.filter(Boolean));

  const messages = [];
  if (performance.coverage.unclassified_flow_events) {
    messages.push(`${performance.coverage.unclassified_flow_events} 筆事件無法判定屬於投資人資金或投資組合活動，其現金不計入任何一方，TWR 與 XIRR 因此建立在不完整的基礎上。`);
  }
  if (missing) messages.push(`${missing} 日缺少估值快照，績效區間會跨過這些日子。`);
  renderMessages(elements.qualityWarnings, messages);
}

/* Consolidation ------------------------------------------------------------ */

function renderConsolidated(summary) {
  const currency = summary.reporting_currency;
  elements.groupMissing.hidden = true;
  elements.groupContent.hidden = false;

  setMoneyValue(elements.cTotal, summary.total_value, currency);
  elements.cCurrency.textContent = `${summary.group_name} · ${summary.portfolio_ids.length} 個帳戶 · ${summary.as_of}`;
  setMoneyValue(elements.cSecurities, summary.securities_value, currency);
  setMoneyValue(elements.cCash, summary.cash_value, currency);
  elements.cCashDetail.textContent = summary.cash_by_currency
    .map((item) => `${item.currency} ${formatDecimal(item.local_amount, 2)}`)
    .join(" · ");
  elements.cCoverage.textContent = formatPercent(summary.converted_value_coverage_percent);
  elements.cCoverageDetail.textContent = summary.unconverted.length
    ? `${summary.unconverted.length} 筆金額無法換算`
    : "全部金額皆已換算";

  clear(elements.fxList);
  summary.fx_rates_used.forEach((rate) => {
    const row = element("li", "fx-row");
    row.append(element("span", "fx-row__pair", `${rate.base_currency}/${rate.quote_currency}`));
    row.append(element("span", "fx-row__rate", formatDecimal(rate.rate, 8)));
    row.append(element("span", "fx-row__meta", `${rate.method} · ${rate.conversion_path.join(" → ")}`));
    if (rate.price_as_of) row.append(element("span", "fx-row__meta", `匯率日期 ${rate.price_as_of.slice(0, 10)}`));
    if (rate.is_stale) row.append(badge("過期匯率", "badge--unknown"));
    elements.fxList.append(row);
  });

  elements.issuerPanel.hidden = summary.issuer_exposure.length === 0;
  clear(elements.issuerList);
  summary.issuer_exposure.forEach((issuer) => {
    const row = element("li", "issuer-row");
    const left = element("div");
    left.append(element("div", "issuer-row__name", issuer.issuer_name));
    left.append(element("div", "issuer-row__tickers", issuer.tickers.join(" · ")));
    const right = element("div", "issuer-row__value", `${formatMoney(issuer.reporting_value, currency)} · ${formatPercent(issuer.weight_percent)}`);
    row.append(left, right);
    elements.issuerList.append(row);
  });

  clear(elements.cPositionsBody);
  elements.cPositionsCount.textContent = `${summary.positions.length} 項持倉`;
  summary.positions.forEach((position) => {
    const row = document.createElement("tr");
    const instrument = element("div", "instrument");
    instrument.append(element("span", "instrument__ticker", position.ticker));
    instrument.append(element("span", "instrument__name", position.local_currency));
    row.append(createCell(instrument));
    row.append(createCell(position.portfolio_name));
    row.append(createCell(formatDecimal(position.quantity), "number"));
    row.append(createCell(position.local_market_value === null ? "—" : formatMoney(position.local_market_value, position.local_currency), "number"));
    // Null means the pair could not be resolved: say so rather than showing a zero.
    const converted = position.reporting_market_value === null
      ? element("span", "unconverted", "無法換算")
      : document.createTextNode(formatMoney(position.reporting_market_value, currency));
    row.append(createCell(converted, "number"));
    row.append(createCell(formatPercent(position.weight_percent), "number"));
    elements.cPositionsBody.append(row);
  });

  const messages = [...summary.warnings];
  summary.unconverted.forEach((item) => {
    messages.push(`${item.currency} ${formatDecimal(item.amount, 2)} 未計入總額：${item.reason}`);
  });
  renderMessages(elements.cWarnings, messages);
}

async function loadConsolidated() {
  if (!state.groupId) {
    const groups = await fetchJson("api/v1/portfolio-groups").catch(() => []);
    state.groupId = Array.isArray(groups) && groups.length ? groups[0].id : null;
  }
  if (!state.groupId) {
    elements.groupMissing.hidden = false;
    elements.groupContent.hidden = true;
    return;
  }
  renderConsolidated(await fetchJson(`api/v1/portfolio-groups/${encodeURIComponent(state.groupId)}/summary`));
}

elements.tabs.forEach((tab) => {
  tab.addEventListener("click", () => selectTab(tab.dataset.tab));
});

elements.select.addEventListener("change", () => {
  state.selectedId = elements.select.value;
  state.loaded = {};
  // Page 3 of one portfolio's journal says nothing about another's, and the expanded event
  // ids do not exist there at all.
  state.journalOffset = 0;
  state.journalExpanded.clear();
  loadSummary();
  if (state.tab !== "overview") loadTab(state.tab);
});
elements.refresh.addEventListener("click", loadSummary);

// Changing the range invalidates the performance/quality cache for this portfolio, since both
// read whatever window resolveHistoryRange() resolves against state.perfRangeMode.
function reloadRangedTabs() {
  delete state.loaded[`performance:${state.selectedId}`];
  delete state.loaded[`quality:${state.selectedId}`];
  if (state.tab === "performance" || state.tab === "quality") loadTab(state.tab);
  else updateRangePickerUI();
}

elements.rangeOptions.forEach((button) => {
  button.addEventListener("click", () => {
    state.perfRangeMode = button.dataset.range;
    if (state.perfRangeMode === "custom" && !state.perfCustomStart) {
      state.perfCustomStart = startOfYear();
      state.perfCustomEnd = today();
      elements.rangeStart.value = state.perfCustomStart;
      elements.rangeEnd.value = state.perfCustomEnd;
      updateRangePickerUI();
      return;
    }
    reloadRangedTabs();
  });
});

elements.rangeApply.addEventListener("click", () => {
  if (!elements.rangeStart.value || !elements.rangeEnd.value) return;
  state.perfRangeMode = "custom";
  state.perfCustomStart = elements.rangeStart.value;
  state.perfCustomEnd = elements.rangeEnd.value;
  reloadRangedTabs();
});

loadDashboard();

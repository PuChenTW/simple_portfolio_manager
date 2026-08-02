const state = {
  portfolios: [],
  selectedId: null,
  requestId: 0,
  tab: "overview",
  // Each tab loads on first view and is cached per portfolio, so switching back is instant
  // and a tab nobody opens costs nothing.
  loaded: {},
  groupId: null,
};

// The widest window the performance tab will look back over. The window actually used starts
// at the first stored snapshot, because a fixed 30-day window on a portfolio with two weeks of
// history would report a fortnight of "missing" dates that no work could ever fill.
const HISTORY_DAYS = 30;

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
  journalBody: document.querySelector("#journal-body"),
  journalCount: document.querySelector("#journal-count"),
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
  const reliable = performance.coverage.is_reliable;
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
  if (reliable && !messages.length) messages.push("此期間資料完整：無缺漏快照、無部分估值，且每筆事件的現金流都可分類。");
  renderMessages(elements.perfCoverage, messages);
  elements.perfCoverage.hidden = false;

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
// an artifact of asking for dates before the portfolio had any history.
async function resolveHistoryRange(id) {
  const wide = `start_date=${isoDaysAgo(HISTORY_DAYS)}&end_date=${today()}`;
  const probe = await fetchJson(`api/v1/portfolios/${id}/nav-history?${wide}`);
  if (!probe.snapshots.length) return { range: wide, history: probe };
  const start = probe.snapshots[0].valuation_date;
  const end = probe.snapshots[probe.snapshots.length - 1].valuation_date;
  const range = `start_date=${start}&end_date=${end}`;
  return { range, history: await fetchJson(`api/v1/portfolios/${id}/nav-history?${range}`) };
}

async function loadPerformance() {
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
    elements.classBody.append(row);
  });
  elements.qClassified.textContent = `${classified} / ${profiles.length}`;
}

// A badge that asks for action must be reserved for events where action is possible. Every event
// now enters through the journal, so the only open question left is a flow nobody can classify.
function statusBadge(event) {
  if (event.flow_classification === "unknown") return badge("待確認", "badge--unknown");
  return badge("已入帳", "badge--internal");
}

function renderJournal(page) {
  clear(elements.journalBody);
  elements.journalCount.textContent = `共 ${page.total} 筆，顯示最近 ${page.items.length} 筆`;
  page.items.forEach((event) => {
    const row = document.createElement("tr");
    row.append(createCell(formatTime(event.occurred_at)));
    row.append(createCell(event.event_type));
    const [label, className] = FLOW_LABELS[event.flow_classification] ?? ["—", "badge--none"];
    const flow = element("div", "instrument");
    flow.append(badge(label, className));
    row.append(createCell(flow));
    row.append(createCell(event.source_reference || event.source || "—"));
    row.append(createCell(statusBadge(event)));
    elements.journalBody.append(row);
  });
  elements.qEvents.textContent = String(page.total);
}

async function loadQuality() {
  const id = encodeURIComponent(state.selectedId);
  const { range, history } = await resolveHistoryRange(id);
  const [summary, journal, performance] = await Promise.all([
    fetchJson(`api/v1/portfolios/${id}/summary`),
    fetchJson(`api/v1/portfolios/${id}/transactions?limit=25`),
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
  loadSummary();
  if (state.tab !== "overview") loadTab(state.tab);
});
elements.refresh.addEventListener("click", loadSummary);
loadDashboard();

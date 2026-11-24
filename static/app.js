localStorage.removeItem("fraud_token");
const DEFAULT_SIMULATOR_TPS = 10;
const MAX_SIMULATOR_TPS = 1000;
const INTRO_DURATION_MS = 1450;
const INTRO_EXIT_MS = 240;
const savedSimulatorTps = Number(localStorage.getItem("fraud_simulator_tps"));
const initialSimulatorTps = Number.isInteger(savedSimulatorTps) && savedSimulatorTps >= 1 && savedSimulatorTps <= MAX_SIMULATOR_TPS
  ? savedSimulatorTps
  : DEFAULT_SIMULATOR_TPS;
const savedTestOffset = Number(localStorage.getItem("fraud_test_offset"));
const initialTestOffset = Number.isSafeInteger(savedTestOffset) && savedTestOffset >= 0 ? savedTestOffset : 0;
const state = {
  token: null,
  user: null,
  cases: [],
  classifications: [],
  manualReviewItems: [],
  manualReviewDrafts: new Map(),
  manualReviewPage: 0,
  manualReviewTotal: 0,
  manualReviewRequestId: 0,
  manualReviewPollTimer: null,
  caseRequestId: 0,
  classificationRequestId: 0,
  selected: null,
  detailRouteActive: false,
  view: "dashboard",
  dashboardPollTimer: null,
  dashboardLoading: false,
  dashboardRefreshPending: false,
  page: 0,
  pageSize: 50,
  total: 0,
  testOffset: initialTestOffset,
  simulatorRunning: false,
  simulatorEverStarted: false,
  simulatorTps: initialSimulatorTps,
  simulatorTpsInputDirty: false,
  simulatorVersion: 0,
  simulatorError: "",
  simulatorSyncError: "",
  simulatorPollTimer: null,
  simulatorPolling: false,
  simulatorHistory: [],
  lastSimulatorViewRefresh: 0,
  authMode: "login",
  faultLabEnabled: false,
  faultLabResult: null,
  faultLabError: "",
  faultLabPreviewTxnId: null,
  driftLoadedAt: 0,
  driftLoading: false,
};
const $ = (selector) => document.querySelector(selector);
let revealObserver = null;
const simulatorChannel = typeof BroadcastChannel === "function" ? new BroadcastChannel("fraud-copilot-simulator") : null;
const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;" })[char]);
const titleCase = (value) => String(value || "").replaceAll("_", " ").toLowerCase().replace(/\b\w/g, (letter) => letter.toUpperCase());
const riskClass = (risk) => String(risk || "").toLowerCase();
const formatProbability = (probability) => `${(Number(probability || 0) * 100).toLocaleString("en-US", { maximumFractionDigits: 2 })}%`;
const statusLabel = (status) => ({
  CONFIRMED_FRAUD: "Confirmed fraud",
  DISMISSED: "Dismissed / safe",
})[status] || titleCase(status);
const decisionLabel = (action) => ({
  OUTCOME_CONFIRMED_FRAUD: "Confirmed fraud",
  OUTCOME_DISMISSED: "Dismissed / safe",
  OUTCOME_INSUFFICIENT_EVIDENCE: "Insufficient evidence",
  ESCALATE: "Escalated to Risk Lead",
  NEEDS_MORE_INFO: "Needs more info",
  CONFIRM_SIMULATED: "Confirmed (demo)",
})[action] || titleCase(action);
const OUTCOME_ACTIONS = ["CONFIRMED_FRAUD", "DISMISSED", "INSUFFICIENT_EVIDENCE"];
const decisionClass = (action) => ({
  OUTCOME_CONFIRMED_FRAUD: "fraud",
  OUTCOME_DISMISSED: "safe",
  OUTCOME_INSUFFICIENT_EVIDENCE: "pending",
  ESCALATE: "escalated",
  NEEDS_MORE_INFO: "pending",
  CONFIRM_SIMULATED: "fraud",
})[action] || "pending";
const decisionSlices = [
  { action: "OUTCOME_CONFIRMED_FRAUD", label: "Confirmed fraud", color: "#d84c67" },
  { action: "OUTCOME_DISMISSED", label: "Dismissed as safe", color: "#15936b" },
  { action: "ESCALATE", label: "Escalated", color: "#d97706" },
  { action: "NEEDS_MORE_INFO", label: "More information needed", color: "#356ae6" },
  { action: "OUTCOME_INSUFFICIENT_EVIDENCE", label: "Insufficient evidence", color: "#7658d9" },
];
const riskGroups = [
  { risk: "HIGH", label: "High Risk" },
  { risk: "MEDIUM", label: "Medium Risk" },
  { risk: "LOW", label: "Low Risk" },
];
const SIMULATOR_CHART_WINDOW_SECONDS = 300;
const decisionSummaryFields = [
  ["decision-confirmed-fraud", "confirmed_fraud"],
  ["decision-dismissed", "dismissed"],
  ["decision-insufficient-evidence", "insufficient_evidence"],
  ["decision-escalated", "escalated"],
  ["decision-needs-more-info", "needs_more_info"],
];
const decisionSummaryFrames = new WeakMap();
const showToast = (message) => { const toast = $("#toast"); toast.textContent = message; toast.classList.add("show"); setTimeout(() => toast.classList.remove("show"), 2800); };
function animateDecisionCount(element, target) {
  const previousFrame = decisionSummaryFrames.get(element);
  if (previousFrame) cancelAnimationFrame(previousFrame);

  const parsedCurrent = Number(element.textContent.trim().replace(/,/g, ""));
  const current = Number.isFinite(parsedCurrent) ? parsedCurrent : 0;
  if (current === target || window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
    element.textContent = String(target);
    return;
  }

  const startedAt = performance.now();
  const duration = 550;
  const update = (now) => {
    const progress = Math.min((now - startedAt) / duration, 1);
    const eased = 1 - Math.pow(1 - progress, 3);
    element.textContent = String(Math.round(current + (target - current) * eased));
    if (progress < 1) {
      decisionSummaryFrames.set(element, requestAnimationFrame(update));
    } else {
      element.textContent = String(target);
      decisionSummaryFrames.delete(element);
    }
  };
  decisionSummaryFrames.set(element, requestAnimationFrame(update));
}
function renderDecisionSummary(decisions = {}) {
  for (const [id, field] of decisionSummaryFields) {
    const value = Number(decisions[field] ?? 0);
    animateDecisionCount($(`#${id}`), Number.isFinite(value) ? value : 0);
  }
}

async function api(path, options = {}) {
  const response = await fetch(`/api/v1${path}`, { ...options, headers: { "Content-Type": "application/json", Authorization: `Bearer ${state.token}`, ...(options.headers || {}) } });
  if (response.status === 401) {
    localStorage.removeItem("fraud_token");
    state.token = null;
    showLogin();
    showToast("Phiên đăng nhập hết hạn, vui lòng đăng nhập lại");
  }
  if (!response.ok) { const body = await response.json().catch(() => ({})); throw new Error(body.detail || `Request failed (${response.status})`); }
  return response.status === 204 ? null : response.json();
}

async function loadFaultLabConfig() {
  try {
    const response = await fetch("/api/v1/fault-lab/config");
    const config = await response.json();
    if (!response.ok) throw new Error(config.detail || `Request failed (${response.status})`);
    state.faultLabEnabled = config.enabled === true;
  } catch (error) {
    state.faultLabEnabled = false;
    showToast(`Could not load Fault Lab configuration: ${error.message}`);
  }
  $("#fault-lab-nav").classList.toggle("hidden", !state.faultLabEnabled);
}

function showApp() {
  $("#intro-view").classList.add("hidden");
  $("#login-view").classList.add("hidden");
  const app = $("#app-view");
  app.classList.remove("hidden");
  app.classList.remove("app-entering");
  void app.offsetWidth;
  app.classList.add("app-entering");
  animateCurrentView();
}
function animateCurrentView() {
  $("#app-view").classList.toggle("dashboard-theme", state.view === "dashboard");
  if (revealObserver) {
    revealObserver.disconnect();
    revealObserver = null;
  }
  const views = {
    dashboard: ["#dashboard-content", ".dashboard-intro, .dashboard-kpi, .dashboard-panel"],
    review: ["#review-content", "#review-kpi-grid > .kpi-card, .section-heading, .queue-card"],
    analytics: ["#review-content", "#analytics-kpi-grid > .kpi-card, .section-heading, .queue-card, .decision-summary-card"],
    classifications: ["#classifications-content", ".section-heading, .queue-card"],
    "manual-review": ["#manual-review-content", ".section-heading"],
    "fault-lab": ["#fault-lab-content", ".section-heading, .queue-card"],
  };
  const [selector, revealSelector] = views[state.view];
  const section = $(selector);
  document.querySelectorAll(".page-transition, .page-reveal, .page-pending").forEach((element) => {
    element.classList.remove("page-transition", "page-reveal", "page-pending");
    element.style.removeProperty("--reveal-delay");
  });
  const prefersReducedMotion = window.matchMedia
    && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const dashboardVideo = $(".dashboard-background-video");
  if (dashboardVideo) {
    if (state.view === "dashboard" && !prefersReducedMotion) {
      const playback = dashboardVideo.play();
      if (playback) playback.catch((error) => console.warn("Dashboard background video could not play:", error));
    } else {
      dashboardVideo.pause();
    }
  }
  if (!section || prefersReducedMotion) return;

  const revealItems = [...section.querySelectorAll(revealSelector)];
  const summaryRevealItems = state.view === "dashboard"
    ? []
    : [...document.querySelectorAll("#analytics-decisions .decision-summary-card")];
  void section.offsetWidth;
  section.classList.add("page-transition");
  if (!("IntersectionObserver" in window)) {
    [...revealItems, ...summaryRevealItems].forEach((element, index) => {
      element.style.setProperty("--reveal-delay", `${index % 4 * 90}ms`);
      element.classList.add("page-reveal");
    });
    return;
  }
  revealItems.forEach((element) => element.classList.add("page-pending"));
  summaryRevealItems.forEach((element, index) => {
    element.style.setProperty("--reveal-delay", `${index % 4 * 90}ms`);
    element.classList.add("page-reveal");
  });
  revealObserver = new IntersectionObserver((entries, observer) => {
    entries.forEach((entry) => {
      if (!entry.isIntersecting) return;
      const index = revealItems.indexOf(entry.target);
      entry.target.classList.remove("page-pending");
      entry.target.style.setProperty("--reveal-delay", `${index % 4 * 90}ms`);
      entry.target.classList.add("page-reveal");
      observer.unobserve(entry.target);
    });
  }, { threshold: 0.08, rootMargin: "0px 0px -48px 0px" });
  revealItems.forEach((element) => revealObserver.observe(element));
}
function startDashboardPolling() {
  if (state.dashboardPollTimer !== null) return;
  state.dashboardPollTimer = setInterval(() => {
    if (state.token && state.view === "dashboard") void loadDashboard();
  }, 10000);
}
function startSimulatorPolling() {
  if (state.simulatorPollTimer !== null) return;
  state.simulatorPollTimer = setInterval(() => {
    void syncSimulatorState().catch((error) => {
      if (state.simulatorSyncError !== error.message) showToast(error.message);
      state.simulatorSyncError = error.message;
    });
  }, 1000);
}
function showIntro() {
  const intro = $("#intro-view");
  intro.classList.remove("hidden", "intro-exiting");
  $("#login-view").classList.add("hidden");
  $("#app-view").classList.add("hidden");
  setTimeout(() => {
    if (intro.classList.contains("hidden")) return;
    intro.classList.add("intro-exiting");
    setTimeout(() => {
      if (!intro.classList.contains("hidden")) showLogin();
    }, INTRO_EXIT_MS);
  }, INTRO_DURATION_MS);
}
function showLogin() {
  const intro = $("#intro-view");
  intro.classList.add("hidden");
  intro.classList.remove("intro-exiting");
  const profileDialog = $("#profile-dialog");
  if (profileDialog.open) profileDialog.close();
  const dashboardVideo = $(".dashboard-background-video");
  if (dashboardVideo) dashboardVideo.pause();
  const login = $("#login-view");
  login.classList.remove("hidden", "login-entering");
  void login.offsetWidth;
  login.classList.add("login-entering");
  const app = $("#app-view");
  app.classList.add("hidden");
  app.classList.remove("app-entering");
  document.querySelectorAll(".page-transition, .page-reveal").forEach((element) => {
    element.classList.remove("page-transition", "page-reveal");
    element.style.removeProperty("--reveal-delay");
  });
  if (state.simulatorPollTimer !== null) clearInterval(state.simulatorPollTimer);
  state.simulatorPollTimer = null;
  if (state.dashboardPollTimer !== null) clearInterval(state.dashboardPollTimer);
  state.dashboardPollTimer = null;
}
function renderBrief(brief, riskLevel) {
  if (!brief) return riskLevel === "LOW"
    ? "Low-risk cases do not generate briefs automatically. You can still review and add notes."
    : "Brief is being generated or is not yet available.";
  if (typeof brief === "string") return "No case brief available.";
  const checklist = (brief.checklist || []).map((item) =>
    `<li><p class="brief-facts">${escapeHtml(item.action || "")}${item.rule_code ? ` <small>(${escapeHtml(item.rule_code)})</small>` : ""}</p>${item.rationale ? `<p class="brief-rationale">${escapeHtml(item.rationale)}</p>` : ""}</li>`
  ).join("");
  const supportingFacts = (brief.checklist || []).some((item) => item.rationale)
    ? (brief.key_signals || []).map((fact) => `<li>${escapeHtml(fact)}</li>`).join("") : "";
  return (brief.source === "rule_based" ? `<p class="brief-fallback-label">Rule-based fallback</p>` : "")
    + (checklist ? `<ol class="investigation-checklist">${checklist}</ol>` : `<p>No case brief available.</p>`)
    + (supportingFacts ? `<details class="brief-supporting-facts"><summary>Supporting case facts</summary><ul>${supportingFacts}</ul></details>` : "");
}
function renderEvidence(item) {
  if (!item.brief_status) return `<span class="evidence muted-label">No brief</span>`;
  const label = item.brief_status === "READY" ? "Brief ready" : titleCase(item.brief_status);
  return `<span class="evidence evidence-${riskClass(item.brief_status)}">${escapeHtml(label)}</span>`;
}
function renderDecisionBreakdown(rows) {
  const counts = new Map((rows || []).map((row) => [`${row.risk_level}:${row.action}`, Number(row.decision_count) || 0]));
  $("#dashboard-decision-breakdown").innerHTML = riskGroups.map(({ risk, label }) => {
    const values = decisionSlices.map((slice) => counts.get(`${risk}:${slice.action}`) || 0);
    const total = values.reduce((sum, value) => sum + value, 0);
    let accumulated = 0;
    const segments = values.map((value, index) => {
      const start = accumulated;
      accumulated += total ? value / total * 100 : 0;
      return `${decisionSlices[index].color} ${start}% ${accumulated}%`;
    });
    const gradient = total ? `conic-gradient(${segments.join(", ")})` : "#edf1f6";
    const legend = decisionSlices.map((slice, index) => {
      const percentage = total ? values[index] / total * 100 : 0;
      return `<li><i class="legend-swatch" style="background:${slice.color}"></i><span>${slice.label}</span><b>${percentage.toLocaleString("en-US", { maximumFractionDigits: 1 })}%</b></li>`;
    }).join("");
    return `<article class="risk-breakdown-card"><h4>${label}</h4><p>Decisions by predicted risk level</p><div class="risk-breakdown-content"><div class="decision-donut" style="background:${gradient}" role="img" aria-label="${label}: ${total} decisions"><span class="decision-donut-center">${total.toLocaleString("en-US")}<small>decisions</small></span></div><ul class="decision-legend">${legend}</ul></div></article>`;
  }).join("");
}
function validationIssueLabel(message) {
  const normalized = String(message || "").toLowerCase();
  if (normalized.includes("field required") || normalized.includes("missing")) return "Missing value";
  if (/greater.?than|less.?than|out of range/.test(normalized)) return "Out of range";
  if (normalized.includes("valid") || normalized.includes("input should")) return "Invalid format";
  return "Invalid value";
}
function renderSignalHealth(data = {}) {
  const featureNames = {
    amt: "Transaction Amount",
    transaction_hour: "Transaction Hour",
    hour_of_day: "Transaction Hour",
    category: "Transaction Category",
    sum_amt_24h: "Spend Total · 24h",
    avg_amt_7d: "Average Spend · 7 days",
    trans_count_24h: "Transaction Count · 24h",
    trans_count_7d: "Transaction Count · 7 days",
    customer_merchant_distance_km: "Distance to Merchant",
  };
  const validationFieldNames = {
    amt: "Transaction Amount",
  };
  const formatter = new Intl.NumberFormat("en-US");
  const features = data.top_features || [];
  $("#dashboard-top-features").innerHTML = features.length
    ? features.map((item, index) => {
      const impact = Number(item.average_impact);
      const impactLabel = Number.isFinite(impact)
        ? impact.toLocaleString("en-US", { maximumFractionDigits: 3 })
        : "—";
      const label = featureNames[item.feature] || titleCase(item.feature || "Model feature");
      return `<article class="signal-feature-card">
        <span class="signal-rank">${String(index + 1).padStart(2, "0")}</span>
        <div class="signal-feature-copy"><b>${escapeHtml(label)}</b><small>Average impact · ${impactLabel}</small></div>
        <span class="signal-feature-count">${formatter.format(Number(item.occurrences) || 0)}<small>occurrences</small></span>
      </article>`;
    }).join("")
    : `<p class="signal-health-empty">No model signals to summarize in the last 24 hours.</p>`;

  const issues = data.validation_issues || [];
  $("#dashboard-validation-issues").innerHTML = issues.length
    ? issues.map((item) => `<article class="signal-issue-card">
        <span class="signal-issue-mark" aria-hidden="true">!</span>
        <div class="signal-issue-copy"><b>${escapeHtml(validationFieldNames[item.field] || titleCase(item.field || "Data field"))}</b><small>${validationIssueLabel(item.message)}</small></div>
        <strong>${formatter.format(Number(item.occurrences) || 0)}</strong>
      </article>`).join("")
    : `<p class="signal-health-empty">No input validation issues in the last 24 hours.</p>`;

  const deadQueueLabels = {
    ml_jobs: "Scoring",
    case_events: "Case updates",
    llm_jobs: "Brief generation",
  };
  const deadQueues = data.dead_letters || [];
  const deadCount = deadQueues.reduce((total, item) => total + (Number(item.count) || 0), 0);
  const unscored = Number(data.unscored_last_24h) || 0;
  $("#dashboard-queue-health").innerHTML = `
    <div class="signal-queue-total"><div><small>Unscored · last 24h</small><strong>${formatter.format(unscored)}</strong></div>
      <span class="${deadCount ? "queue-attention" : "queue-healthy"}">${deadCount ? `${formatter.format(deadCount)} jobs need attention` : "Queues clear"}</span>
    </div>
    <div class="signal-queue-breakdown">${deadQueues.map((item) => {
      const label = deadQueueLabels[item.queue] || titleCase(item.queue);
      return `<div><span>${escapeHtml(label)}</span><b>${formatter.format(Number(item.count) || 0)}</b></div>`;
    }).join("")}</div>`;
}
function renderSimulatorChart() {
  const host = $("#dashboard-simulator-chart");
  if (!state.simulatorHistory.length) {
    host.innerHTML = `<p class="chart-empty">No simulator data is available yet.</p>`;
    return;
  }
  const width = 760;
  const height = 230;
  const left = 54;
  const right = 720;
  const top = 18;
  const bottom = 178;
  const plotWidth = right - left;
  const plotHeight = bottom - top;
  const now = Date.now();
  const windowStart = now - SIMULATOR_CHART_WINDOW_SECONDS * 1000;
  const rows = state.simulatorHistory.filter((sample) => sample.timestamp >= windowStart);
  if (!rows.length) {
    host.innerHTML = `<p class="chart-empty">Waiting for simulator data...</p>`;
    return;
  }
  const start = Math.max(windowStart, rows[0].timestamp);
  const duration = Math.max(1000, now - start);
  const maxRate = Math.max(1, ...rows.map((sample) => sample.total));
  const getX = (sample) => left + Math.max(0, Math.min(1, (sample.timestamp - start) / duration)) * plotWidth;
  const getY = (value) => bottom - Math.max(0, Math.min(1, value / maxRate)) * plotHeight;
  const grid = Array.from({ length: 5 }, (_, index) => {
    const y = top + plotHeight * index / 4;
    const rate = (maxRate * (4 - index) / 4).toLocaleString("en-US", { maximumFractionDigits: 1 });
    return `<line class="simulator-chart-gridline" x1="${left}" y1="${y}" x2="${right}" y2="${y}"></line><text class="simulator-chart-axis" x="${left - 8}" y="${y + 3}" text-anchor="end">${rate}</text>`;
  }).join("");
  const points = rows.map((sample) => ({ x: getX(sample), y: getY(sample.total) }));
  const totalPath = points.reduce((path, point, index) => {
    if (index === 0) return `M${point.x.toFixed(1)},${point.y.toFixed(1)}`;
    const previous = points[index - 1];
    const beforePrevious = points[Math.max(0, index - 2)];
    const next = points[Math.min(points.length - 1, index + 1)];
    const control1X = previous.x + (point.x - beforePrevious.x) / 6;
    const control1Y = previous.y + (point.y - beforePrevious.y) / 6;
    const control2X = point.x - (next.x - previous.x) / 6;
    const control2Y = point.y - (next.y - previous.y) / 6;
    return `${path} C${control1X.toFixed(1)},${control1Y.toFixed(1)} ${control2X.toFixed(1)},${control2Y.toFixed(1)} ${point.x.toFixed(1)},${point.y.toFixed(1)}`;
  }, "");
  const areaPath = `${totalPath} L${right},${bottom} L${left},${bottom} Z`;
  const timeLabels = Array.from({ length: 5 }, (_, index) => {
    const timestamp = new Date(start + duration * index / 4);
    const secondsAgo = duration * (4 - index) / 4000;
    const label = index === 4
      ? "Now"
      : duration < 60_000
        ? `${secondsAgo.toLocaleString("en-US", { maximumFractionDigits: 1 })}s ago`
        : timestamp.toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
    const x = left + plotWidth * index / 4;
    return `<text class="simulator-chart-axis" x="${x}" y="${bottom + 20}" text-anchor="${index === 0 ? "start" : index === 4 ? "end" : "middle"}">${label}</text>`;
  }).join("");
  const hitTargets = rows.map((sample, index) => {
    const x = getX(sample).toFixed(1);
    const y = getY(sample.total).toFixed(1);
    const rate = sample.total.toLocaleString("en-US", { maximumFractionDigits: 1 });
    const time = new Date(sample.timestamp).toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
    return `<circle class="simulator-chart-point" data-index="${index}" cx="${x}" cy="${y}" r="9" tabindex="0" role="button" aria-label="${rate} cases per second at ${time}"></circle><circle class="simulator-chart-active-point" data-index="${index}" cx="${x}" cy="${y}" r="0"></circle>`;
  }).join("");
  host.innerHTML = `<svg viewBox="0 0 ${width} ${height}" role="group" aria-label="Interactive total cases per second chart over the last 5 minutes"><defs><linearGradient id="simulator-total-gradient" x1="0" x2="1"><stop offset="0%" stop-color="#d94f67"></stop><stop offset="100%" stop-color="#f5f5f6"></stop></linearGradient><linearGradient id="simulator-area-gradient" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="#d94f67" stop-opacity=".16"></stop><stop offset="100%" stop-color="#d94f67" stop-opacity="0"></stop></linearGradient></defs><text class="simulator-chart-axis-title" x="${left}" y="11">Cases / sec</text>${grid}<path class="simulator-chart-total-fill" d="${areaPath}"></path><path class="simulator-chart-total" d="${totalPath}"></path>${hitTargets}${timeLabels}</svg><div class="simulator-chart-tooltip" role="tooltip" aria-live="polite"></div>`;
  const svg = host.querySelector("svg");
  const tooltip = host.querySelector(".simulator-chart-tooltip");
  const markers = svg.querySelectorAll(".simulator-chart-active-point");
  const slices = [
    { key: "low", label: "Low", color: "#22a06b" },
    { key: "medium", label: "Medium", color: "#e5ad2f" },
    { key: "high", label: "High", color: "#e5484d" },
  ];
  const hideTooltip = () => {
    tooltip.classList.remove("visible");
    markers.forEach((marker) => marker.setAttribute("r", "0"));
  };
  const showTooltip = (index) => {
    const sample = rows[index];
    const values = slices.map((slice) => Math.max(0, Number(sample[slice.key]) || 0));
    const riskTotal = values.reduce((sum, value) => sum + value, 0);
    const segments = [];
    let accumulated = 0;
    values.forEach((value, sliceIndex) => {
        if (!riskTotal || !value) return;
        const startAngle = accumulated / riskTotal * 100;
        accumulated += value;
        const endAngle = accumulated / riskTotal * 100;
        segments.push(`${slices[sliceIndex].color} ${startAngle}% ${endAngle}%`);
    });
    const pie = segments.length ? `conic-gradient(${segments.join(", ")})` : "#edf1f6";
    const legend = slices.map((slice, sliceIndex) => {
        const percentage = riskTotal ? values[sliceIndex] / riskTotal * 100 : 0;
        const value = values[sliceIndex].toLocaleString("en-US", { minimumFractionDigits: 1, maximumFractionDigits: 1 });
        return `<li><span><i style="background:${slice.color}"></i>${slice.label}</span><strong>${value}<small>${percentage.toLocaleString("en-US", { maximumFractionDigits: 1 })}%</small></strong></li>`;
    }).join("");
    const total = sample.total.toLocaleString("en-US", { minimumFractionDigits: 1, maximumFractionDigits: 1 });
    const time = new Date(sample.timestamp).toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
    tooltip.innerHTML = `<div class="simulator-tooltip-pie" style="background:${pie}" role="img" aria-label="Low ${values[0]}, Medium ${values[1]}, High ${values[2]} cases per second"></div><div class="simulator-tooltip-details"><span class="simulator-tooltip-time">${time}</span><strong>${total}<small>cases / sec</small></strong><ul>${legend}</ul></div>`;
    tooltip.classList.add("visible");
    markers.forEach((marker, markerIndex) => marker.setAttribute("r", markerIndex === index ? "4" : "0"));
    const svgRect = svg.getBoundingClientRect();
    const hostRect = host.getBoundingClientRect();
    const scaleX = svgRect.width / width;
    const scaleY = svgRect.height / height;
    const x = svgRect.left - hostRect.left + host.scrollLeft + points[index].x * scaleX;
    const y = points[index].y * scaleY;
    const tooltipWidth = tooltip.offsetWidth;
    const tooltipHeight = tooltip.offsetHeight;
    const minLeft = host.scrollLeft + 8;
    const maxLeft = Math.max(minLeft, host.scrollLeft + host.clientWidth - tooltipWidth - 8);
    const maxTop = Math.max(8, host.clientHeight - tooltipHeight - 8);
    tooltip.style.left = `${Math.max(minLeft, Math.min(maxLeft, x - tooltipWidth / 2))}px`;
    tooltip.style.top = `${Math.max(8, Math.min(maxTop, y > tooltipHeight + 22 ? y - tooltipHeight - 14 : y + 14))}px`;
  };
  svg.querySelectorAll(".simulator-chart-point").forEach((marker) => {
    const index = Number(marker.dataset.index);
    marker.addEventListener("pointerenter", () => showTooltip(index));
    marker.addEventListener("pointerleave", hideTooltip);
    marker.addEventListener("focus", () => showTooltip(index));
    marker.addEventListener("click", () => showTooltip(index));
  });
  svg.addEventListener("pointerleave", hideTooltip);
  svg.addEventListener("focusout", (event) => {
    if (!svg.contains(event.relatedTarget)) hideTooltip();
  });
}
function renderMlOperationsChart(metrics) {
  const host = $("#dashboard-ml-operations-chart");
  const latency = (metrics.latency_p95_ms || []).filter((point) => Number.isFinite(Number(point.timestamp)) && Number.isFinite(Number(point.value)));
  const throughput = (metrics.throughput_rps || []).filter((point) => Number.isFinite(Number(point.timestamp)) && Number.isFinite(Number(point.value)));
  if (!latency.length && !throughput.length) {
    host.innerHTML = `<p class="chart-empty">No inference data in the last ${Number(metrics.window_minutes) || 60} minutes.</p>`;
    return;
  }

  const width = 760;
  const height = 270;
  const left = 58;
  const right = 702;
  const top = 24;
  const bottom = 212;
  const plotWidth = right - left;
  const plotHeight = bottom - top;
  const end = Date.now() / 1000;
  const start = end - (Number(metrics.window_minutes) || 60) * 60;
  const maxLatency = Math.max(1, ...latency.map((point) => Number(point.value)));
  const maxThroughput = Math.max(1, ...throughput.map((point) => Number(point.value)));
  const makePath = (points, maxValue) => points.map((point, index) => {
    const x = left + Math.max(0, Math.min(1, (Number(point.timestamp) - start) / (end - start))) * plotWidth;
    const y = bottom - Math.max(0, Math.min(1, Number(point.value) / maxValue)) * plotHeight;
    return `${index ? "L" : "M"}${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(" ");
  const grid = Array.from({ length: 5 }, (_, index) => {
    const y = top + plotHeight * index / 4;
    const latencyTick = (maxLatency * (4 - index) / 4).toLocaleString("vi-VN", { maximumFractionDigits: 1 });
    const throughputTick = (maxThroughput * (4 - index) / 4).toLocaleString("vi-VN", { maximumFractionDigits: 1 });
    return `<line class="chart-gridline" x1="${left}" y1="${y}" x2="${right}" y2="${y}"></line><text class="chart-axis-label" x="${left - 8}" y="${y + 3}" text-anchor="end">${latencyTick}</text><text class="chart-axis-label" x="${right + 8}" y="${y + 3}">${throughputTick}</text>`;
  }).join("");
  const timeLabels = Array.from({ length: 5 }, (_, index) => {
    const x = left + plotWidth * index / 4;
    const timestamp = new Date((start + (end - start) * index / 4) * 1000);
    const label = index === 4 ? "Now" : timestamp.toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit" });
    return `<text class="chart-axis-label" x="${x}" y="${bottom + 22}" text-anchor="middle">${label}</text>`;
  }).join("");
  host.innerHTML = `<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="P95 inference latency and throughput over the last ${Number(metrics.window_minutes) || 60} minutes"><text class="chart-axis-title chart-latency" x="${left}" y="12">Latency (ms)</text><text class="chart-axis-title chart-throughput" x="${right}" y="12" text-anchor="end">Requests / sec</text>${grid}<path class="chart-line chart-latency" d="${latency.length ? makePath(latency, maxLatency) : ""}"></path><path class="chart-line chart-throughput" d="${throughput.length ? makePath(throughput, maxThroughput) : ""}"></path>${timeLabels}<g transform="translate(255 ${height - 8})"><circle class="chart-latency" r="4"></circle><text class="chart-legend" x="9" y="4">P95 inference latency</text><circle class="chart-throughput" cx="178" r="4"></circle><text class="chart-legend" x="187" y="4">Throughput</text></g></svg>`;
}
async function loadMlOperations() {
  try {
    const metrics = await api("/cases/analytics/ml-operations?window_minutes=60");
    if (state.view === "dashboard") renderMlOperationsChart(metrics);
  } catch (error) {
    if (state.view === "dashboard") {
      $("#dashboard-ml-operations-chart").innerHTML = `<p class="chart-empty">Unable to load model metrics: ${escapeHtml(error.message)}</p>`;
      showToast(error.message);
    }
  }
}
const DRIFT_REFRESH_MS = 60000;
const DRIFT_STATUS_LABELS = {
  stable: "Stable",
  moderate: "Moderate drift",
  significant: "Significant drift",
  insufficient_data: "Not enough data",
};
function driftPercent(value, digits = 2) {
  const number = Number(value);
  return value === null || value === undefined || !Number.isFinite(number)
    ? "—"
    : `${(number * 100).toLocaleString("en-US", { maximumFractionDigits: digits })}%`;
}
function driftPill(status) {
  const key = DRIFT_STATUS_LABELS[status] ? status : "insufficient_data";
  return `<span class="drift-pill drift-${key}">${DRIFT_STATUS_LABELS[key]}</span>`;
}
function renderDrift(report) {
  const formatter = new Intl.NumberFormat("en-US");
  const status = report.overall_status || "insufficient_data";
  const badge = $("#dashboard-drift-status");
  badge.className = `drift-status drift-${DRIFT_STATUS_LABELS[status] ? status : "insufficient_data"}`;
  badge.textContent = DRIFT_STATUS_LABELS[status] || DRIFT_STATUS_LABELS.insufficient_data;
  const scope = report.window || {};
  const minRows = Number(report.thresholds?.min_rows) || 200;
  if (!Number(scope.scored_rows)) {
    $("#dashboard-drift").innerHTML = `<p class="dashboard-empty">No scored transactions yet. Drift is reported once at least ${formatter.format(minRows)} transactions are scored.</p>`;
    return;
  }
  const score = report.score || {};
  const review = report.review_rate || {};
  const quality = report.data_quality || {};
  const psi = (value) => (value === null || value === undefined ? "—" : Number(value).toLocaleString("en-US", { maximumFractionDigits: 3 }));
  const shift = (item) => item?.top_shift
    ? `Largest change: ${escapeHtml(item.top_shift.bin)} · ${driftPercent(item.top_shift.expected, 1)} → ${driftPercent(item.top_shift.actual, 1)}`
    : "";
  const features = [...(report.features || [])]
    .sort((left, right) => (Number(right.psi) || 0) - (Number(left.psi) || 0))
    .slice(0, 8)
    .map((item) => {
    const width = Math.min(100, (Number(item.psi) || 0) / 0.5 * 100);
    return `<article class="drift-feature">
      <div class="drift-feature-copy"><b>${escapeHtml(item.label || item.feature)}</b><small>${shift(item)}${Number(item.missing_rate) > 0 ? ` · missing ${driftPercent(item.missing_rate, 1)}` : ""}</small></div>
      <span class="drift-bar" aria-hidden="true"><i class="drift-${escapeHtml(item.status)}" style="width:${width.toFixed(1)}%"></i></span>
      <strong title="Population Stability Index">${psi(item.psi)}</strong>${driftPill(item.status)}
    </article>`;
  }).join("");
  $("#dashboard-drift").innerHTML = `
    <div class="drift-summary">
      <div><small>Score distribution (PSI)</small><strong>${psi(score.psi)}</strong>${driftPill(score.status)}</div>
      <div><small>Review rate · expected → live</small><strong>${driftPercent(review.expected_rate)} → ${driftPercent(review.actual_rate)}</strong><span>Medium + High share of scored traffic</span></div>
      <div><small>Unscored inputs</small><strong>${driftPercent(quality.unscored_rate)}</strong><span>${formatter.format(Number(quality.unscored) || 0)} of ${formatter.format(Number(quality.rows) || 0)} recent transactions</span></div>
      <div><small>Window</small><strong>${formatter.format(Number(scope.scored_rows) || 0)}</strong><span>scored rows · model ${escapeHtml(report.model_version || "—")}</span></div>
    </div>
    <div class="drift-feature-list">${features || `<p class="dashboard-empty">No monitored features.</p>`}</div>
    <p class="drift-footnote">PSI &lt; 0.10 stable · 0.10–0.25 moderate · ≥ 0.25 significant. Baseline: ${escapeHtml(report.baseline?.reference || "validation")} split (${formatter.format(Number(report.baseline?.rows) || 0)} rows). Calendar fields are not monitored because a short window always covers only a few days.</p>`;
}
async function loadDrift(force = false) {
  if (state.driftLoading || (!force && Date.now() - state.driftLoadedAt < DRIFT_REFRESH_MS)) return;
  state.driftLoading = true;
  try {
    const report = await api("/monitoring/drift");
    state.driftLoadedAt = Date.now();
    if (state.view === "dashboard") renderDrift(report);
  } catch (error) {
    state.driftLoadedAt = Date.now();
    if (state.view === "dashboard") {
      $("#dashboard-drift-status").className = "drift-status drift-insufficient_data";
      $("#dashboard-drift-status").textContent = "Unavailable";
      $("#dashboard-drift").innerHTML = `<p class="dashboard-empty">Drift monitoring unavailable: ${escapeHtml(error.message)}</p>`;
    }
  } finally {
    state.driftLoading = false;
  }
}
function renderDashboard(data) {
  const summary = data.summary || {};
  const total = Number(summary.total_scored) || 0;
  const high = Number(summary.high_open) || 0;
  const medium = Number(summary.medium_open) || 0;
  const formatter = new Intl.NumberFormat("en-US");
  $("#dashboard-total").textContent = formatter.format(total);
  $("#dashboard-total-subtitle").textContent = `${formatter.format(Number(summary.scored_last_hour) || 0)} transactions in the last 60 minutes`;
  $("#dashboard-high").textContent = formatter.format(high);
  $("#dashboard-high-subtitle").textContent = `${formatter.format(Number(summary.high_open_last_hour) || 0)} new cases in the last 60 minutes`;
  $("#dashboard-medium").textContent = formatter.format(medium);
  $("#dashboard-medium-subtitle").textContent = `${formatter.format(Number(summary.unscored_total) || 0)} unscored transactions`;
  $("#dashboard-review-rate").textContent = total
    ? `${((high + medium) / total * 100).toLocaleString("en-US", { maximumFractionDigits: 2 })}%`
    : "—";
  const latency = Number(summary.latency_p95_ms);
  $("#dashboard-latency").textContent = total && Number.isFinite(latency)
    ? `${latency.toLocaleString("en-US", { maximumFractionDigits: 0 })} ms`
    : "—";
  const reviewCount = Number(summary.open_count ?? high + medium) || 0;
  const navCount = $("#review-nav-count");
  navCount.textContent = formatter.format(reviewCount);
  navCount.classList.toggle("hidden", reviewCount === 0);
  renderSimulatorChart();
  renderDecisionBreakdown(data.decision_breakdown || []);
  renderSignalHealth(data.signal_health || {});
  void loadMlOperations();
  void loadDrift(!$("#dashboard-drift .drift-summary"));

  const cases = data.recent_cases || [];
  $("#dashboard-recent-cases").innerHTML = cases.length
    ? cases.map((item) => {
      const probability = Number(item.calibrated_prob);
      const amount = item.amount !== null && item.amount !== undefined && item.amount !== ""
        ? Number(item.amount).toLocaleString("en-US", { maximumFractionDigits: 2 })
        : "Unknown amount";
      const confidence = Number.isFinite(probability) ? Math.min(100, Math.max(0, probability * 100)) : 0;
      const riskLabel = item.risk_level === "HIGH" ? "High risk" : item.risk_level === "MEDIUM" ? "Medium risk" : "Low risk";
      return `<button class="dashboard-case-row" type="button" data-dashboard-case-id="${escapeHtml(item.case_id)}"><span class="dashboard-case-risk risk-${riskClass(item.risk_level)}">${riskLabel}</span><span class="dashboard-case-main"><b>${escapeHtml(item.merchant || "Merchant chưa rõ")}</b><small>${escapeHtml(amount)} · ${escapeHtml(String(item.txn_id || "").slice(0, 12))}…</small><span class="dashboard-case-progress"><i style="width:${confidence}%"></i></span></span><span class="dashboard-case-probability">${formatProbability(probability)}</span><span class="dashboard-case-arrow">⌄</span></button>`;
    }).join("")
    : `<p class="dashboard-empty">There are no cases awaiting review.</p>`;
  document.querySelectorAll("[data-dashboard-case-id]").forEach((row) =>
    row.addEventListener("click", () => openDetail(row.dataset.dashboardCaseId))
  );
  $("#dashboard-updated").textContent = `Updated at ${new Date().toLocaleTimeString("en-US")} · Refreshes every 10 seconds`;
}
async function loadDashboard() {
  if (state.dashboardLoading) {
    state.dashboardRefreshPending = true;
    return;
  }
  state.dashboardLoading = true;
  try {
    const data = await api("/cases/analytics/dashboard");
    if (state.view === "dashboard") renderDashboard(data);
  } catch (error) {
    if (state.view === "dashboard") {
      $("#dashboard-decision-breakdown").innerHTML = `<p class="dashboard-empty">Unable to load overview data: ${escapeHtml(error.message)}</p>`;
      $("#dashboard-signal-health").innerHTML = `<p class="dashboard-empty">Unable to load pipeline insights: ${escapeHtml(error.message)}</p>`;
      showToast(error.message);
    }
  } finally {
    state.dashboardLoading = false;
    const refreshPending = state.dashboardRefreshPending;
    state.dashboardRefreshPending = false;
    if (refreshPending && state.token && state.view === "dashboard") void loadDashboard();
  }
}
function renderCases() {
  const query = $("#search-input").value.toLowerCase();
  const risk = $("#risk-filter").value;
  const status = $("#status-filter").value;
  const items = state.cases.filter((item) => (!risk || item.risk_level === risk) && (!status || item.status === status) && (!query || `${item.case_id} ${item.txn_id}`.toLowerCase().includes(query)));
  const first = state.total === 0 ? 0 : state.page * state.pageSize + 1;
  const last = Math.min((state.page + 1) * state.pageSize, state.total);
  $("#result-count").textContent = `${first}-${last} / ${state.total} case${state.total === 1 ? "" : "s"}`;
  $("#cases-body").innerHTML = items.length ? items.map((item) => {
    const probability = Number(item.calibrated_prob || 0) * 100;
    const date = item.created_at ? new Date(item.created_at).toLocaleDateString("vi-VN") : "—";
    return `<tr data-case-id="${escapeHtml(item.case_id)}"><td><b class="case-id">#${escapeHtml(item.run_number ?? item.case_id)}</b><div class="txn">${escapeHtml(item.txn_id)}</div></td><td><span class="risk risk-${riskClass(item.risk_level)}">${escapeHtml(titleCase(item.risk_level))}</span></td><td><span class="confidence">${formatProbability(item.calibrated_prob)}</span><span class="confidence-bar"><i style="width:${probability}%"></i></span><small class="score-caption">calibrated probability</small></td><td><span class="status status-${riskClass(item.status)}">${escapeHtml(statusLabel(item.status))}</span></td><td>${date}</td><td>${renderEvidence(item)}</td></tr>`;
  }).join("") : `<tr><td colspan="6" class="empty">No cases match these filters.</td></tr>`;
  document.querySelectorAll("[data-case-id]").forEach((row) => row.addEventListener("click", () => openDetail(row.dataset.caseId)));
  const pageCount = Math.max(1, Math.ceil(state.total / state.pageSize));
  $("#page-number").textContent = `${state.page + 1} / ${pageCount}`;
  $("#prev-page").disabled = state.page === 0;
  $("#next-page").disabled = state.page + 1 >= pageCount;
}

function dateInputBoundary(value, nextDay = false) {
  if (!value) return "";
  const date = new Date(`${value}T00:00:00`);
  if (nextDay) date.setDate(date.getDate() + 1);
  return date.toISOString();
}

async function loadCases() {
  const requestId = ++state.caseRequestId;
  const requestView = state.view;
  try {
    const offset = state.page * state.pageSize;
    const params = new URLSearchParams({
      limit: String(state.pageSize),
      offset: String(offset),
      sort: $("#sort-filter").value,
    });
    const filters = {
      risk_level: $("#risk-filter").value,
      status: $("#status-filter").value,
      score_band: $("#score-filter").value,
      received_from: dateInputBoundary($("#received-from-filter").value),
      received_before: dateInputBoundary($("#received-to-filter").value, true),
      evidence_status: $("#evidence-filter").value,
    };
    Object.entries(filters).forEach(([key, value]) => {
      if (value) params.set(key, value);
    });
    const path = state.view === "analytics"
      ? `/cases/analytics/transactions?${params}`
      : `/cases?${params}`;
    const result = await api(path);
    if (requestId !== state.caseRequestId || requestView !== state.view) return;
    state.cases = result.items || [];
    state.total = Number(result.total || 0);
    const navCount = $("#review-nav-count");
    navCount.textContent = new Intl.NumberFormat("en-US").format(Number(result.open_count) || 0);
    navCount.classList.toggle("hidden", !Number(result.open_count));
    $("#open-count").textContent = result.open_count ?? 0;
    $("#high-count").textContent = result.high_count ?? 0;
    $("#medium-count").textContent = result.medium_count ?? 0;
    renderDecisionSummary(result.decision_summary || {});
    renderCases();
  } catch (error) {
    if (requestId !== state.caseRequestId || requestView !== state.view) return;
    $("#cases-body").innerHTML = `<tr><td colspan="6" class="empty">Could not load cases: ${escapeHtml(error.message)}</td></tr>`;
    showToast(error.message);
  }
}
function renderClassifications() {
  const rows = state.classifications;
  const first = state.total === 0 ? 0 : state.page * state.pageSize + 1;
  const last = Math.min((state.page + 1) * state.pageSize, state.total);
  $("#classification-result-count").textContent = `${first}-${last} / ${state.total} manually reviewed`;
  $("#classifications-body").innerHTML = rows.length ? rows.map((item) => {
    const decisionTime = item.decided_at ? new Date(item.decided_at).toLocaleString("vi-VN") : "—";
    const amount = item.amount === null ? "—" : Number(item.amount).toLocaleString("vi-VN", { maximumFractionDigits: 2 });
    const merchant = item.merchant || item.category || "—";
    const location = [item.city, item.region].filter(Boolean).join(", ");
    const confidence = formatProbability(item.calibrated_prob);
    const locked = ["CLOSED", "CONFIRMED_SIMULATED"].includes(item.status);
    return `<tr data-case-id="${escapeHtml(item.case_id)}"><td><b class="case-id">#${escapeHtml(item.run_number ?? item.case_id)}</b><div class="txn">${escapeHtml(item.txn_id)}</div></td><td>${escapeHtml(merchant)}${location ? `<div class="txn">${escapeHtml(location)}</div>` : ""}</td><td><span class="risk risk-${riskClass(item.risk_level)}">${escapeHtml(titleCase(item.risk_level))} · ${confidence}</span></td><td><span class="decision-badge decision-${decisionClass(item.action)}">${escapeHtml(decisionLabel(item.action))}</span>${item.note ? `<div class="decision-note-preview">${escapeHtml(item.note)}</div>` : ""}</td><td>${escapeHtml(item.actor)}<div class="txn">${escapeHtml(titleCase(item.role))}</div></td><td>${amount}</td><td>${decisionTime}</td><td><button class="classification-edit" type="button" data-edit-case-id="${escapeHtml(item.case_id)}" ${locked ? "disabled title=\"This case is closed\"" : ""}>Edit</button></td></tr>`;
  }).join("") : `<tr><td colspan="8" class="empty">No manually classified transactions found.</td></tr>`;
  document.querySelectorAll("#classifications-body [data-case-id]").forEach((row) =>
    row.addEventListener("click", () => openDetail(row.dataset.caseId))
  );
  document.querySelectorAll("#classifications-body [data-edit-case-id]").forEach((button) =>
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      openDetail(button.dataset.editCaseId, true);
    })
  );
  const pageCount = Math.max(1, Math.ceil(state.total / state.pageSize));
  $("#classification-page").textContent = `${state.page + 1} / ${pageCount}`;
  $("#classification-prev").disabled = state.page === 0;
  $("#classification-next").disabled = state.page + 1 >= pageCount;
}
async function loadClassifications() {
  const requestId = ++state.classificationRequestId;
  const params = new URLSearchParams({
    limit: String(state.pageSize),
    offset: String(state.page * state.pageSize),
    sort: $("#classification-sort-filter").value,
  });
  const decision = $("#classification-filter").value;
  const search = $("#classification-search").value.trim();
  const filters = {
    status: $("#classification-status-filter").value,
    stream: $("#classification-stream-filter").value,
    merchant_type: $("#classification-merchant-filter").value,
    score_band: $("#classification-score-filter").value,
  };
  if (decision) params.set("decision", decision);
  if (search) params.set("search", search);
  Object.entries(filters).forEach(([key, value]) => {
    if (value) params.set(key, value);
  });
  try {
    const result = await api(`/cases/classifications?${params}`);
    if (requestId !== state.classificationRequestId || state.view !== "classifications") return;
    state.classifications = result.items || [];
    state.total = Number(result.total || 0);
    renderDecisionSummary(result.decision_summary || {});
    renderClassifications();
  } catch (error) {
    if (requestId !== state.classificationRequestId || state.view !== "classifications") return;
    $("#classifications-body").innerHTML = `<tr><td colspan="8" class="empty">Could not load manual decisions: ${escapeHtml(error.message)}</td></tr>`;
    showToast(error.message);
  }
}
function loadCurrentView() {
  if (state.view === "dashboard") return loadDashboard();
  if (state.view === "classifications") return loadClassifications();
  if (state.view === "manual-review") return loadManualReview();
  if (state.view === "fault-lab") return renderFaultLabResult();
  return loadCases();
}
function setView(view) {
  if (view === "fault-lab" && !state.faultLabEnabled) return;
  if (state.manualReviewPollTimer !== null) clearTimeout(state.manualReviewPollTimer);
  state.manualReviewPollTimer = null;
  state.view = view;
  const dashboard = view === "dashboard";
  const analytics = view === "analytics";
  const classifications = view === "classifications";
  const manualReview = view === "manual-review";
  const faultLab = view === "fault-lab";
  const showDecisionSummary = !dashboard && !faultLab && !manualReview;
  state.page = 0;
  state.manualReviewPage = 0;
  $("#work-queue-layout").classList.toggle("with-summary", showDecisionSummary);
  $("#dashboard-nav").classList.toggle("active", dashboard);
  $("#review-nav").classList.toggle("active", view === "review");
  $("#analytics-nav").classList.toggle("active", analytics);
  $("#classifications-nav").classList.toggle("active", classifications);
  $("#manual-review-nav").classList.toggle("active", manualReview);
  $("#fault-lab-nav").classList.toggle("active", faultLab);
  $("#page-title").textContent = dashboard ? "Risk Overview" : classifications ? "Decision History" : analytics ? "Risk Analytics" : manualReview ? "Manual Review" : faultLab ? "Fault Lab" : "Review Queue";
  $("#review-kpi-grid").classList.toggle("hidden", view !== "review");
  $("#analytics-kpi-grid").classList.toggle("hidden", !analytics);
  $("#analytics-decisions").classList.toggle("hidden", !showDecisionSummary);
  $("#dashboard-content").classList.toggle("hidden", !dashboard);
  $("#review-content").classList.toggle("hidden", dashboard || classifications || faultLab || manualReview);
  $("#classifications-content").classList.toggle("hidden", !classifications);
  $("#manual-review-content").classList.toggle("hidden", !manualReview);
  $("#fault-lab-content").classList.toggle("hidden", !faultLab);
  $("#queue-title").textContent = analytics ? "High & Medium Risk Transactions" : "Case Review Queue";
  $("#queue-description").textContent = analytics
    ? "Transactions ranked by model-predicted fraud probability."
    : "Cases across all risk levels, ordered by model confidence.";
  animateCurrentView();
  loadCurrentView();
}

function renderFaultLabResult() {
  const result = state.faultLabResult;
  const host = $("#fault-lab-result");
  if (!result) {
    host.innerHTML = state.faultLabError
      ? `<p class="fault-lab-error">${escapeHtml(state.faultLabError)}</p>`
      : `<p class="muted">Chưa gửi giao dịch nào.</p>`;
    return;
  }
  const errors = Array.isArray(result.validation_errors) ? result.validation_errors : [];
  host.innerHTML = `
    <dl>
      <dt>txn_id</dt><dd>${escapeHtml(result.txn_id || "—")}</dd>
      <dt>trace_id</dt><dd>${escapeHtml(result.trace_id || "—")}</dd>
      <dt>status</dt><dd>${escapeHtml(result.status || "QUEUED")}</dd>
      <dt>risk_level</dt><dd>${escapeHtml(result.risk_level || "—")}</dd>
    </dl>
    <b>validation_errors</b>
    <pre>${escapeHtml(errors.length ? JSON.stringify(errors, null, 2) : "[]")}</pre>
    ${result.status === "MANUAL_FALLBACK_UNSCORED"
      ? `<p class="manual-fallback-message">This transaction was routed to Manual Review. <button class="secondary-button" type="button" data-open-manual-review>Open Manual Review</button></p>`
      : ""}
    ${state.faultLabError ? `<p class="fault-lab-error">${escapeHtml(state.faultLabError)}</p>` : ""}
  `;
  host.querySelector("[data-open-manual-review]")?.addEventListener("click", () => setView("manual-review"));
}
function renderManualReview() {
  const items = state.manualReviewItems;
  const list = $("#manual-review-list");
  list.innerHTML = items.length ? items.map((item) => {
    const errors = Array.isArray(item.validation_errors) ? item.validation_errors : [];
    const errorList = errors.length
      ? `<ul>${errors.map((error) => `<li><b>${escapeHtml(error.field || "Data")}</b>: ${escapeHtml(error.msg || "Invalid value")}</li>`).join("")}</ul>`
      : `<p class="muted">No validation details were recorded.</p>`;
    const transaction = JSON.stringify(item.transaction || {}, null, 2);
    const received = item.created_at ? new Date(item.created_at).toLocaleString("vi-VN") : "—";
    return `<article class="manual-review-item" data-manual-review-txn="${escapeHtml(item.txn_id)}">
      <header><div><b>${escapeHtml(item.txn_id)}</b><small>Received ${escapeHtml(received)} · Trace ${escapeHtml(item.trace_id || "—")}</small></div><span class="status status-open">Unscored</span></header>
      <div class="manual-review-errors"><b>Validation errors</b>${errorList}</div>
      <details><summary>Transaction data</summary><pre>${escapeHtml(transaction)}</pre></details>
      <div class="manual-review-actions">
        <label>Risk level
          <select data-manual-review-risk>
            <option value="">Select a risk level</option><option value="LOW">Low</option><option value="MEDIUM">Medium</option><option value="HIGH">High</option>
          </select>
        </label>
        <label>Review reason
          <input data-manual-review-reason maxlength="2000" placeholder="Explain your assessment" />
        </label>
        <button class="primary-button" type="button" data-create-review-case>Create review case</button>
      </div>
    </article>`;
  }).join("") : `<p class="muted">No transactions are waiting for manual review.</p>`;
  list.querySelectorAll("[data-manual-review-txn]").forEach((card) => {
    const txnId = card.dataset.manualReviewTxn;
    const riskInput = card.querySelector("[data-manual-review-risk]");
    const reasonInput = card.querySelector("[data-manual-review-reason]");
    const draft = state.manualReviewDrafts.get(txnId);
    if (draft) {
      riskInput.value = draft.riskLevel;
      reasonInput.value = draft.reason;
    }
    const saveDraft = () => {
      const current = { riskLevel: riskInput.value, reason: reasonInput.value };
      if (current.riskLevel || current.reason) state.manualReviewDrafts.set(txnId, current);
      else state.manualReviewDrafts.delete(txnId);
    };
    riskInput.addEventListener("change", saveDraft);
    reasonInput.addEventListener("input", saveDraft);
    card.querySelector("[data-create-review-case]").addEventListener("click", (event) =>
      createManualReviewCase(txnId, event.currentTarget)
    );
  });
  const first = state.manualReviewTotal ? state.manualReviewPage * state.pageSize + 1 : 0;
  const last = Math.min((state.manualReviewPage + 1) * state.pageSize, state.manualReviewTotal);
  $("#manual-review-count").textContent = `${first}-${last} / ${state.manualReviewTotal} transactions`;
  $("#manual-review-page").textContent = `${state.manualReviewPage + 1}`;
  $("#manual-review-prev").disabled = state.manualReviewPage === 0;
  $("#manual-review-next").disabled = last >= state.manualReviewTotal;
}
function scheduleManualReviewRefresh() {
  if (state.manualReviewPollTimer !== null) clearTimeout(state.manualReviewPollTimer);
  state.manualReviewPollTimer = null;
  if (!state.token || state.view !== "manual-review") return;
  state.manualReviewPollTimer = setTimeout(() => {
    state.manualReviewPollTimer = null;
    void loadManualReview();
  }, 10000);
}
async function loadManualReview() {
  const requestId = ++state.manualReviewRequestId;
  try {
    const params = new URLSearchParams({
      limit: String(state.pageSize),
      offset: String(state.manualReviewPage * state.pageSize),
    });
    const result = await api(`/cases/manual-review/queue?${params}`);
    if (requestId !== state.manualReviewRequestId) return;
    const previousTotal = state.manualReviewTotal;
    const itemsChanged = result.total !== previousTotal
      || (result.items || []).length !== state.manualReviewItems.length
      || (result.items || []).some((item, index) => item.txn_id !== state.manualReviewItems[index]?.txn_id);
    state.manualReviewItems = result.items || [];
    state.manualReviewTotal = Number(result.total) || 0;
    const badge = $("#manual-review-nav-count");
    badge.textContent = new Intl.NumberFormat("en-US").format(state.manualReviewTotal);
    badge.classList.toggle("hidden", !state.manualReviewTotal);
    if (state.view === "manual-review" && state.manualReviewPage > 0 && state.manualReviewTotal > previousTotal) {
      state.manualReviewPage = 0;
      void loadManualReview();
      return;
    }
    if (state.view === "manual-review" && itemsChanged) renderManualReview();
    scheduleManualReviewRefresh();
  } catch (error) {
    if (requestId !== state.manualReviewRequestId || state.view !== "manual-review") return;
    $("#manual-review-list").innerHTML = `<p class="fault-lab-error">Could not load manual review queue: ${escapeHtml(error.message)}</p>`;
    showToast(error.message);
    scheduleManualReviewRefresh();
  }
}
async function createManualReviewCase(txnId, button) {
  const item = button.closest("[data-manual-review-txn]");
  const riskLevel = item.querySelector("[data-manual-review-risk]").value;
  const reason = item.querySelector("[data-manual-review-reason]").value.trim();
  if (!riskLevel || !reason) {
    showToast("Select a risk level and enter a review reason.");
    return;
  }
  button.disabled = true;
  try {
    const result = await api("/cases/manual", {
      method: "POST",
      body: JSON.stringify({ txn_id: txnId, risk_level: riskLevel, reason }),
    });
    state.manualReviewDrafts.delete(txnId);
    state.manualReviewTotal = Math.max(0, state.manualReviewTotal - 1);
    $("#manual-review-nav-count").textContent = new Intl.NumberFormat("en-US").format(state.manualReviewTotal);
    $("#manual-review-nav-count").classList.toggle("hidden", !state.manualReviewTotal);
    showToast("Review case created. Opening it for assessment.");
    setView("review");
    void openDetail(result.case_id);
  } catch (error) {
    showToast(error.message);
    button.disabled = false;
  }
}
function makeFaultLabTxnId() {
  const id = typeof crypto !== "undefined" && crypto.randomUUID
    ? crypto.randomUUID()
    : `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
  return `fault-lab-${id}`;
}
function buildFaultLabPayload() {
  if (!state.faultLabPreviewTxnId) state.faultLabPreviewTxnId = makeFaultLabTxnId();
  const payload = {
    trans_num: "fault-lab-preview",
    trans_date_trans_time: "2026-01-15T12:00:00Z",
    cc_num: "4111111111111111",
    merchant: "fraud_Kirlin and Sons",
    category: "shopping_pos",
    amt: 42.5,
    lat: 37.7749,
    long: -122.4194,
    merch_lat: 37.7849,
    merch_long: -122.4094,
    city: "San Francisco",
    state: "CA",
  };
  const mode = $("#fault-lab-kind").value;
  if (mode === "missing") {
    delete payload[$("#fault-lab-missing-field").value];
  } else if (mode === "type") {
    payload[$("#fault-lab-type-field").value] = $("#fault-lab-type-value").value;
  } else if (mode === "invalid") {
    const field = $("#fault-lab-invalid-value").value;
    payload[field] = field === "amt" ? -100 : 999;
  } else if (mode === "advanced") {
    const advanced = JSON.parse($("#fault-lab-json").value);
    if (!advanced || typeof advanced !== "object" || Array.isArray(advanced)) {
      throw new Error("Payload phải là một JSON object");
    }
    return { ...advanced, trans_num: state.faultLabPreviewTxnId };
  }
  return { ...payload, trans_num: state.faultLabPreviewTxnId };
}
function renderFaultLabComposer() {
  const mode = $("#fault-lab-kind").value;
  $("#fault-lab-missing-options").classList.toggle("hidden", mode !== "missing");
  $("#fault-lab-type-options").classList.toggle("hidden", mode !== "type");
  $("#fault-lab-invalid-options").classList.toggle("hidden", mode !== "invalid");
  $("#fault-lab-advanced-options").classList.toggle("hidden", mode !== "advanced");
  try {
    const payload = buildFaultLabPayload();
    $("#fault-lab-input-error").textContent = "";
    $("#fault-lab-preview-json").textContent = JSON.stringify(payload, null, 2);
  } catch (error) {
    $("#fault-lab-input-error").textContent = error.message;
    $("#fault-lab-preview-json").textContent = "";
  }
}
async function pollFaultLabResult(txnId) {
  try {
    for (let attempt = 0; attempt < 30; attempt += 1) {
      await new Promise((resolve) => setTimeout(resolve, 1000));
      const result = await api(`/transactions/${encodeURIComponent(txnId)}/result`);
      if (state.faultLabResult?.txn_id !== txnId) return;
      if (result.status === "PENDING") continue;
      state.faultLabResult = { ...state.faultLabResult, ...result };
      state.faultLabError = "";
      renderFaultLabResult();
      if (result.status === "MANUAL_FALLBACK_UNSCORED") void loadManualReview();
      return;
    }
    if (state.faultLabResult?.txn_id !== txnId) return;
    state.faultLabError = "Đã gửi giao dịch nhưng chưa nhận được kết quả.";
    renderFaultLabResult();
  } catch (error) {
    if (state.faultLabResult?.txn_id !== txnId) return;
    state.faultLabError = `Không thể lấy kết quả: ${error.message}`;
    renderFaultLabResult();
  }
}
async function submitFaultLab() {
  const button = $("#fault-lab-submit");
  state.faultLabError = "";
  let payload;
  try {
    payload = buildFaultLabPayload();
  } catch (error) {
    $("#fault-lab-input-error").textContent = error.message;
    return;
  }
  state.faultLabPreviewTxnId = makeFaultLabTxnId();
  payload.trans_num = state.faultLabPreviewTxnId;
  $("#fault-lab-preview-json").textContent = JSON.stringify(payload, null, 2);
  if ($("#fault-lab-kind").value === "advanced") {
    $("#fault-lab-json").value = JSON.stringify(payload, null, 2);
  }
  button.disabled = true;
  try {
    const queued = await api("/fault-lab/transactions", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    $("#fault-lab-json").value = JSON.stringify({ ...payload, trans_num: queued.txn_id }, null, 2);
    state.faultLabResult = queued;
    state.faultLabPreviewTxnId = makeFaultLabTxnId();
    renderFaultLabResult();
    renderFaultLabComposer();
    if (queued.status === "QUEUED") void pollFaultLabResult(queued.txn_id);
  } catch (error) {
    state.faultLabError = error.message;
    renderFaultLabResult();
  } finally {
    button.disabled = false;
  }
}

async function loadTestBatch() {
  try {
    const current = await api(`/demo/simulator?initial_offset=${state.testOffset}&initial_tps=${state.simulatorTps}`);
    applySimulatorState(current);
    const action = current.running ? "stop" : "start";
    const tps = action === "start" ? readSimulatorTps() : undefined;
    const result = await api("/demo/simulator", {
      method: "POST",
      body: JSON.stringify({ action, ...(tps === undefined ? {} : { tps }) }),
    });
    applySimulatorState(result);
    showToast(action === "stop" ? "Simulator stopped across all pages" : "Simulator started");
  } catch (error) {
    showToast(error.message);
    if (/transactions\/sec/.test(error.message)) $("#simulator-tps").focus();
  }
}
async function resetDemoData() {
  if (!window.confirm("This deletes simulator-generated transactions, their cases and review actions, then immediately restarts replay from the beginning. Continue?")) return;
  const button = $("#reset-demo-data");
  button.disabled = true;
  try {
    const result = await api("/demo/reset", { method: "POST" });
    state.page = 0;
    state.lastSimulatorViewRefresh = Date.now();
    applySimulatorState(result);
    loadCurrentView();
    showToast(`Deleted ${result.deleted_transactions} demo transactions and ${result.deleted_cases} cases; replay restarted`);
  } catch (error) {
    showToast(error.message);
  } finally {
    button.disabled = false;
  }
}
async function syncSimulatorState() {
  if (!state.token || state.simulatorPolling) return;
  state.simulatorPolling = true;
  try {
    const result = await api(`/demo/simulator?initial_offset=${state.testOffset}&initial_tps=${state.simulatorTps}`);
    state.simulatorSyncError = "";
    applySimulatorState(result);
  } finally {
    state.simulatorPolling = false;
  }
}
function applySimulatorState(simulator, broadcast = true) {
  if (simulator.version < state.simulatorVersion) return;
  const previousOffset = state.testOffset;
  const wasRunning = state.simulatorRunning;
  state.testOffset = simulator.offset;
  state.simulatorTps = simulator.tps;
  state.simulatorEverStarted = simulator.ever_started;
  state.simulatorVersion = simulator.version;
  state.simulatorRunning = simulator.running;
  state.simulatorError = simulator.error;
  const sampledAt = Date.now();
  const history = Array.isArray(simulator.open_case_rate_history)
    ? simulator.open_case_rate_history
    : [];
  state.simulatorHistory = history.map((point) => {
    const low = Number(point.low) || 0;
    const medium = Number(point.medium) || 0;
    const high = Number(point.high) || 0;
    const value = Number(point.value);
    return {
      timestamp: Number(point.timestamp) * 1000,
      low,
      medium,
      high,
      total: Number.isFinite(value) ? value : low + medium + high,
    };
  });
  if (!state.simulatorHistory.length) {
    state.simulatorHistory.push({
      timestamp: sampledAt,
      low: 0,
      medium: 0,
      high: 0,
      total: Number(simulator.open_cases_per_second) || 0,
    });
  }
  const cutoff = sampledAt - SIMULATOR_CHART_WINDOW_SECONDS * 1000;
  state.simulatorHistory = state.simulatorHistory.filter((point) => point.timestamp >= cutoff);
  if (state.view === "dashboard") renderSimulatorChart();
  $("#simulator-actual-tps").textContent =
    `New cases (5-sec avg): ${Number(simulator.open_cases_per_second || 0).toLocaleString("en-US", { maximumFractionDigits: 1 })} / sec`;
  localStorage.setItem("fraud_test_offset", String(state.testOffset));
  localStorage.setItem("fraud_simulator_tps", String(state.simulatorTps));
  if (!state.simulatorTpsInputDirty) $("#simulator-tps").value = String(state.simulatorTps);
  if (wasRunning !== state.simulatorRunning) setSimulatorRunning(state.simulatorRunning);
  if (state.simulatorError && state.simulatorError !== state.simulatorSyncError) {
    showToast(`Simulator stopped: ${state.simulatorError}`);
    state.simulatorSyncError = state.simulatorError;
  }
  if (state.testOffset !== previousOffset && Date.now() - state.lastSimulatorViewRefresh >= 3000) {
    state.lastSimulatorViewRefresh = Date.now();
    void loadCurrentView();
  }
  if (broadcast) simulatorChannel?.postMessage({ type: "simulator-state", state: simulator });
}
simulatorChannel?.addEventListener("message", ({ data }) => {
  if (data?.type === "simulator-state") applySimulatorState(data.state, false);
});
function setSimulatorRunning(running) {
  state.simulatorRunning = running;
  const button = $("#load-test-batch");
  const icon = running
    ? '<svg class="symbol-icon button-icon" viewBox="0 0 24 24" aria-hidden="true"><rect x="6" y="6" width="12" height="12" rx="2" fill="currentColor" stroke="none" /></svg>'
    : '<svg class="symbol-icon button-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M8 5v14l11-7z" fill="currentColor" stroke="none" /></svg>';
  button.innerHTML = `${icon}${running ? "Stop Simulator" : "Run Simulator"}`;
  button.classList.toggle("warn", running);
}
function readSimulatorTps() {
  const input = $("#simulator-tps");
  const tps = Number(input.value);
  if (!input.value || !Number.isInteger(tps) || tps < 1 || tps > MAX_SIMULATOR_TPS) {
    throw new Error(`Enter an integer from 1 to ${MAX_SIMULATOR_TPS} transactions/sec`);
  }
  return tps;
}
async function openDetail(caseId, editDecision = false) {
  try {
    const item = await api(`/cases/${caseId}`); state.selected = item;
    if (!state.detailRouteActive && window.history?.pushState) {
      window.history.pushState({ caseReviewDetail: true }, "", `#case/${encodeURIComponent(item.case_id)}`);
      state.detailRouteActive = true;
    }
    const transaction = Object.entries(item.transaction || {}).map(([key, value]) => `<div class="data-field"><small>${escapeHtml(titleCase(key))}</small><b>${escapeHtml(value)}</b></div>`).join("");
    const validationErrors = Array.isArray(item.validation_errors) ? item.validation_errors : [];
    const validationErrorList = validationErrors.length
      ? `<ul>${validationErrors.map((error) => `<li><b>${escapeHtml(error.field || "Data")}</b>: ${escapeHtml(error.msg || "Invalid value")}</li>`).join("")}</ul>`
      : `<p>No validation details were recorded.</p>`;
    const validationSection = item.scoring_status !== "SCORED"
      ? `<section class="detail-section manual-validation-section"><h4>Manual review · transaction was not scored</h4>${validationErrorList}</section>`
      : "";
    $("#detail-title").textContent = `Case #${item.run_number ?? item.case_id}`;
    const terminal = ["CLOSED", "DISMISSED", "CONFIRMED_FRAUD", "CONFIRMED_SIMULATED"].includes(item.status);
    const hasPreviousOutcome = (item.actions || []).some((action) =>
      action.action.startsWith("OUTCOME_")
    );
    const reclassifying = editDecision && hasPreviousOutcome &&
      ["CONFIRMED_FRAUD", "DISMISSED"].includes(item.status);
    const decisionLocked = terminal && !reclassifying;
    const actionHistory = (item.actions || []).map((action) =>
      `<li><b>${escapeHtml(decisionLabel(action.action))}</b><span>${escapeHtml(action.actor)} · ${new Date(action.created_at).toLocaleString("vi-VN")}</span>${action.note ? `<p>${escapeHtml(action.note)}</p>` : ""}</li>`
    ).join("");
    const actionButtons = `<button class="decision-fraud" data-action="CONFIRMED_FRAUD">Confirm fraud</button><button class="decision-dismiss" data-action="DISMISSED">Dismiss / safe</button><button class="decision-evidence" data-action="INSUFFICIENT_EVIDENCE">Insufficient evidence</button>${reclassifying ? "" : `<button class="decision-escalate" data-action="ESCALATE">Escalate to Risk Lead</button><button class="decision-more" data-action="NEEDS_MORE_INFO">Need more info</button><button class="decision-note" data-action="NOTE">Save note</button>`}`;
    const decisionSection = decisionLocked
      ? `<p class="decision-locked">This case already has a final outcome.</p>`
      : `${reclassifying ? `<p class="section-note reclassification-note">Revising this decision appends a new entry to the audit history.</p>` : ""}<label class="decision-note-label" for="decision-note">Review note <span>(optional)</span></label><textarea id="decision-note" maxlength="2000" placeholder="Add investigation notes or handoff context..."></textarea><div class="decision-actions">${actionButtons}</div>`;
    $("#detail-content").innerHTML =
      `<div class="case-review-grid">
        <div class="case-review-left">
          <section class="case-evidence-card">${window.RiskReview.renderOverview(item)}
            ${validationSection}
            <section class="detail-section investigation-section"><h4>Investigation checklist</h4><div class="brief">${renderBrief(item.brief, item.risk_level)}</div></section>
            ${window.RiskReview.renderTechnicalDetails(item)}
            <details class="transaction-details"><summary>Transaction details</summary><div class="data-grid">${transaction}</div></details>
          </section>
        </div>
        <div class="case-review-right">
          <section class="case-evidence-card risk-signals-card">${window.RiskReview.renderSignals(item)}</section>
          <section class="case-evidence-card decision-card">
            <div class="detail-section decision-section">
              <h4>${reclassifying ? "Re-evaluate decision" : "Analyst decision / action"}</h4>
              <p class="section-note">Record the review outcome or send this case to a Risk Lead. Decisions are audited in case history; no payment or account blocking integration is connected.</p>
              ${decisionSection}
            </div>
            ${actionHistory ? `<div class="detail-section decision-history-section"><h4>Decision history</h4><ul class="decision-history">${actionHistory}</ul></div>` : ""}
          </section>
        </div>
      </div>`;
    $("#detail-content .copy-transaction-id")?.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(String(item.txn_id || ""));
        showToast("Transaction ID copied");
      } catch {
        showToast("Could not copy. The full ID is available in Model details.");
      }
    });
    $("#detail-panel").classList.add("open");
    document.body?.classList?.add("case-review-open");
    $("#detail-panel").scrollTop = 0;
    $("#detail-panel").focus?.();
    document.querySelectorAll("[data-action]").forEach((button) => button.addEventListener("click", () => actOnCase(button.dataset.action)));
  } catch (error) { showToast(error.message); }
}

async function actOnCase(action) {
  const note = $("#decision-note")?.value.trim() || "";
  try {
    const previousOutcome = (state.selected.actions || []).some((entry) =>
      entry.action.startsWith("OUTCOME_")
    );
    if (OUTCOME_ACTIONS.includes(action)) {
      await api(`/cases/${state.selected.case_id}/outcome`, {
        method: "PUT",
        body: JSON.stringify({ outcome: action, note: note || null }),
      });
    } else {
      if (action === "NOTE" && !note) {
        showToast("Nhập nội dung ghi chú trước khi lưu");
        return;
      }
      await api(`/cases/${state.selected.case_id}/actions`, {
        method: "POST",
        body: JSON.stringify({ action, note: note || null }),
      });
    }
    const messages = {
      CONFIRMED_FRAUD: "Đã ghi nhận xác nhận gian lận",
      DISMISSED: "Đã ghi nhận giao dịch an toàn",
      INSUFFICIENT_EVIDENCE: "Đã ghi nhận chưa đủ bằng chứng",
      ESCALATE: "Đã chuyển case lên Risk Lead",
      NEEDS_MORE_INFO: "Đã đánh dấu cần bổ sung thông tin",
      NOTE: "Đã lưu ghi chú",
    };
    showToast(previousOutcome && OUTCOME_ACTIONS.includes(action)
      ? "Đã cập nhật quyết định và lưu vào lịch sử"
      : messages[action]);
    closeDetail();
    await loadCurrentView();
  } catch (error) { showToast(error.message); }
}
function closeDetail(useHistory = true) {
  const shouldGoBack = useHistory && state.detailRouteActive && window.history?.state?.caseReviewDetail;
  state.detailRouteActive = false;
  state.selected = null;
  $("#detail-panel").classList.remove("open");
  document.body?.classList?.remove("case-review-open");
  if (shouldGoBack) window.history.back();
}

function setAuthMode(mode) {
  state.authMode = mode;
  const register = mode === "register";
  $("#auth-subtitle").textContent = register ? "Tạo tài khoản để tham gia quy trình điều tra." : "Đăng nhập để tiếp tục tới không gian điều tra rủi ro.";
  $("#login-card-title").textContent = register ? "Tạo tài khoản" : "Chào mừng trở lại";
  $("#login-form").querySelector("button[type=submit]").innerHTML = register ? "Tạo tài khoản <span>→</span>" : "Đăng nhập <span>→</span>";
  $("#auth-switch").textContent = register ? "Đã có tài khoản? Đăng nhập" : "Chưa có tài khoản? Tạo tài khoản";
  $("#login-hint").classList.toggle("hidden", register);
  $("#forgot-password").classList.toggle("hidden", register);
  $("#google-login").classList.toggle("hidden", register);
  $("#login-form").elements.password.autocomplete = register ? "new-password" : "current-password";
  if (register) {
    $("#login-form").elements.username.value = "";
    $("#login-form").elements.password.value = "";
  }
}
$("#auth-switch").addEventListener("click", () => setAuthMode(state.authMode === "login" ? "register" : "login"));
$("#google-login").addEventListener("click", () => { window.location.href = "/api/v1/auth/google"; });
$("#forgot-password").addEventListener("click", async () => {
  const email = prompt("Nhập email đã đăng ký:");
  if (!email) return;
  try { const result = await fetch("/api/v1/auth/forgot-password", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ email }) }); const body = await result.json(); if (!result.ok) throw new Error(body.detail || "Không thể gửi email"); showToast(body.message); } catch (error) { showToast(error.message); }
});
$("#login-form").addEventListener("submit", async (event) => {
  event.preventDefault(); const data = Object.fromEntries(new FormData(event.currentTarget));
  try {
    const registering = state.authMode === "register";
    const endpoint = registering ? "/api/v1/auth/register" : "/api/v1/auth/login";
    const result = await fetch(endpoint, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data) });
    const body = await result.json();
    if (!result.ok) throw new Error(body.detail || "Không thể xác thực");
    state.token = body.access_token;
    localStorage.setItem("fraud_token", state.token);
    state.user = { username: data.username.trim().toLowerCase(), role: body.role };
    await loadFaultLabConfig();
    if (registering) {
      showToast("Tài khoản đã được tạo thành công");
      $("#login-form").elements.password.value = "";
      setAuthMode("login");
    }
    showApp();
    updateUser();
    await loadCurrentView();
    startDashboardPolling();
    try {
      await syncSimulatorState();
    } catch (error) {
      showToast(error.message);
    } finally {
      startSimulatorPolling();
    }
  } catch (error) {
    showToast(error.message);
  }
});
function updateUser() { const name = state.user?.username || "Chuyên viên"; const role = state.user?.role === "risk_lead" ? "Trưởng nhóm rủi ro" : "Chuyên viên phân tích rủi ro"; $("#user-name").textContent = name; $("#user-avatar").textContent = name[0].toUpperCase(); $("#user-role").textContent = role; }
function renderProfile(profile) {
  const displayName = profile.display_name || profile.username;
  const roleLabels = {
    analyst: "Chuyên viên phân tích rủi ro",
    risk_lead: "Trưởng nhóm rủi ro",
    admin: "Quản trị viên",
  };
  $("#profile-title").textContent = displayName;
  $("#profile-username").textContent = `@${profile.username}`;
  $("#profile-role").textContent = roleLabels[profile.role] || titleCase(profile.role);
  $("#profile-avatar").textContent = displayName.charAt(0).toUpperCase();
  $("#profile-cases-reviewed").textContent = Number(profile.cases_reviewed || 0).toLocaleString("vi-VN");
  $("#profile-active-streak").textContent = Number(profile.active_day_streak || 0).toLocaleString("vi-VN");
  $("#profile-active-days").textContent = Number(profile.active_days || 0).toLocaleString("vi-VN");
  $("#profile-badges").innerHTML = profile.badges.map((badge) => {
    const target = Math.max(1, Number(badge.target) || 1);
    const progress = Math.max(0, Math.min(target, Number(badge.progress) || 0));
    const percentage = Math.round(progress / target * 100);
    const status = badge.earned ? "Đã đạt" : "Đang tích lũy";
    return `<article class="profile-badge ${badge.earned ? "earned" : "locked"}">
      <span class="profile-medal" aria-hidden="true">${badge.earned ? "★" : "☆"}</span>
      <div class="profile-badge-details">
        <div class="profile-badge-title"><b>${escapeHtml(badge.name)}</b><span>${status}</span></div>
        <p>${escapeHtml(badge.description)}</p>
        <div class="profile-progress" role="progressbar" aria-valuenow="${progress}" aria-valuemin="0" aria-valuemax="${target}" aria-label="${escapeHtml(badge.name)}">
          <span style="width:${percentage}%"></span>
        </div>
        <small>${progress.toLocaleString("vi-VN")} / ${target.toLocaleString("vi-VN")} hồ sơ</small>
      </div>
    </article>`;
  }).join("");
}
async function openProfile() {
  const dialog = $("#profile-dialog");
  dialog.showModal();
  $("#profile-badges").innerHTML = `<p class="profile-loading">Đang tải hồ sơ...</p>`;
  try {
    renderProfile(await api("/auth/profile"));
  } catch (error) {
    $("#profile-badges").innerHTML = `<p class="profile-loading">Không tải được hồ sơ: ${escapeHtml(error.message)}</p>`;
  }
}
$("#account-button").addEventListener("click", openProfile);
$("#profile-close").addEventListener("click", () => $("#profile-dialog").close());
$("#profile-dialog").addEventListener("click", (event) => {
  if (event.target === event.currentTarget) event.currentTarget.close();
});
function logout() {
  if (state.manualReviewPollTimer !== null) clearTimeout(state.manualReviewPollTimer);
  state.manualReviewPollTimer = null;
  localStorage.removeItem("fraud_token");
  state.token = null;
  state.user = null;
  state.faultLabEnabled = false;
  state.faultLabResult = null;
  state.faultLabError = "";
  state.faultLabPreviewTxnId = null;
  state.manualReviewItems = [];
  state.manualReviewTotal = 0;
  $("#manual-review-nav-count").classList.add("hidden");
  $("#fault-lab-nav").classList.add("hidden");
  showLogin();
}
$("#logout-button").addEventListener("click", logout);
$("#profile-logout-button").addEventListener("click", logout);
$("#simulator-tps").value = String(state.simulatorTps);
$("#simulator-tps").addEventListener("input", () => { state.simulatorTpsInputDirty = true; });
$("#simulator-tps").addEventListener("change", async () => {
  const input = $("#simulator-tps");
  const previousTps = state.simulatorTps;
  try {
    const tps = readSimulatorTps();
    if (tps === previousTps) {
      state.simulatorTpsInputDirty = false;
      input.value = String(previousTps);
      return;
    }
    input.disabled = true;
    try {
      const simulator = await api("/demo/simulator", {
        method: "POST",
        body: JSON.stringify({ action: "set_speed", tps }),
      });
      state.simulatorTpsInputDirty = false;
      applySimulatorState(simulator);
    } finally {
      input.disabled = false;
    }
    showToast(`Simulator rate set to ${tps} transactions/sec`);
  } catch (error) {
    showToast(error.message);
  }
});
$("#refresh-button").addEventListener("click", loadCurrentView); $("#load-test-batch").addEventListener("click", loadTestBatch); $("#reset-demo-data").addEventListener("click", resetDemoData); $("#dashboard-nav").addEventListener("click", () => setView("dashboard")); $("#dashboard-review-link").addEventListener("click", () => setView("review")); $("#dashboard-view-all").addEventListener("click", () => setView("review")); $("#review-nav").addEventListener("click", () => setView("review")); $("#analytics-nav").addEventListener("click", () => setView("analytics")); $("#classifications-nav").addEventListener("click", () => setView("classifications")); $("#manual-review-nav").addEventListener("click", () => setView("manual-review")); $("#fault-lab-nav").addEventListener("click", () => setView("fault-lab")); $("#fault-lab-submit").addEventListener("click", submitFaultLab); $("#dashboard-search").addEventListener("keydown", (event) => { if (event.key === "Enter") { $("#search-input").value = event.currentTarget.value.trim(); state.page = 0; setView("review"); renderCases(); } }); $("#search-input").addEventListener("input", renderCases); $("#risk-filter").addEventListener("change", renderCases); $("#status-filter").addEventListener("change", renderCases); $("#prev-page").addEventListener("click", () => { if (state.page > 0) { state.page -= 1; loadCases(); } }); $("#next-page").addEventListener("click", () => { if ((state.page + 1) * state.pageSize < state.total) { state.page += 1; loadCases(); } }); $("#classification-prev").addEventListener("click", () => { if (state.page > 0) { state.page -= 1; loadClassifications(); } }); $("#classification-next").addEventListener("click", () => { if ((state.page + 1) * state.pageSize < state.total) { state.page += 1; loadClassifications(); } }); $("#classification-filter").addEventListener("change", () => { state.page = 0; loadClassifications(); }); let classificationSearchTimer; $("#classification-search").addEventListener("input", () => { clearTimeout(classificationSearchTimer); classificationSearchTimer = setTimeout(() => { state.page = 0; loadClassifications(); }, 250); }); $("#close-detail").addEventListener("click", () => closeDetail());
$("#manual-review-prev").addEventListener("click", () => { if (state.manualReviewPage > 0) { state.manualReviewPage -= 1; loadManualReview(); } });
$("#manual-review-next").addEventListener("click", () => { if ((state.manualReviewPage + 1) * state.pageSize < state.manualReviewTotal) { state.manualReviewPage += 1; loadManualReview(); } });
$("#fault-lab-kind").addEventListener("change", renderFaultLabComposer);
$("#fault-lab-missing-field").addEventListener("change", renderFaultLabComposer);
$("#fault-lab-type-field").addEventListener("change", () => {
  $("#fault-lab-type-value").value = $("#fault-lab-type-field").value === "amt" ? "abc" : "not-a-number";
  renderFaultLabComposer();
});
$("#fault-lab-type-value").addEventListener("input", renderFaultLabComposer);
$("#fault-lab-invalid-value").addEventListener("change", renderFaultLabComposer);
$("#fault-lab-json").addEventListener("input", renderFaultLabComposer);
renderFaultLabComposer();
window.addEventListener("popstate", () => {
  if (state.detailRouteActive) closeDetail(false);
});
window.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && $("#detail-panel").classList.contains("open")) closeDetail();
});
const resetCasePageAndLoad = () => { state.page = 0; void loadCases(); };
["risk-filter", "status-filter", "score-filter", "received-from-filter", "received-to-filter", "evidence-filter", "sort-filter"]
  .forEach((id) => $(`#${id}`).addEventListener("change", resetCasePageAndLoad));
$("#review-filter-reset").addEventListener("click", () => {
  ["search-input", "risk-filter", "status-filter", "score-filter", "received-from-filter", "received-to-filter", "evidence-filter"]
    .forEach((id) => { $(`#${id}`).value = ""; });
  $("#sort-filter").value = "newest";
  resetCasePageAndLoad();
});
["classification-status-filter", "classification-stream-filter", "classification-merchant-filter", "classification-score-filter", "classification-sort-filter"]
  .forEach((id) => $(`#${id}`).addEventListener("change", () => { state.page = 0; void loadClassifications(); }));
$("#classification-filter-reset").addEventListener("click", () => {
  ["classification-search", "classification-filter", "classification-status-filter", "classification-stream-filter", "classification-merchant-filter", "classification-score-filter"]
    .forEach((id) => { $(`#${id}`).value = ""; });
  $("#classification-sort-filter").value = "newest";
  state.page = 0;
  void loadClassifications();
});
$("#export-button").addEventListener("click", () => { const csv = ["case_id,txn_id,risk_level,confidence,status", ...state.cases.map((item) => `${item.case_id},${item.txn_id},${item.risk_level},${item.calibrated_prob},${item.status}`)].join("\n"); const link = document.createElement("a"); link.href = URL.createObjectURL(new Blob([csv], { type: "text/csv" })); link.download = "fraud-cases.csv"; link.click(); });
$("#export-classifications").addEventListener("click", () => {
  const columns = ["case_id", "transaction_id", "merchant", "category", "risk_level", "model_probability", "decision", "status", "reviewer", "role", "note", "amount", "city", "region", "decided_at"];
  const rows = state.classifications.map((item) => [
    item.case_id, item.txn_id, item.merchant, item.category, item.risk_level,
    item.calibrated_prob, decisionLabel(item.action), item.status, item.actor,
    item.role, item.note, item.amount, item.city, item.region, item.decided_at,
  ]);
  const csv = [columns, ...rows].map((row) =>
    row.map((value) => `"${String(value ?? "").replaceAll('"', '""')}"`).join(",")
  ).join("\r\n");
  const link = document.createElement("a");
  link.href = URL.createObjectURL(new Blob([csv], { type: "text/csv;charset=utf-8" }));
  link.download = "manual-classifications.csv";
  link.click();
  URL.revokeObjectURL(link.href);
});
showIntro();

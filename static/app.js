localStorage.removeItem("fraud_token");
const DEFAULT_SIMULATOR_TPS = 10;
const MAX_SIMULATOR_TPS = 1000;
const savedSimulatorTps = Number(localStorage.getItem("fraud_simulator_tps"));
const initialSimulatorTps = Number.isInteger(savedSimulatorTps) && savedSimulatorTps >= 1 && savedSimulatorTps <= MAX_SIMULATOR_TPS
  ? savedSimulatorTps
  : DEFAULT_SIMULATOR_TPS;
const state = { token: null, user: null, cases: [], classifications: [], selected: null, view: "review", page: 0, pageSize: 200, total: 0, testOffset: Number(localStorage.getItem("fraud_test_offset") || 0), simulatorRunning: false, simulatorTps: initialSimulatorTps, authMode: "login" };
const $ = (selector) => document.querySelector(selector);
const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;" })[char]);
const titleCase = (value) => String(value || "").replaceAll("_", " ").toLowerCase().replace(/\b\w/g, (letter) => letter.toUpperCase());
const riskClass = (risk) => String(risk || "").toLowerCase();
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
const showToast = (message) => { const toast = $("#toast"); toast.textContent = message; toast.classList.add("show"); setTimeout(() => toast.classList.remove("show"), 2800); };

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

function showApp() { $("#login-view").classList.add("hidden"); $("#app-view").classList.remove("hidden"); }
function showLogin() { $("#login-view").classList.remove("hidden"); $("#app-view").classList.add("hidden"); }
function renderBrief(brief) {
  if (!brief) return "Brief đang được tạo hoặc chưa có dữ liệu.";
  if (typeof brief === "string") return escapeHtml(brief);
  const checklist = (brief.checklist || []).map((item) =>
    `<li>${escapeHtml(item.action || "")}${item.rule_code ? ` <small>(${escapeHtml(item.rule_code)})</small>` : ""}</li>`
  ).join("");
  const signals = (brief.key_signals || []).map((signal) => `<li>${escapeHtml(signal)}</li>`).join("");
  const citations = (brief.citations || []).map((citation) =>
    `<li><b>${escapeHtml(citation.rule_code || "")}</b>: ${escapeHtml(citation.quote || "")}</li>`
  ).join("");
  return `<p>${escapeHtml(brief.summary || "")}</p>`
    + (signals ? `<h5>Tín hiệu chính</h5><ul>${signals}</ul>` : "")
    + (checklist ? `<h5>Checklist điều tra</h5><ol>${checklist}</ol>` : "")
    + (citations ? `<h5>Nguồn tham chiếu</h5><ul>${citations}</ul>` : "");
}
function renderEvidence(item) {
  if (!item.brief_status) return `<span class="evidence muted-label">No brief</span>`;
  const label = item.brief_status === "READY" ? "Brief ready" : titleCase(item.brief_status);
  return `<span class="evidence evidence-${riskClass(item.brief_status)}">${escapeHtml(label)}</span>`;
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
    const confidence = Math.round(Number(item.calibrated_prob || 0) * 100);
    const date = item.created_at ? new Date(item.created_at).toLocaleDateString("vi-VN") : "—";
    return `<tr data-case-id="${escapeHtml(item.case_id)}"><td><b class="case-id">#${escapeHtml(item.case_id)}</b><div class="txn">${escapeHtml(item.txn_id)}</div></td><td><span class="risk risk-${riskClass(item.risk_level)}">${escapeHtml(titleCase(item.risk_level))}</span></td><td><span class="confidence">${confidence}%</span><span class="confidence-bar"><i style="width:${confidence}%"></i></span><small class="score-caption">calibrated probability</small></td><td><span class="status status-${riskClass(item.status)}">${escapeHtml(statusLabel(item.status))}</span></td><td>${date}</td><td>${renderEvidence(item)}</td></tr>`;
  }).join("") : `<tr><td colspan="6" class="empty">Không có case phù hợp với bộ lọc.</td></tr>`;
  document.querySelectorAll("[data-case-id]").forEach((row) => row.addEventListener("click", () => openDetail(row.dataset.caseId)));
  const pageCount = Math.max(1, Math.ceil(state.total / state.pageSize));
  $("#page-number").textContent = `${state.page + 1} / ${pageCount}`;
  $("#prev-page").disabled = state.page === 0;
  $("#next-page").disabled = state.page + 1 >= pageCount;
}

async function loadCases() {
  try {
    const offset = state.page * state.pageSize;
    const path = state.view === "analytics"
      ? `/cases/analytics/transactions?limit=${state.pageSize}&offset=${offset}`
      : `/cases?limit=${state.pageSize}&offset=${offset}`;
    const result = await api(path);
    state.cases = result.items || [];
    state.total = Number(result.total || 0);
    $("#open-count").textContent = result.open_count ?? 0;
    $("#high-count").textContent = result.high_count ?? 0;
    $("#confidence-count").textContent = `${Math.round(Number(result.average_probability || 0) * 100)}%`;
    const latencyMs = Number(result.scoring_latency_ms || 0);
    $("#latency-count").innerHTML = latencyMs >= 1000
      ? `${(latencyMs / 1000).toFixed(2)}<span class="unit">s</span>`
      : `${Math.round(latencyMs)}<span class="unit">ms</span>`;
    $("#nav-count").textContent = result.total ?? 0;
    renderCases();
  } catch (error) { $("#cases-body").innerHTML = `<tr><td colspan="6" class="empty">Không tải được dữ liệu: ${escapeHtml(error.message)}</td></tr>`; showToast(error.message); }
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
    const confidence = Math.round(Number(item.calibrated_prob || 0) * 100);
    const locked = ["CLOSED", "CONFIRMED_SIMULATED"].includes(item.status);
    return `<tr data-case-id="${escapeHtml(item.case_id)}"><td><b class="case-id">#${escapeHtml(item.case_id)}</b><div class="txn">${escapeHtml(item.txn_id)}</div></td><td>${escapeHtml(merchant)}${location ? `<div class="txn">${escapeHtml(location)}</div>` : ""}</td><td><span class="risk risk-${riskClass(item.risk_level)}">${escapeHtml(titleCase(item.risk_level))} · ${confidence}%</span></td><td><span class="decision-badge decision-${decisionClass(item.action)}">${escapeHtml(decisionLabel(item.action))}</span>${item.note ? `<div class="decision-note-preview">${escapeHtml(item.note)}</div>` : ""}</td><td>${escapeHtml(item.actor)}<div class="txn">${escapeHtml(titleCase(item.role))}</div></td><td>${amount}</td><td>${decisionTime}</td><td><button class="classification-edit" type="button" data-edit-case-id="${escapeHtml(item.case_id)}" ${locked ? "disabled title=\"This case is closed\"" : ""}>Edit</button></td></tr>`;
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
  const params = new URLSearchParams({
    limit: String(state.pageSize),
    offset: String(state.page * state.pageSize),
  });
  const decision = $("#classification-filter").value;
  const search = $("#classification-search").value.trim();
  if (decision) params.set("decision", decision);
  if (search) params.set("search", search);
  try {
    const result = await api(`/cases/classifications?${params}`);
    state.classifications = result.items || [];
    state.total = Number(result.total || 0);
    $("#classification-count").textContent = state.total;
    renderClassifications();
  } catch (error) {
    $("#classifications-body").innerHTML = `<tr><td colspan="8" class="empty">Could not load manual decisions: ${escapeHtml(error.message)}</td></tr>`;
    showToast(error.message);
  }
}
function loadCurrentView() {
  if (state.view === "classifications") return loadClassifications();
  return loadCases();
}
function setView(view) {
  state.view = view;
  const analytics = view === "analytics";
  const classifications = view === "classifications";
  state.page = 0;
  $("#review-nav").classList.toggle("active", view === "review");
  $("#analytics-nav").classList.toggle("active", analytics);
  $("#classifications-nav").classList.toggle("active", classifications);
  $("#page-title").textContent = classifications ? "Manual decisions" : analytics ? "Analytics" : "Review queue";
  $("#kpi-grid").classList.toggle("hidden", classifications);
  $("#review-content").classList.toggle("hidden", classifications);
  $("#classifications-content").classList.toggle("hidden", !classifications);
  $("#queue-title").textContent = analytics ? "Medium & High transactions" : "Priority cases";
  $("#queue-description").textContent = analytics
    ? "Medium và High risk, sắp xếp theo xác suất giảm dần."
    : "Cases include LOW signals; only Medium and High receive briefs.";
  loadCurrentView();
}
async function loadTestBatch() {
  const button = $("#load-test-batch");
  if (state.simulatorRunning) {
    setSimulatorRunning(false);
    showToast("Đã dừng simulator");
    return;
  }
  try {
    state.simulatorTps = readSimulatorTps();
  } catch (error) {
    showToast(error.message);
    $("#simulator-tps").focus();
    return;
  }
  setSimulatorRunning(true);
  try {
    while (state.simulatorRunning) {
      const startedAt = performance.now();
      state.simulatorTps = readSimulatorTps();
      const result = await api(`/demo/test-batch?batch_size=${state.simulatorTps}&offset=${state.testOffset}`, { method: "POST" });
      state.testOffset = result.next_offset;
      localStorage.setItem("fraud_test_offset", String(state.testOffset));
      await loadCases();
      if (!result.has_more) {
        state.simulatorRunning = false;
        showToast(`Đã phát hết dữ liệu test (${result.queued} giao dịch ở lượt cuối)`);
        break;
      }
      const waitMs = Math.max(0, 1000 - (performance.now() - startedAt));
      if (waitMs) await new Promise((resolve) => setTimeout(resolve, waitMs));
    }
  } catch (error) {
    showToast(error.message);
  } finally {
    setSimulatorRunning(false);
  }
}
function setSimulatorRunning(running) {
  state.simulatorRunning = running;
  const button = $("#load-test-batch");
  const tracker = $("#simulator-tracker");
  const video = $("#tracker-video");
  button.textContent = running ? "■ Dừng simulator" : "▶ Chạy simulator";
  button.classList.toggle("warn", running);
  tracker.classList.toggle("hidden", !running);
  tracker.setAttribute("aria-hidden", String(!running));
  if (running) {
    if (video.readyState > 0) video.currentTime = 0;
    video.play().catch(() => showToast("Không phát được video truy vết."));
  } else {
    video.pause();
    if (video.readyState > 0) video.currentTime = 0;
  }
}
function readSimulatorTps() {
  const input = $("#simulator-tps");
  const tps = Number(input.value);
  if (!input.value || !Number.isInteger(tps) || tps < 1 || tps > MAX_SIMULATOR_TPS) {
    throw new Error(`Nhập số nguyên từ 1 đến ${MAX_SIMULATOR_TPS} giao dịch/giây`);
  }
  state.simulatorTps = tps;
  localStorage.setItem("fraud_simulator_tps", String(tps));
  return tps;
}

async function openDetail(caseId, editDecision = false) {
  try {
    const item = await api(`/cases/${caseId}`); state.selected = item;
    const confidence = Math.round(Number(item.calibrated_prob || 0) * 100);
    const rawScore = Number(item.raw_model_score || 0);
    const signals = (item.explanations || []).map((signal) => `<li><span>${escapeHtml(titleCase(signal.feature))}</span><b class="${Number(signal.contribution) >= 0 ? "signal-up" : "signal-down"}">${Number(signal.contribution) >= 0 ? "+" : ""}${Number(signal.contribution).toFixed(2)}</b><small>${escapeHtml(signal.value)}</small></li>`).join("");
    const transaction = Object.entries(item.transaction || {}).map(([key, value]) => `<div class="data-field"><small>${escapeHtml(titleCase(key))}</small><b>${escapeHtml(value)}</b></div>`).join("");
    const trace = `<div class="trace-row"><span>Transaction ID</span><b>${escapeHtml(item.txn_id)}</b></div><div class="trace-row"><span>Model version</span><b>${escapeHtml(item.model_version || "—")}</b></div><div class="trace-row"><span>Threshold config</span><b>v${escapeHtml(item.threshold_config_version || "—")}</b></div>`;
    $("#detail-title").textContent = `Case #${item.case_id}`;
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
    $("#detail-content").innerHTML = `<section class="detail-hero"><div><span class="risk risk-${riskClass(item.risk_level)}">${escapeHtml(titleCase(item.risk_level))} priority</span><p class="detail-subtitle">${escapeHtml(item.txn_id)}</p></div><div class="hero-score"><strong>${confidence}%</strong><small>fraud probability</small></div></section><div class="detail-section"><div class="detail-grid"><div><small>Routing decision</small><b>${escapeHtml(titleCase(item.risk_level))} risk</b></div><div><small>Workflow status</small><b>${escapeHtml(statusLabel(item.status))}</b></div><div><small>Raw model score</small><b>${rawScore.toFixed(5)}</b></div><div><small>Brief status</small><b>${escapeHtml(item.brief_status ? titleCase(item.brief_status) : "Not generated")}</b></div></div></div><div class="detail-section"><h4>Model explanation</h4><p class="section-note">Top contributing signals used for triage. This is decision support, not a final fraud determination.</p><ul class="signal-list">${signals || `<li class="muted-label">No explanation signals available.</li>`}</ul></div><div class="detail-section"><h4>Case brief & investigation checklist</h4><div class="brief">${renderBrief(item.brief)}</div></div><div class="detail-section"><h4>Original transaction</h4><div class="data-grid">${transaction}</div></div><div class="detail-section"><h4>Traceability</h4><div class="trace-list">${trace}</div></div><div class="detail-section decision-section"><h4>${reclassifying ? "Re-evaluate decision" : "Analyst decision / action"}</h4><p class="section-note">Record the review outcome or send this case to a Risk Lead. Decisions are audited in case history; no payment or account blocking integration is connected.</p>${decisionSection}</div>${actionHistory ? `<div class="detail-section"><h4>Decision history</h4><ul class="decision-history">${actionHistory}</ul></div>` : ""}`;
    $("#detail-panel").classList.add("open"); $("#panel-backdrop").classList.add("open");
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
function closeDetail() { $("#detail-panel").classList.remove("open"); $("#panel-backdrop").classList.remove("open"); }

function setAuthMode(mode) {
  state.authMode = mode;
  const register = mode === "register";
  $("#auth-subtitle").textContent = register ? "Tạo tài khoản để tham gia quy trình điều tra." : "Tập trung tín hiệu rủi ro. Ra quyết định nhanh hơn.";
  $("#login-form").querySelector("button[type=submit]").innerHTML = register ? "Tạo tài khoản <span>→</span>" : "Đăng nhập <span>→</span>";
  $("#auth-switch").textContent = register ? "Đã có tài khoản? Đăng nhập" : "Chưa có tài khoản? Tạo tài khoản";
  $("#login-hint").classList.toggle("hidden", register);
  $("#forgot-password").classList.toggle("hidden", register);
  $("#google-login").classList.toggle("hidden", register);
  let display = $("#display-name");
  if (register && !display) {
    display = document.createElement("label");
    display.id = "display-name";
    display.innerHTML = 'Tên hiển thị<input name="display_name" autocomplete="name" required />';
    $("#login-form").insertBefore(display, $("#login-form").querySelector("label"));
    const email = document.createElement("label");
    email.id = "email-field";
    email.innerHTML = 'Email<input name="email" type="email" autocomplete="email" required />';
    $("#login-form").insertBefore(email, $("#login-form").querySelector("label"));
  } else if (!register && display) { display.remove(); $("#email-field")?.remove(); }
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
  try { const endpoint = state.authMode === "register" ? "/api/v1/auth/register" : "/api/v1/auth/login"; const result = await fetch(endpoint, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data) }); const body = await result.json(); if (!result.ok) throw new Error(body.detail || "Không thể xác thực"); if (state.authMode === "register") { showToast(body.message); setAuthMode("login"); return; } const auth = body; state.token = auth.access_token; localStorage.setItem("fraud_token", state.token); state.user = { username: data.username, role: auth.role }; showApp(); updateUser(); await loadCases(); void loadTestBatch(); } catch (error) { showToast(error.message); }
});
function updateUser() { const name = state.user?.username || "Analyst"; $("#user-name").textContent = name; $("#user-avatar").textContent = name[0].toUpperCase(); $("#user-role").textContent = titleCase(state.user?.role || "analyst"); }
$("#simulator-tps").value = String(state.simulatorTps);
$("#simulator-tps").addEventListener("change", () => {
  try { readSimulatorTps(); } catch (error) { showToast(error.message); return; }
  if (state.simulatorRunning) showToast(`Tốc độ simulator đổi thành ${state.simulatorTps} giao dịch/giây`);
});
$("#refresh-button").addEventListener("click", loadCurrentView); $("#load-test-batch").addEventListener("click", loadTestBatch); $("#review-nav").addEventListener("click", () => setView("review")); $("#analytics-nav").addEventListener("click", () => setView("analytics")); $("#classifications-nav").addEventListener("click", () => setView("classifications")); $("#search-input").addEventListener("input", renderCases); $("#risk-filter").addEventListener("change", renderCases); $("#status-filter").addEventListener("change", renderCases); $("#prev-page").addEventListener("click", () => { if (state.page > 0) { state.page -= 1; loadCases(); } }); $("#next-page").addEventListener("click", () => { if ((state.page + 1) * state.pageSize < state.total) { state.page += 1; loadCases(); } }); $("#classification-prev").addEventListener("click", () => { if (state.page > 0) { state.page -= 1; loadClassifications(); } }); $("#classification-next").addEventListener("click", () => { if ((state.page + 1) * state.pageSize < state.total) { state.page += 1; loadClassifications(); } }); $("#classification-filter").addEventListener("change", () => { state.page = 0; loadClassifications(); }); let classificationSearchTimer; $("#classification-search").addEventListener("input", () => { clearTimeout(classificationSearchTimer); classificationSearchTimer = setTimeout(() => { state.page = 0; loadClassifications(); }, 250); }); $("#close-detail").addEventListener("click", closeDetail); $("#panel-backdrop").addEventListener("click", closeDetail);
$("#logout-button").addEventListener("click", () => { localStorage.removeItem("fraud_token"); state.token = null; showLogin(); });
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
showLogin();

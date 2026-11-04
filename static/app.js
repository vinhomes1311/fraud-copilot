const state = { token: localStorage.getItem("fraud_token"), user: null, cases: [], selected: null };
const $ = (selector) => document.querySelector(selector);
const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;" })[char]);
const titleCase = (value) => String(value || "").replaceAll("_", " ").toLowerCase().replace(/\b\w/g, (letter) => letter.toUpperCase());
const riskClass = (risk) => String(risk || "").toLowerCase();
const showToast = (message) => { const toast = $("#toast"); toast.textContent = message; toast.classList.add("show"); setTimeout(() => toast.classList.remove("show"), 2800); };

async function api(path, options = {}) {
  const response = await fetch(`/api/v1${path}`, { ...options, headers: { "Content-Type": "application/json", Authorization: `Bearer ${state.token}`, ...(options.headers || {}) } });
  if (!response.ok) { const body = await response.json().catch(() => ({})); throw new Error(body.detail || `Request failed (${response.status})`); }
  return response.status === 204 ? null : response.json();
}

function showApp() { $("#login-view").classList.add("hidden"); $("#app-view").classList.remove("hidden"); }
function showLogin() { $("#login-view").classList.remove("hidden"); $("#app-view").classList.add("hidden"); }
function renderCases() {
  const query = $("#search-input").value.toLowerCase();
  const risk = $("#risk-filter").value;
  const status = $("#status-filter").value;
  const items = state.cases.filter((item) => (!risk || item.risk_level === risk) && (!status || item.status === status) && (!query || `${item.case_id} ${item.txn_id}`.toLowerCase().includes(query)));
  $("#result-count").textContent = `${items.length} case${items.length === 1 ? "" : "s"} shown`;
  $("#cases-body").innerHTML = items.length ? items.map((item) => {
    const confidence = Math.round(Number(item.calibrated_prob || 0) * 100);
    const date = item.created_at ? new Date(item.created_at).toLocaleDateString("vi-VN") : "—";
    return `<tr data-case-id="${escapeHtml(item.case_id)}"><td><b class="case-id">#${escapeHtml(item.case_id)}</b><div class="txn">${escapeHtml(item.txn_id)}</div></td><td><span class="risk risk-${riskClass(item.risk_level)}">${escapeHtml(titleCase(item.risk_level))}</span></td><td><span class="confidence">${confidence}%</span><span class="confidence-bar"><i style="width:${confidence}%"></i></span></td><td><span class="status ${item.status === "CLOSED" ? "status-closed" : ""}">${escapeHtml(titleCase(item.status))}</span></td><td>${date}</td><td>›</td></tr>`;
  }).join("") : `<tr><td colspan="6" class="empty">Không có case phù hợp với bộ lọc.</td></tr>`;
  document.querySelectorAll("[data-case-id]").forEach((row) => row.addEventListener("click", () => openDetail(row.dataset.caseId)));
}

async function loadCases() {
  try {
    const result = await api("/cases?limit=200");
    state.cases = result.items || [];
    $("#open-count").textContent = state.cases.filter((item) => item.status !== "CLOSED").length;
    $("#high-count").textContent = state.cases.filter((item) => item.risk_level === "HIGH").length;
    const average = state.cases.length ? state.cases.reduce((sum, item) => sum + Number(item.calibrated_prob || 0), 0) / state.cases.length : 0;
    $("#confidence-count").textContent = `${Math.round(average * 100)}%`;
    $("#nav-count").textContent = state.cases.length;
    renderCases();
  } catch (error) { $("#cases-body").innerHTML = `<tr><td colspan="6" class="empty">Không tải được dữ liệu: ${escapeHtml(error.message)}</td></tr>`; showToast(error.message); }
}

async function openDetail(caseId) {
  try {
    const item = await api(`/cases/${caseId}`); state.selected = item;
    const confidence = Math.round(Number(item.calibrated_prob || 0) * 100);
    $("#detail-title").textContent = `Case #${item.case_id}`;
    $("#detail-content").innerHTML = `<div class="detail-section"><div class="detail-grid"><div><small>Risk level</small><b><span class="risk risk-${riskClass(item.risk_level)}">${escapeHtml(titleCase(item.risk_level))}</span></b></div><div><small>Model confidence</small><b class="score">${confidence}%</b></div><div><small>Status</small><b>${escapeHtml(titleCase(item.status))}</b></div><div><small>Model version</small><b>${escapeHtml(item.model_version || "—")}</b></div></div></div><div class="detail-section"><h4>Analyst brief</h4><div class="brief">${escapeHtml(item.brief || "Brief đang được tạo hoặc chưa có dữ liệu.")}</div></div><div class="detail-section"><h4>Transaction signals</h4><div class="detail-grid">${Object.entries(item.transaction || {}).slice(0, 6).map(([key, value]) => `<div><small>${escapeHtml(titleCase(key))}</small><b>${escapeHtml(value)}</b></div>`).join("")}</div></div><div class="detail-section"><h4>Actions</h4><div class="action-row"><button data-action="NOTE">Add note</button><button data-action="NEEDS_MORE_INFO">Need more info</button><button class="warn" data-action="ESCALATE">Escalate case</button></div></div>`;
    $("#detail-panel").classList.add("open"); $("#panel-backdrop").classList.add("open");
    document.querySelectorAll("[data-action]").forEach((button) => button.addEventListener("click", () => actOnCase(button.dataset.action)));
  } catch (error) { showToast(error.message); }
}

async function actOnCase(action) {
  try { await api(`/cases/${state.selected.case_id}/actions`, { method: "POST", body: JSON.stringify({ action }) }); showToast("Case đã được cập nhật"); closeDetail(); await loadCases(); } catch (error) { showToast(error.message); }
}
function closeDetail() { $("#detail-panel").classList.remove("open"); $("#panel-backdrop").classList.remove("open"); }

$("#login-form").addEventListener("submit", async (event) => {
  event.preventDefault(); const data = Object.fromEntries(new FormData(event.currentTarget));
  try { const result = await fetch("/api/v1/auth/login", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data) }); if (!result.ok) throw new Error((await result.json()).detail || "Đăng nhập thất bại"); const auth = await result.json(); state.token = auth.access_token; localStorage.setItem("fraud_token", state.token); state.user = { username: data.username, role: auth.role }; showApp(); updateUser(); await loadCases(); } catch (error) { showToast(error.message); }
});
function updateUser() { const name = state.user?.username || "Analyst"; $("#user-name").textContent = name; $("#user-avatar").textContent = name[0].toUpperCase(); $("#user-role").textContent = titleCase(state.user?.role || "analyst"); }
$("#refresh-button").addEventListener("click", loadCases); $("#search-input").addEventListener("input", renderCases); $("#risk-filter").addEventListener("change", renderCases); $("#status-filter").addEventListener("change", renderCases); $("#close-detail").addEventListener("click", closeDetail); $("#panel-backdrop").addEventListener("click", closeDetail);
$("#logout-button").addEventListener("click", () => { localStorage.removeItem("fraud_token"); state.token = null; showLogin(); });
$("#export-button").addEventListener("click", () => { const csv = ["case_id,txn_id,risk_level,confidence,status", ...state.cases.map((item) => `${item.case_id},${item.txn_id},${item.risk_level},${item.calibrated_prob},${item.status}`)].join("\n"); const link = document.createElement("a"); link.href = URL.createObjectURL(new Blob([csv], { type: "text/csv" })); link.download = "fraud-cases.csv"; link.click(); });
if (state.token) { showApp(); updateUser(); loadCases(); }

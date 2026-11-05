const seedCases = [
  { id: "FC-1042", txn: "TXN-7F2A91", merchant: "Northstar Electronics", category: "Electronics", amount: 2480.5, currency: "USD", score: 0.94, risk: "HIGH", status: "OPEN", city: "Miami, US", time: "2 min ago", signals: ["Amount is unusually high for this cardholder", "Merchant is outside the cardholder's usual region", "Several attempts occurred in a short time"], actions: ["Verify cardholder through a trusted channel", "Review recent authorization attempts", "Check merchant and device history"] },
  { id: "FC-1041", txn: "TXN-61BC04", merchant: "Metro Travel Online", category: "Travel", amount: 892.0, currency: "USD", score: 0.78, risk: "HIGH", status: "IN_REVIEW", city: "Prague, CZ", time: "8 min ago", signals: ["Cross-border purchase is atypical", "Transaction occurred outside the usual activity window"], actions: ["Confirm travel plans with the cardholder", "Compare device and location signals"] },
  { id: "FC-1040", txn: "TXN-C09E17", merchant: "Green Market", category: "Groceries", amount: 164.23, currency: "USD", score: 0.46, risk: "MEDIUM", status: "OPEN", city: "Austin, US", time: "16 min ago", signals: ["Purchase amount is above the merchant-category baseline"], actions: ["Check for related transactions", "Review cardholder's recent spending pattern"] },
  { id: "FC-1039", txn: "TXN-A4D881", merchant: "CloudNine Subscriptions", category: "Digital services", amount: 14.99, currency: "USD", score: 0.12, risk: "LOW", status: "CLOSED", city: "Seattle, US", time: "28 min ago", signals: ["Small recurring payment at a previously seen merchant"], actions: ["No additional action suggested in this demo"] },
  { id: "FC-1038", txn: "TXN-3E8D20", merchant: "Luna Luxury Goods", category: "Luxury retail", amount: 1675.0, currency: "USD", score: 0.87, risk: "HIGH", status: "ESCALATED", city: "London, GB", time: "42 min ago", signals: ["High-value purchase in a new country", "Merchant has not appeared in the sample history"], actions: ["Escalate to the risk lead", "Verify cardholder and delivery details"] },
  { id: "FC-1037", txn: "TXN-B21760", merchant: "City Pharmacy", category: "Pharmacy", amount: 52.4, currency: "USD", score: 0.23, risk: "LOW", status: "OPEN", city: "Denver, US", time: "1 hr ago", signals: ["Location and amount are consistent with the sample profile"], actions: ["No additional action suggested in this demo"] },
];

const storageKey = "fraud-copilot-public-demo-v1";
const escapeHtml = (value) => String(value).replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;" })[char]);
const statusLabel = (value) => ({ IN_REVIEW: "In review", ESCALATED: "Escalated", CLOSED: "Closed", OPEN: "Open" }[value] || value);
const riskClass = (value) => value.toLowerCase();
let cases = loadCases();
let activeView = "review";
let toastTimer;

function loadCases() {
  try {
    const saved = JSON.parse(localStorage.getItem(storageKey));
    if (Array.isArray(saved) && saved.every((item) => item && typeof item.id === "string")) return saved;
  } catch (error) {
    console.warn("Could not load saved demo state; using sample cases.", error);
  }
  return structuredClone(seedCases);
}

function saveCases() {
  try {
    localStorage.setItem(storageKey, JSON.stringify(cases));
  } catch (error) {
    showToast("Không lưu được thay đổi trên trình duyệt này.");
    console.error("Could not save demo state.", error);
  }
}

function showToast(message) {
  const toast = document.querySelector("#toast");
  toast.textContent = message;
  toast.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.remove("show"), 2600);
}

function updateSummary() {
  const open = cases.filter((item) => item.status !== "CLOSED");
  const high = open.filter((item) => item.risk === "HIGH").length;
  const average = cases.length ? Math.round(cases.reduce((sum, item) => sum + item.score, 0) / cases.length * 100) : 0;
  document.querySelector("#open-count").textContent = String(open.length);
  document.querySelector("#high-count").textContent = String(high);
  document.querySelector("#average-score").textContent = `${average}%`;
  document.querySelector("#total-count").textContent = String(cases.length);
  document.querySelector("#nav-count").textContent = String(open.length);
}

function renderCases() {
  const query = document.querySelector("#search-input").value.trim().toLowerCase();
  const risk = document.querySelector("#risk-filter").value;
  const status = document.querySelector("#status-filter").value;
  const filtered = cases.filter((item) =>
    (!risk || item.risk === risk) &&
    (!status || item.status === status) &&
    (!query || `${item.id} ${item.txn} ${item.merchant} ${item.city}`.toLowerCase().includes(query))
  );
  document.querySelector("#result-count").textContent = `${filtered.length} of ${cases.length} cases`;
  document.querySelector("#cases-body").innerHTML = filtered.length ? filtered.map((item) => `
    <tr data-case-id="${escapeHtml(item.id)}">
      <td><b class="case-id">#${escapeHtml(item.id)}</b><div class="txn">${escapeHtml(item.merchant)} · ${escapeHtml(item.txn)}</div></td>
      <td><span class="risk risk-${riskClass(item.risk)}">${escapeHtml(item.risk)}</span></td>
      <td><span class="score">${Math.round(item.score * 100)}%</span><span class="score-bar"><i style="width:${Math.round(item.score * 100)}%"></i></span></td>
      <td><span class="status status-${riskClass(item.status)}">${escapeHtml(statusLabel(item.status))}</span></td>
      <td>${escapeHtml(item.currency)} ${item.amount.toLocaleString("en-US", { minimumFractionDigits: 2 })}</td>
      <td>${escapeHtml(item.city)}<div class="txn">${escapeHtml(item.time)}</div></td>
    </tr>`).join("") : `<tr><td class="empty" colspan="6">No cases match these filters.</td></tr>`;
  document.querySelectorAll("[data-case-id]").forEach((row) => row.addEventListener("click", () => openDetail(row.dataset.caseId)));
}

function openDetail(id) {
  const item = cases.find((candidate) => candidate.id === id);
  if (!item) return;
  document.querySelector("#detail-title").textContent = `Case ${item.id}`;
  document.querySelector("#detail-content").innerHTML = `
    <section class="detail-hero">
      <div><span class="risk risk-${riskClass(item.risk)}">${escapeHtml(item.risk)} priority</span><p class="detail-subtitle">${escapeHtml(item.merchant)} · ${escapeHtml(item.txn)}</p></div>
      <div class="hero-score"><strong>${Math.round(item.score * 100)}%</strong><small>illustrative risk score</small></div>
    </section>
    <section class="detail-section"><div class="detail-grid">
      <div><small>Category</small><b>${escapeHtml(item.category)}</b></div><div><small>Workflow status</small><b>${escapeHtml(statusLabel(item.status))}</b></div>
      <div><small>Transaction amount</small><b>${escapeHtml(item.currency)} ${item.amount.toLocaleString("en-US", { minimumFractionDigits: 2 })}</b></div><div><small>Location</small><b>${escapeHtml(item.city)}</b></div>
    </div></section>
    <section class="detail-section"><h3>Risk signals</h3><p>Example signals for presentation only; not generated by a live model.</p><ul class="signal-list">${item.signals.map((signal) => `<li>${escapeHtml(signal)}</li>`).join("")}</ul></section>
    <section class="detail-section"><h3>Suggested review checklist</h3><ol class="check-list">${item.actions.map((action) => `<li>${escapeHtml(action)}</li>`).join("")}</ol></section>
    <section class="detail-section"><h3>Analyst demo actions</h3><p>These actions update this browser's local demo state only.</p>
      <div class="action-row"><button data-action="IN_REVIEW">Mark in review</button><button data-action="ESCALATED" class="escalate-button">Escalate case</button><button data-action="CLOSED">Close case</button></div>
    </section>`;
  document.querySelectorAll("[data-action]").forEach((button) => button.addEventListener("click", () => {
    item.status = button.dataset.action;
    saveCases();
    updateSummary();
    renderCases();
    renderAnalytics();
    closeDetail();
    showToast(`Demo status updated: ${statusLabel(item.status)}`);
  }));
  document.querySelector("#detail-panel").classList.add("open");
  document.querySelector("#detail-panel").setAttribute("aria-hidden", "false");
  document.querySelector("#backdrop").classList.add("open");
}

function closeDetail() {
  document.querySelector("#detail-panel").classList.remove("open");
  document.querySelector("#detail-panel").setAttribute("aria-hidden", "true");
  document.querySelector("#backdrop").classList.remove("open");
}

function renderAnalytics() {
  const renderBars = (containerId, entries, colorClass) => {
    const max = Math.max(1, ...entries.map(([, count]) => count));
    document.querySelector(containerId).innerHTML = entries.map(([label, count]) => `
      <div class="bar-row ${colorClass(label)}"><span>${escapeHtml(label)}</span><div class="bar-track"><i style="width:${count / max * 100}%"></i></div><b>${count}</b></div>`).join("");
  };
  renderBars("#risk-chart", ["HIGH", "MEDIUM", "LOW"].map((risk) => [risk, cases.filter((item) => item.risk === risk).length]), (risk) => risk.toLowerCase());
  renderBars("#status-chart", ["OPEN", "IN_REVIEW", "ESCALATED", "CLOSED"].map((status) => [statusLabel(status), cases.filter((item) => item.status === status).length]), () => "");
}

function addDemoTransactions() {
  const merchants = ["Harbor Tech Shop", "Pine & Oak Market", "Nova Travel Desk", "Brightline Pharmacy", "Urban Vault"];
  const cities = ["Toronto, CA", "Austin, US", "Lisbon, PT", "Chicago, US", "Singapore, SG"];
  const categories = ["Electronics", "Groceries", "Travel", "Pharmacy", "Retail"];
  for (let index = 0; index < 3; index += 1) {
    const score = Math.round((0.16 + Math.random() * 0.82) * 100) / 100;
    const risk = score >= 0.7 ? "HIGH" : score >= 0.4 ? "MEDIUM" : "LOW";
    const serial = String(Date.now()).slice(-6) + index;
    cases.unshift({
      id: `FC-${serial}`, txn: `TXN-${Math.random().toString(16).slice(2, 8).toUpperCase()}`,
      merchant: merchants[Math.floor(Math.random() * merchants.length)],
      category: categories[Math.floor(Math.random() * categories.length)],
      amount: Math.round((20 + Math.random() * 1800) * 100) / 100, currency: "USD", score, risk,
      status: "OPEN", city: cities[Math.floor(Math.random() * cities.length)], time: "just now",
      signals: ["New illustrative sample added by the browser demo", "Signals and score are randomly generated for presentation"],
      actions: ["Review transaction context", "Verify cardholder activity if needed"],
    });
  }
  saveCases();
  updateSummary();
  renderCases();
  renderAnalytics();
  showToast("Added 3 illustrative demo transactions.");
}

function exportCsv() {
  const columns = ["case_id", "transaction_id", "merchant", "risk", "score", "status", "amount", "currency", "city"];
  const rows = cases.map((item) => [item.id, item.txn, item.merchant, item.risk, item.score, item.status, item.amount, item.currency, item.city]);
  const csv = [columns, ...rows].map((row) => row.map((value) => `"${String(value).replaceAll('"', '""')}"`).join(",")).join("\r\n");
  const link = document.createElement("a");
  link.href = URL.createObjectURL(new Blob([csv], { type: "text/csv;charset=utf-8" }));
  link.download = "fraud-copilot-demo-cases.csv";
  link.click();
  URL.revokeObjectURL(link.href);
}

function setView(view) {
  activeView = view;
  document.querySelector("#review-view").classList.toggle("hidden", view !== "review");
  document.querySelector("#analytics-view").classList.toggle("hidden", view !== "analytics");
  document.querySelector("#page-title").textContent = view === "review" ? "Review queue" : "Analytics";
  document.querySelectorAll(".nav-item").forEach((button) => button.classList.toggle("active", button.dataset.view === view));
  if (activeView === "analytics") renderAnalytics();
}

document.querySelectorAll(".nav-item").forEach((button) => button.addEventListener("click", () => setView(button.dataset.view)));
document.querySelector("#search-input").addEventListener("input", renderCases);
document.querySelector("#risk-filter").addEventListener("change", renderCases);
document.querySelector("#status-filter").addEventListener("change", renderCases);
document.querySelector("#simulate-button").addEventListener("click", addDemoTransactions);
document.querySelector("#reset-button").addEventListener("click", () => {
  cases = structuredClone(seedCases);
  saveCases();
  updateSummary();
  renderCases();
  renderAnalytics();
  showToast("Demo data reset.");
});
document.querySelector("#export-button").addEventListener("click", exportCsv);
document.querySelector("#close-detail").addEventListener("click", closeDetail);
document.querySelector("#backdrop").addEventListener("click", closeDetail);
document.addEventListener("keydown", (event) => { if (event.key === "Escape") closeDetail(); });
updateSummary();
renderCases();
renderAnalytics();

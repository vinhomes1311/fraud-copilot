/* Render stored scoring evidence without changing the model or routing. */
window.RiskReview = (() => {
  const escape = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;",
  })[char]);
  const numeric = (value) => ["number", "string"].includes(typeof value) && String(value).trim() !== ""
    && Number.isFinite(Number(value)) ? Number(value) : null;
  const number = (value, digits = 2) => numeric(value) === null ? "No data"
    : Number(value).toLocaleString("en-US", { maximumFractionDigits: digits });
  const featureNames = {
    amt: "Transaction amount", transaction_hour: "Transaction hour", hour_of_day: "Transaction hour",
    avg_amt_7d: "Average amount (previous 7 days)", avg_amt_24h: "Average amount (previous 24 hours)",
    sum_amt_24h: "Total amount (previous 24 hours)", max_amt_7d: "Maximum amount (previous 7 days)",
    std_amt_7d: "Amount variability (previous 7 days)", trans_count_1h: "Transactions in the previous hour",
    trans_count_24h: "Transactions in the previous 24 hours", txn_count_24h: "Transactions in the previous 24 hours",
    trans_count_7d: "Transactions in the previous 7 days", amt_to_avg_7d_ratio: "Amount-to-average ratio (7 days)",
    amt_to_max_7d_ratio: "Amount-to-maximum ratio (7 days)", amt_vs_avg_ratio: "Amount-to-average ratio (24 hours)",
    customer_merchant_distance_km: "Home-to-merchant distance", dist_home_merchant_km: "Home-to-merchant distance",
    time_since_last_trans_sec: "Time since previous transaction", customer_age: "Customer age",
    city_pop: "City population", transaction_dayofweek: "Day of week", transaction_month: "Transaction month",
    transaction_day: "Transaction day", is_weekend: "Weekend transaction", merchant: "Merchant", category: "Category",
  };
  const riskLabels = { HIGH: "High priority", MEDIUM: "Medium priority", LOW: "Low priority" };
  const workflowLabels = { OPEN: "Open", IN_REVIEW: "In Review", NEEDS_MORE_INFO: "Needs More Info",
    ESCALATED: "Escalated", CLOSED: "Closed", CONFIRMED_FRAUD: "Confirmed fraud",
    CONFIRMED_SIMULATED: "Confirmed Simulated", DISMISSED: "Dismissed / safe", RETURNED: "Returned" };

  function probability(value) {
    const prob = numeric(value);
    if (prob === null || prob < 0 || prob > 1) return "Not available";
    if (prob > 0 && prob < 0.00005) return "<0.01%";
    if (prob < 1 && prob >= 0.99995) return "≈100%";
    return `${number(prob * 100)}%`;
  }

  function featureValue(item, signal) {
    const features = item.features || {};
    const raw = Object.hasOwn(features, signal.feature) ? features[signal.feature]
      : Object.hasOwn(item.transaction || {}, signal.feature) ? item.transaction[signal.feature]
      : featureNames[signal.feature] && !["merchant", "category"].includes(signal.feature) ? signal.value : null;
    if (raw === null || raw === undefined || raw === "") return "Original value unavailable";
    if (["merchant", "category"].includes(signal.feature) || typeof raw === "string" && numeric(raw) === null) return String(raw);
    if (numeric(raw) === null) return "Original value unavailable";
    if (["transaction_hour", "hour_of_day"].includes(signal.feature)) {
      return Number.isInteger(Number(raw)) && raw >= 0 && raw <= 23
        ? `${String(raw).padStart(2, "0")}:00` : "Invalid hour";
    }
    if (["customer_merchant_distance_km", "dist_home_merchant_km"].includes(signal.feature)) return `${number(raw)} km`;
    if (signal.feature === "time_since_last_trans_sec") return raw < 0 ? "No previous transaction" : `${number(raw)} seconds`;
    if (signal.feature === "is_weekend") return Number(raw) === 1 ? "Yes" : "No";
    if (signal.feature === "transaction_dayofweek") return ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"][Number(raw)] || "Original value unavailable";
    return number(raw);
  }

  function signalsFor(item) {
    return (item.explanations || []).filter((signal) => signal.feature !== "model_probability"
      && numeric(signal.contribution) !== null).slice().sort((a, b) => Math.abs(b.contribution) - Math.abs(a.contribution));
  }

  function renderOverview(item) {
    const tx = item.transaction || {};
    const risk = Object.hasOwn(riskLabels, item.risk_level) ? item.risk_level : "unknown";
    const id = String(item.txn_id || "");
    const shortId = id.length > 20 ? `${id.slice(0, 8)}…${id.slice(-6)}` : id || "ID unavailable";
    return `<section class="risk-overview" aria-label="Transaction overview">
      <div class="risk-overview-status"><span class="risk risk-${risk.toLowerCase()}">${escape(riskLabels[risk] || "Risk level unavailable")}</span>
        <span class="review-workflow">${escape(workflowLabels[item.status] || item.status || "Status unavailable")}</span></div>
      <div class="transaction-amount">${number(tx.amt)}</div>
      <p class="transaction-merchant">${escape(tx.merchant || "Merchant unavailable")}</p>
      <span class="transaction-category">${escape(tx.category || "Category unavailable")}</span>
      <div class="risk-estimate"><span>Fraud probability</span><strong>${probability(item.calibrated_prob)}</strong></div>
      <p class="model-context">This is decision support, not a final fraud determination.</p>
      <div class="transaction-reference"><span>Transaction ID</span><code title="${escape(id)}">${escape(shortId)}</code>
        <button type="button" class="copy-transaction-id" aria-label="Copy full transaction ID" title="Copy full transaction ID">Copy</button></div>
    </section>`;
  }

  function renderSignals(item) {
    const signals = signalsFor(item);
    const groups = [
      { title: "Factors increasing risk score", name: "increase", accepts: (v) => v > 0 },
      { title: "Factors decreasing risk score", name: "decrease", accepts: (v) => v < 0 },
      { title: "Factors with no score impact", name: "neutral", accepts: (v) => v === 0 },
    ];
    const content = groups.map((group) => {
      const rows = signals.filter((signal) => group.accepts(Number(signal.contribution)));
      if (!rows.length) return "";
      return `<section class="risk-factor-group"><h4>${group.title}</h4><ul class="risk-factors">${rows.map((signal, index) =>
        `<li class="risk-factor ${group.name}"><span class="factor-rank" aria-hidden="true">${String(index + 1).padStart(2, "0")}</span>
        <div><span class="factor-name" title="${escape(signal.feature)}">${escape(featureNames[signal.feature] || signal.feature)}</span>
        <span class="factor-value">${escape(featureValue(item, signal))}</span></div>
        <span class="factor-impact">${group.name === "neutral" ? "No change" : index === 0 ? "Highest impact" : "Lower impact"}</span></li>`
      ).join("")}</ul></section>`;
    }).join("");
    return `<div class="detail-section risk-explanation">${content || '<h4>Model explanation</h4><p class="section-note">No explanation signals available.</p>'}
      ${signals.length ? '<p class="factor-context">Relative impact among the returned signals; contributions are not percentages.</p>' : ""}</div>`;
  }

  function renderTechnicalDetails(item) {
    const rows = signalsFor(item).map((signal) => `<tr><th scope="row">${escape(signal.feature)}</th><td>${Number(signal.contribution) > 0 ? "+" : ""}${number(signal.contribution, 4)}</td></tr>`).join("");
    return `<details class="model-details"><summary>Model details</summary><div class="model-details-content">
      ${rows ? `<table class="contribution-table"><thead><tr><th>Feature</th><th>Contribution</th></tr></thead><tbody>${rows}</tbody></table>` : ""}
      <dl><div><dt>Raw model score</dt><dd>${number(item.raw_model_score, 5)}</dd></div>
      <div><dt>Model version</dt><dd>${escape(item.model_version || "No data")}</dd></div>
      <div><dt>Threshold config</dt><dd>${escape(item.threshold_config_version ?? "No data")}</dd></div>
      <div><dt>Brief status</dt><dd>${escape(({ PENDING: "Pending", RUNNING: "Running", READY: "Ready", FAILED: "Failed", FAILED_TIMEOUT: "Failed Timeout" })[item.brief_status] || item.brief_status || (item.risk_level === "LOW" ? "Not generated automatically" : "Not generated"))}</dd></div>
      <div><dt>Transaction ID</dt><dd>${escape(item.txn_id)}</dd></div></dl></div></details>`;
  }

  return Object.freeze({ probability, featureValue, renderOverview, renderSignals, renderTechnicalDetails });
})();

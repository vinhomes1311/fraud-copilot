/* Render stored scoring evidence without changing the model or routing. */
window.RiskReview = (() => {
  const escape = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;",
  })[char]);
  const numeric = (value) => ["number", "string"].includes(typeof value) && String(value).trim() !== ""
    && Number.isFinite(Number(value)) ? Number(value) : null;
  const number = (value, digits = 2) => numeric(value) === null ? "Chưa có dữ liệu"
    : Number(value).toLocaleString("vi-VN", { maximumFractionDigits: digits });
  const featureNames = {
    amt: "Số tiền giao dịch", transaction_hour: "Giờ giao dịch", hour_of_day: "Giờ giao dịch",
    avg_amt_7d: "Số tiền trung bình 7 ngày trước", avg_amt_24h: "Số tiền trung bình 24 giờ trước",
    sum_amt_24h: "Tổng chi tiêu 24 giờ trước", max_amt_7d: "Số tiền lớn nhất 7 ngày trước",
    std_amt_7d: "Độ biến động số tiền trong 7 ngày", trans_count_1h: "Số giao dịch trong giờ trước",
    trans_count_24h: "Số giao dịch trong 24 giờ trước", txn_count_24h: "Số giao dịch trong 24 giờ trước",
    trans_count_7d: "Số giao dịch trong 7 ngày trước", amt_to_avg_7d_ratio: "Tỷ lệ so với trung bình 7 ngày",
    amt_to_max_7d_ratio: "Tỷ lệ so với mức cao nhất 7 ngày", amt_vs_avg_ratio: "Tỷ lệ so với trung bình 24 giờ",
    customer_merchant_distance_km: "Khoảng cách nhà – merchant", dist_home_merchant_km: "Khoảng cách nhà – merchant",
    time_since_last_trans_sec: "Thời gian từ giao dịch trước", customer_age: "Tuổi khách hàng",
    city_pop: "Dân số khu vực", transaction_dayofweek: "Thứ trong tuần", transaction_month: "Tháng giao dịch",
    transaction_day: "Ngày giao dịch", is_weekend: "Giao dịch cuối tuần", merchant: "Merchant", category: "Danh mục",
  };
  const riskLabels = { HIGH: "Rủi ro cao", MEDIUM: "Rủi ro trung bình", LOW: "Rủi ro thấp" };
  const workflowLabels = { OPEN: "Chờ xem xét", IN_REVIEW: "Đang xem xét", NEEDS_MORE_INFO: "Cần bổ sung thông tin",
    ESCALATED: "Đã chuyển cấp", CLOSED: "Đã đóng", CONFIRMED_FRAUD: "Đã xác nhận gian lận",
    CONFIRMED_SIMULATED: "Đã xác nhận mô phỏng", DISMISSED: "Đã loại nghi vấn", RETURNED: "Cần xem xét lại" };

  function probability(value) {
    const prob = numeric(value);
    if (prob === null || prob < 0 || prob > 1) return "Chưa có điểm";
    if (prob > 0 && prob < 0.00005) return "<0,01%";
    if (prob < 1 && prob >= 0.99995) return "≈100%";
    return `${number(prob * 100)}%`;
  }

  function featureValue(item, signal) {
    const features = item.features || {};
    const raw = Object.hasOwn(features, signal.feature) ? features[signal.feature]
      : Object.hasOwn(item.transaction || {}, signal.feature) ? item.transaction[signal.feature]
      : featureNames[signal.feature] && !["merchant", "category"].includes(signal.feature) ? signal.value : null;
    if (raw === null || raw === undefined || raw === "") return "Chưa có giá trị gốc";
    if (["merchant", "category"].includes(signal.feature) || typeof raw === "string" && numeric(raw) === null) return String(raw);
    if (numeric(raw) === null) return "Chưa có giá trị gốc";
    if (["transaction_hour", "hour_of_day"].includes(signal.feature)) {
      return Number.isInteger(Number(raw)) && raw >= 0 && raw <= 23
        ? `${String(raw).padStart(2, "0")}:00 · giờ ghi nhận, chưa có múi giờ` : "Giờ không hợp lệ";
    }
    if (["customer_merchant_distance_km", "dist_home_merchant_km"].includes(signal.feature)) return `${number(raw)} km`;
    if (signal.feature === "time_since_last_trans_sec") return raw < 0 ? "Chưa có giao dịch trước" : `${number(raw)} giây`;
    if (signal.feature === "is_weekend") return Number(raw) === 1 ? "Có" : "Không";
    if (signal.feature === "transaction_dayofweek") return ["Thứ hai", "Thứ ba", "Thứ tư", "Thứ năm", "Thứ sáu", "Thứ bảy", "Chủ nhật"][Number(raw)] || "Chưa có giá trị gốc";
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
    const shortId = id.length > 20 ? `${id.slice(0, 8)}…${id.slice(-6)}` : id || "Chưa có mã";
    return `<section class="risk-overview" aria-label="Tổng quan giao dịch">
      <div class="risk-overview-status"><span class="risk risk-${risk.toLowerCase()}">${escape(riskLabels[risk] || "Chưa có mức rủi ro")}</span>
        <span class="review-workflow">${escape(workflowLabels[item.status] || item.status || "Chưa có trạng thái")}</span></div>
      <div class="transaction-amount">${number(tx.amt)}</div>
      <p class="currency-note">Đơn vị tiền tệ chưa được cung cấp</p>
      <p class="transaction-merchant">${escape(tx.merchant || "Chưa có merchant")}</p>
      <span class="transaction-category">${escape(tx.category || "Chưa có danh mục")}</span>
      <div class="risk-estimate"><span>Xác suất theo mô hình</span><strong>${probability(item.calibrated_prob)}</strong></div>
      <p class="model-context">Dự đoán hỗ trợ xem xét; không thay thế kết luận của chuyên viên.</p>
      <div class="transaction-reference"><span>Mã giao dịch</span><code title="${escape(id)}">${escape(shortId)}</code>
        <button type="button" class="copy-transaction-id" aria-label="Sao chép mã giao dịch đầy đủ" title="Sao chép mã giao dịch đầy đủ">Sao chép</button></div>
    </section>`;
  }

  function renderSignals(item) {
    const signals = signalsFor(item);
    const groups = [
      { title: "Yếu tố làm tăng điểm rủi ro", name: "increase", accepts: (v) => v > 0 },
      { title: "Yếu tố làm giảm điểm rủi ro", name: "decrease", accepts: (v) => v < 0 },
      { title: "Yếu tố không làm thay đổi điểm", name: "neutral", accepts: (v) => v === 0 },
    ];
    const content = groups.map((group) => {
      const rows = signals.filter((signal) => group.accepts(Number(signal.contribution)));
      if (!rows.length) return "";
      return `<section class="risk-factor-group"><h4>${group.title}</h4><ul class="risk-factors">${rows.map((signal, index) =>
        `<li class="risk-factor ${group.name}"><span class="factor-rank" aria-hidden="true">${String(index + 1).padStart(2, "0")}</span>
        <div><span class="factor-name" title="${escape(signal.feature)}">${escape(featureNames[signal.feature] || signal.feature)}</span>
        <span class="factor-value">${escape(featureValue(item, signal))}</span></div>
        <span class="factor-impact">${group.name === "neutral" ? "Không đổi" : index === 0 ? "Tác động lớn nhất" : "Tác động tiếp theo"}</span></li>`
      ).join("")}</ul></section>`;
    }).join("");
    return `<div class="detail-section risk-explanation">${content || '<h4>Tín hiệu mô hình</h4><p class="section-note">Chưa có giải thích theo đặc trưng.</p>'}
      ${signals.length ? '<p class="factor-context">Thứ tự tương đối trong các tín hiệu được trả về, không phải toàn bộ mô hình. Mức đóng góp không phải phần trăm.</p>' : ""}</div>`;
  }

  function renderReviewContext(item) {
    const names = new Set(signalsFor(item).map((signal) => signal.feature));
    const points = [];
    if (names.has("amt") || [...names].some((name) => name.includes("ratio"))) points.push("Đối chiếu số tiền với lịch sử chi tiêu có sẵn; chỉ so sánh khi có baseline hợp lệ.");
    if (names.has("transaction_hour") || names.has("hour_of_day")) points.push("Đối chiếu giờ ghi nhận và thói quen giao dịch; cần xác nhận múi giờ trước khi diễn giải.");
    if ([...names].some((name) => /7d|24h|1h/.test(name))) points.push("Kiểm tra độ đầy đủ của lịch sử và đúng cửa sổ thời gian dùng để tính đặc trưng.");
    if (!points.length) return "";
    return `<section class="detail-section review-context"><h4>Nội dung cần đối chiếu</h4><ul>${points.map((point) => `<li>${point}</li>`).join("")}</ul></section>`;
  }

  function renderTechnicalDetails(item) {
    const rows = signalsFor(item).map((signal) => `<tr><th scope="row">${escape(signal.feature)}</th><td>${Number(signal.contribution) > 0 ? "+" : ""}${number(signal.contribution, 4)}</td></tr>`).join("");
    return `<details class="model-details"><summary>Chi tiết mô hình</summary><div class="model-details-content">
      ${rows ? `<table class="contribution-table"><thead><tr><th>Đặc trưng</th><th>Đóng góp</th></tr></thead><tbody>${rows}</tbody></table>` : ""}
      <dl><div><dt>Raw model score</dt><dd>${number(item.raw_model_score, 5)}</dd></div>
      <div><dt>Phiên bản model</dt><dd>${escape(item.model_version || "Chưa có dữ liệu")}</dd></div>
      <div><dt>Phiên bản ngưỡng</dt><dd>${escape(item.threshold_config_version ?? "Chưa có dữ liệu")}</dd></div>
      <div><dt>Trạng thái brief</dt><dd>${escape(({ PENDING: "Đang chờ", RUNNING: "Đang tạo", READY: "Sẵn sàng", FAILED: "Tạo brief thất bại", FAILED_TIMEOUT: "Quá thời gian tạo brief" })[item.brief_status] || item.brief_status || (item.risk_level === "LOW" ? "Không tạo tự động" : "Chưa có brief"))}</dd></div>
      <div><dt>Mã giao dịch đầy đủ</dt><dd>${escape(item.txn_id)}</dd></div></dl></div></details>`;
  }

  return Object.freeze({ probability, featureValue, renderOverview, renderSignals, renderReviewContext, renderTechnicalDetails });
})();

const assert = require('node:assert/strict');
const { test } = require('node:test');
const { readFileSync } = require('node:fs');
const { resolve } = require('node:path');
const vm = require('node:vm');

const root = resolve(__dirname, '../..');
const context = { window: {} };
vm.runInNewContext(readFileSync(resolve(root, 'static/risk-review.js'), 'utf8'), context);
const view = context.window.RiskReview;
const example = {
  txn_id: 'ab4b379d2c0c9c667d46508d4e126d72', risk_level: 'HIGH', status: 'OPEN',
  calibrated_prob: 1, raw_model_score: 0.99997, model_version: 'xgb-test', threshold_config_version: 1,
  transaction: { amt: 780.52, merchant: 'fraud_Rodriguez, Yost and Jenkins', category: 'misc_net' },
  features: { amt: 780.52, transaction_hour: 23, avg_amt_7d: 125.4 },
  explanations: [
    { feature: 'amt', value: 780.52, contribution: 6.31 },
    { feature: 'transaction_hour', value: 23, contribution: 2.41 },
    { feature: 'avg_amt_7d', value: 125.4, contribution: 1.78 },
  ],
};

test('missing or invalid probability is not presented as zero; rounding stays explicit', () => {
  for (const value of [null, undefined, '', ' ', false, [], {}, NaN, Infinity, -1, 2]) {
    assert.equal(view.probability(value), 'Chưa có điểm');
  }
  assert.equal(view.probability(0), '0%');
  assert.equal(view.probability(1), '100%');
  assert.equal(view.probability(0.999999), '≈100%');
  assert.equal(view.probability(0.000001), '<0,01%');
});

test('overview uses local number formatting, retains copy target and invents no currency', () => {
  const html = view.renderOverview(example);
  assert.match(html, /780,52/);
  assert.match(html, /Rủi ro cao/);
  assert.match(html, /Chờ xem xét/);
  assert.match(html, /ab4b379d…126d72/);
  assert.match(html, /Đơn vị tiền tệ chưa được cung cấp/);
  assert.doesNotMatch(html, /USD|VND|₫|\$/);
});

test('positive, negative and zero effects stay separate without modifying stored evidence', () => {
  const item = { ...example, explanations: [
    { feature: 'amt', value: 780.52, contribution: -6.31 },
    { feature: 'transaction_hour', value: 23, contribution: 2.41 },
    { feature: 'avg_amt_7d', value: 125.4, contribution: 0 },
  ] };
  const before = JSON.stringify(item);
  const html = view.renderSignals(item);
  assert.match(html, /làm tăng điểm rủi ro/);
  assert.match(html, /làm giảm điểm rủi ro/);
  assert.match(html, /không làm thay đổi điểm/);
  assert.match(html, /Số tiền giao dịch/);
  assert.doesNotMatch(html, /6,31|2,41/);
  assert.match(view.renderTechnicalDetails(item), /-6,31/);
  assert.match(view.renderTechnicalDetails(item), /\+2,41/);
  assert.equal(JSON.stringify(item), before);
});

test('original feature values take precedence and categorical encoding is never shown as fact', () => {
  assert.match(view.featureValue(example, { feature: 'transaction_hour', value: 1 }), /23:00/);
  assert.equal(view.featureValue({ transaction: {}, features: { avg_amt_7d: null } },
    { feature: 'avg_amt_7d', value: 999 }), 'Chưa có giá trị gốc');
  assert.equal(view.featureValue({ transaction: {}, features: {} },
    { feature: 'merchant', value: 1 }), 'Chưa có giá trị gốc');
  assert.equal(view.featureValue({ transaction: {}, features: { trans_count_7d: 0 } },
    { feature: 'trans_count_7d', value: 99 }), '0');
});

test('untrusted text is escaped across overview, features and model details', () => {
  const attack = '<img src=x onerror="alert(1)">';
  const item = { ...example, txn_id: attack, model_version: attack,
    transaction: { amt: null, merchant: attack, category: attack },
    explanations: [{ feature: attack, value: attack, contribution: 1 }], features: { [attack]: attack } };
  const html = view.renderOverview(item) + view.renderSignals(item) + view.renderTechnicalDetails(item);
  assert.doesNotMatch(html, /<img|onerror="/);
  assert.match(html, /&lt;img/);
  assert.match(html, /Chưa có dữ liệu/);
});

test('fallback score is not presented as an attributed risk factor', () => {
  const html = view.renderSignals({ ...example, explanations: [
    { feature: 'model_probability', value: .9, contribution: .9 },
    { feature: 'amt', value: 10, contribution: null },
  ] });
  assert.match(html, /Chưa có giải thích theo đặc trưng/);
  assert.doesNotMatch(html, /làm tăng/);
});

test('context proposes checks without claiming an anomaly or using missing comparisons', () => {
  const html = view.renderReviewContext(example);
  assert.match(html, /chỉ so sánh khi có baseline hợp lệ/);
  assert.match(html, /cần xác nhận múi giờ/);
  assert.doesNotMatch(html, /bất thường|vượt|gian lận|ban đêm/);
});

test('original summary, signals and citations remain available under expandable sections', () => {
  const app = readFileSync(resolve(root, 'static/app.js'), 'utf8');
  const functionText = app.slice(app.indexOf('function renderBrief('), app.indexOf('function renderEvidence('));
  const escapeHtml = (value) => String(value ?? '').replaceAll('<', '&lt;').replaceAll('>', '&gt;');
  const render = vm.runInNewContext(`${functionText}; renderBrief`, { escapeHtml });
  const html = render({ summary: 'Original summary', key_signals: ['amt đóng góp 6.31'],
    checklist: [{ action: 'Review evidence', rule_code: 'PB-01' }],
    citations: [{ rule_code: 'PB-01', quote: 'Approved reference' }] }, 'HIGH');
  assert.match(html, /<details[^>]*><summary>Bản tóm tắt gốc/);
  assert.match(html, /Original summary/);
  assert.match(html, /amt đóng góp 6.31/);
  assert.match(html, /Review evidence/);
  assert.match(html, /Approved reference/);
  assert.match(render(null, 'LOW'), /LOW không tự tạo brief/);
});

test('case drawer integrates the new sections and copies the full ID without changing server data', async () => {
  const elements = new Map();
  function element(selector) {
    if (!elements.has(selector)) {
      const classes = new Set();
      elements.set(selector, { innerHTML: '', textContent: '', value: '', handlers: {},
        addEventListener(name, callback) { this.handlers[name] = callback; },
        classList: { add: (name) => classes.add(name), remove: (name) => classes.delete(name),
          contains: (name) => classes.has(name) } });
    }
    return elements.get(selector);
  }
  let copied;
  const requests = [];
  const sandbox = {
    window: {}, document: { querySelector: element, querySelectorAll: () => [] },
    localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    setTimeout: () => 0, clearInterval() {},
    navigator: { clipboard: { async writeText(value) { copied = value; } } },
    fetch: async (path, options) => { requests.push({ path, options });
      return { ok: true, status: 200, json: async () => ({ ...example, case_id: 101,
        actions: [], brief: null }) }; },
  };
  vm.createContext(sandbox);
  vm.runInContext(readFileSync(resolve(root, 'static/risk-review.js'), 'utf8'), sandbox);
  vm.runInContext(readFileSync(resolve(root, 'static/app.js'), 'utf8'), sandbox);
  await sandbox.openDetail(101);
  const html = element('#detail-content').innerHTML;
  assert.match(html, /Tổng quan giao dịch/);
  assert.match(html, /Nội dung cần đối chiếu/);
  assert.match(html, /Chi tiết mô hình/);
  assert.match(html, /data-action="CONFIRMED_FRAUD"/);
  assert.ok(element('#detail-panel').classList.contains('open'));
  await element('#detail-content .copy-transaction-id').handlers.click();
  assert.equal(copied, example.txn_id);
  assert.equal(requests.length, 1);
  assert.equal(requests[0].path, '/api/v1/cases/101');
  assert.equal(requests[0].options.method, undefined);
});

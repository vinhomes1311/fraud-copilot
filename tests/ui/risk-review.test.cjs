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
    assert.equal(view.probability(value), 'Not available');
  }
  assert.equal(view.probability(0), '0%');
  assert.equal(view.probability(1), '100%');
  assert.equal(view.probability(0.999999), '>99.99%');
  assert.equal(view.probability(0.000001), '<0.01%');
});

test('overview uses dashboard number formatting, retains copy target and invents no currency', () => {
  const html = view.renderOverview(example);
  assert.match(html, /780.52/);
  assert.match(html, /High priority/);
  assert.match(html, /Open/);
  assert.match(html, /ab4b379d…126d72/);
  assert.doesNotMatch(html, /Currency not provided/);
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
  assert.match(html, /Factors increasing risk score/);
  assert.match(html, /Factors decreasing risk score/);
  assert.match(html, /Factors with no score impact/);
  assert.match(html, /Transaction amount/);
  assert.doesNotMatch(html, /6.31|2.41/);
  assert.match(view.renderTechnicalDetails(item), /-6.31/);
  assert.doesNotMatch(view.renderTechnicalDetails(item), /Đóng góp vào điểm mô hình|Dấu dương làm tăng|Contributions to the model score/);
  assert.match(view.renderTechnicalDetails(item), /\+2.41/);
  assert.equal(JSON.stringify(item), before);
});

test('original feature values take precedence and categorical encoding is never shown as fact', () => {
  assert.equal(view.featureValue(example, { feature: 'transaction_hour', value: 1 }), '23:00');
  assert.equal(view.featureValue({ transaction: {}, features: { avg_amt_7d: null } },
    { feature: 'avg_amt_7d', value: 999 }), 'Original value unavailable');
  assert.equal(view.featureValue({ transaction: {}, features: {} },
    { feature: 'merchant', value: 1 }), 'Original value unavailable');
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
  assert.match(html, /No data/);
});

test('fallback score is not presented as an attributed risk factor', () => {
  const html = view.renderSignals({ ...example, explanations: [
    { feature: 'model_probability', value: .9, contribution: .9 },
    { feature: 'amt', value: 10, contribution: null },
  ] });
  assert.match(html, /No explanation signals available/);
  assert.doesNotMatch(html, /làm tăng/);
});

test('impact annotations use English labels consistent with the main dashboard', () => {
  const html = view.renderSignals(example);
  assert.match(html, /Highest impact/);
  assert.match(html, /Lower impact/);
  assert.doesNotMatch(html, /Tác động lớn nhất|Tác động tiếp theo/);
});

test('readable factors show reason, share of evidence and probability without the factor', () => {
  const item = { ...example, calibrated_prob: 0.93, explanations: [
    { feature: 'amt', value: 780.52, contribution: 2.1, label: 'Transaction amount', display_value: '780.52',
      reason: "780.52 is 6.2× this card's average of 125.40", share_pct: 41.3, probability_without: 0.412 },
    { feature: 'merchant', value: 1, contribution: -0.4, label: 'Merchant', display_value: 'fraud_Kub-Smith',
      reason: '<b>learned</b> from past transactions', share_pct: 7.9, probability_without: 0.95 },
  ] };
  const html = view.renderSignals(item);
  assert.match(html, /6\.2× this card&#039;s average/);
  assert.match(html, /41\.3% of model evidence/);
  assert.match(html, /Without it: 41\.2%/);
  assert.match(html, /fraud_Kub-Smith/);
  assert.doesNotMatch(html, /Original value unavailable|<b>learned/);
  assert.match(html, /&lt;b&gt;learned/);
  assert.match(html, /Share of model evidence/);
  assert.doesNotMatch(html, /Highest impact/);
  assert.match(view.renderTechnicalDetails(item), /41\.3%/);
});

test('checklist retains rule codes without repeating references or the original summary', () => {
  const app = readFileSync(resolve(root, 'static/app.js'), 'utf8');
  const functionText = app.slice(app.indexOf('function renderBrief('), app.indexOf('function renderEvidence('));
  const escapeHtml = (value) => String(value ?? '').replaceAll('<', '&lt;').replaceAll('>', '&gt;');
  const render = vm.runInNewContext(`${functionText}; renderBrief`, { escapeHtml });
  const html = render({ summary: 'Original summary', key_signals: ['amt đóng góp 6.31'],
    checklist: [{ action: 'Review evidence', rule_code: 'PB-01' }],
    citations: [{ rule_code: 'PB-01', quote: 'Approved reference' }] }, 'HIGH');
  assert.doesNotMatch(html, /Bản tóm tắt gốc|Original summary|amt đóng góp 6.31/);
  assert.doesNotMatch(render('Legacy summary', 'HIGH'), /Legacy summary/);
  assert.match(html, /Review evidence/);
  assert.match(html, /PB-01/);
  assert.doesNotMatch(html, /References|Approved reference/);
  assert.match(render(null, 'LOW'), /Low-risk cases do not generate briefs automatically/);
});

test('brief shows generated explanations with supporting facts and escapes generated text', () => {
  const app = readFileSync(resolve(root, 'static/app.js'), 'utf8');
  const functionText = app.slice(app.indexOf('function renderBrief('), app.indexOf('function renderEvidence('));
  const escapeHtml = (value) => String(value ?? '').replaceAll('<', '&lt;').replaceAll('>', '&gt;');
  const render = vm.runInNewContext(`${functionText}; renderBrief`, { escapeHtml });
  const html = render({
    key_signals: ['Transaction amount: 1500.'],
    checklist: [{action: 'Average transaction amount: 100.', rule_code: 'PB-08',
      rationale: 'The current amount exceeds the recent average. <img src=x onerror=alert()>',
      evidence_ids: ['EV-AMOUNT']}],
    citations: [{quote: 'Internal playbook instructions'}],
  }, 'HIGH');
  assert.match(html, /The current amount exceeds the recent average/);
  assert.match(html, /Supporting case facts/);
  assert.match(html, /Transaction amount: 1500/);
  assert.match(html, /&lt;img/);
  assert.doesNotMatch(html, /<img|EV-AMOUNT|Internal playbook instructions/);
});

test('demo reset asks for confirmation and restarts replay through the reset endpoint', async () => {
  const app = readFileSync(resolve(root, 'static/app.js'), 'utf8');
  const functionText = app.slice(app.indexOf('async function resetDemoData('), app.indexOf('\nasync function syncSimulatorState('));
  const button = { disabled: false };
  const calls = [];
  const messages = [];
  const state = { page: 4, lastSimulatorViewRefresh: 0 };
  const run = vm.runInNewContext(`${functionText}; resetDemoData`, {
    window: { confirm: () => true },
    state,
    $: (selector) => {
      assert.equal(selector, '#reset-demo-data');
      return button;
    },
    api: async (path, options) => {
      calls.push({ path, options });
      return { deleted_transactions: 3, deleted_cases: 2, offset: 0, running: true };
    },
    applySimulatorState: (result) => calls.push({ applied: result }),
    loadCurrentView: () => calls.push({ refreshed: true }),
    showToast: (message) => messages.push(message),
    Date,
  });

  await run();

  assert.equal(button.disabled, false);
  assert.deepEqual(calls.map((call) => call.path || (call.refreshed ? 'refresh' : 'apply')), [
    '/demo/reset', 'apply', 'refresh',
  ]);
  assert.equal(calls[0].options.method, 'POST');
  assert.equal(state.page, 0);
  assert.equal(messages[0], 'Deleted 3 demo transactions and 2 cases; replay restarted');
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
    window: { addEventListener() {} },
    document: { querySelector: element, querySelectorAll: () => [] },
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
  assert.match(html, /Transaction overview/);
  assert.doesNotMatch(html, /Nội dung cần đối chiếu|Đối chiếu giờ ghi nhận|Kiểm tra độ đầy đủ của lịch sử/);
  assert.match(html, /Model details/);
  assert.match(html, /Transaction details/);
  assert.doesNotMatch(html, /Dữ liệu giao dịch đã che/);
  assert.match(html, /data-action="CONFIRMED_FRAUD"/);
  assert.ok(element('#detail-panel').classList.contains('open'));
  await element('#detail-content .copy-transaction-id').handlers.click();
  assert.equal(copied, example.txn_id);
  assert.equal(requests.length, 1);
  assert.equal(requests[0].path, '/api/v1/cases/101');
  assert.equal(requests[0].options.method, undefined);
});

test('model details show input data, prompt and playbook versions for traceability', () => {
  const html = view.renderTechnicalDetails({ ...example, data_version: 'fraudTest.csv@sha256:abc123',
    trace_id: 'trace-<1>', brief_status: 'READY', brief_model: 'gemini', prompt_version: 'v11',
    playbook_version: 'common-playbook@2.1#sha256:def' });
  assert.match(html, /Input data version<\/dt><dd>fraudTest\.csv@sha256:abc123/);
  assert.match(html, /Playbook version<\/dt><dd>common-playbook@2\.1#sha256:def/);
  assert.match(html, /gemini · v11/);
  assert.match(html, /trace-&lt;1&gt;/);
  const legacy = view.renderTechnicalDetails(example);
  assert.match(legacy, /Input data version<\/dt><dd>Not recorded/);
  assert.doesNotMatch(legacy, /Playbook version/);
});

test('drift panel ranks features, shows status and escapes server text', () => {
  const app = readFileSync(resolve(root, 'static/app.js'), 'utf8');
  const functionText = app.slice(app.indexOf('const DRIFT_REFRESH_MS'), app.indexOf('async function loadDrift('));
  const elements = {};
  const $ = (selector) => (elements[selector] ||= { innerHTML: '', textContent: '', className: '' });
  const escapeHtml = (value) => String(value ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;' })[c]);
  const render = vm.runInNewContext(`${functionText}; renderDrift`, { $, escapeHtml, Intl, Number, Math });
  render({
    overall_status: 'significant', model_version: 'xgb-<b>', thresholds: { min_rows: 200 },
    window: { scored_rows: 5000 }, baseline: { reference: 'validation', rows: 194502 },
    score: { psi: 0.04, status: 'stable' }, review_rate: { expected_rate: 0.01, actual_rate: 0.025 },
    data_quality: { unscored_rate: 0.002, unscored: 10, rows: 5010 },
    features: [
      { feature: 'trans_count_7d', label: 'Transactions in the previous 7 days', psi: 3.1, status: 'significant',
        missing_rate: 0, top_shift: { bin: '<= 9', expected: 0.11, actual: 1 } },
      { feature: 'amt', label: '<img src=x>', psi: 0.02, status: 'stable', missing_rate: 0.05, top_shift: null },
    ],
  });
  const html = elements['#dashboard-drift'].innerHTML;
  assert.equal(elements['#dashboard-drift-status'].textContent, 'Significant drift');
  assert.match(elements['#dashboard-drift-status'].className, /drift-significant/);
  assert.match(html, /Transactions in the previous 7 days/);
  assert.match(html, /&lt;= 9 · 11% → 100%/);
  assert.match(html, /1% → 2\.5%/);
  assert.match(html, /missing 5%/);
  assert.match(html, /xgb-&lt;b&gt;/);
  assert.doesNotMatch(html, /<img|xgb-<b>/);
  assert.ok(html.indexOf('Transactions in the previous 7 days') < html.indexOf('&lt;img'));

  render({ overall_status: 'insufficient_data', window: { scored_rows: 0 }, thresholds: { min_rows: 200 } });
  assert.match(elements['#dashboard-drift'].innerHTML, /No scored transactions yet/);
  assert.equal(elements['#dashboard-drift-status'].textContent, 'Not enough data');
});

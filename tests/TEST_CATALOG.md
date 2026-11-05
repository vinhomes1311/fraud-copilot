# Danh sách kiểm thử Fraud Copilot

Tài liệu này liệt kê các test hiện có trong thư mục `tests/`. Một test được
đánh dấu **passed** khi chạy hết hàm mà không có assertion sai hoặc exception
ngoài dự kiến. Test được đánh dấu **failed** khi một điều kiện kiểm tra không
đúng. Test **skipped** là test chưa chạy vì môi trường bắt buộc chưa sẵn sàng,
không phải là test passed.

## Tổng quan

- **25 file test pytest** và **1 file test JavaScript**, được chia theo các job CI.
- **127 hàm test**.
- Có kiểm thử unit, integration, SQL lifecycle, queue, ML và E2E.
- Kết quả ghi nhận trước khi bổ sung test PR #5: `54 passed, 2 skipped, 1 warning`.
- Các test parameterized trong `test_logic.py` chạy nhiều bộ dữ liệu khác nhau.

## `unit_integration/test_logic.py`

Kiểm thử validation, routing, bảo mật PII và circuit breaker bằng dữ liệu trong
bộ nhớ, không cần Redis hoặc PostgreSQL.

| Test | Nội dung | Đúng khi | Sai khi |
|---|---|---|---|
| `test_routing_thresholds` | Kiểm tra phân loại LOW, MEDIUM, HIGH theo ngưỡng xác suất. | `0.30` là MEDIUM, `0.70` là HIGH và các giá trị biên được phân loại đúng. | Một xác suất bị route sang risk level sai. |
| `test_validation_ok` | Kiểm tra transaction hợp lệ. | Dữ liệu mẫu trả về `ok=True`. | Transaction hợp lệ bị từ chối. |
| `test_validation_rejects_numeric_boundary_values` | Kiểm tra `amt`, `lat`, `long` nằm ngoài miền cho phép. | Các giá trị ngoài biên đều bị từ chối. | Giá trị ngoài biên vẫn được chấp nhận. |
| `test_validation_accepts_numeric_boundary_values` | Kiểm tra các giá trị min/max hợp lệ. | Các giá trị biên hợp lệ đều được chấp nhận. | Giá trị biên hợp lệ bị từ chối. |
| `test_validation_rejects_bad_data` | Kiểm tra dữ liệu sai kiểu, sai miền hoặc merchant rỗng. | Validator trả `ok=False` và có lỗi. | Dữ liệu sai vẫn được chấp nhận. |
| `test_validation_missing_field` | Kiểm tra thiếu trường bắt buộc. | Thiếu `cc_num` bị từ chối. | Transaction thiếu trường vẫn được chấp nhận. |
| `test_pii_masking` | Kiểm tra che số thẻ, tên và nhãn nhạy cảm. | PII bị loại bỏ, số thẻ được mask và account hash dài 32 ký tự. | PII còn lộ hoặc giá trị mask/hash sai. |
| `test_circuit_breaker_opens_then_blocks` | Kiểm tra circuit breaker mở sau đủ số lỗi. | Sau 2 lỗi, request tiếp theo nhận `CircuitOpenError`. | Breaker không mở hoặc vẫn cho request đi qua. |

`test_validation_rejects_numeric_boundary_values` có 6 trường hợp và
`test_validation_accepts_numeric_boundary_values` có 6 trường hợp.
`test_validation_rejects_bad_data` có 4 trường hợp.

## `ml_evaluation/test_ml_pipeline.py`

Kiểm thử tạo feature và chia dữ liệu theo thời gian.

| Test | Nội dung | Đúng khi | Sai khi |
|---|---|---|---|
| `test_extract_features_is_temporal_and_removes_pii` | Kiểm tra feature lịch sử và loại PII. | `cc_num` bị loại, giao dịch đầu có count bằng 0 và giao dịch sau có count bằng 1. | PII còn lộ hoặc count lịch sử sai. |
| `test_chronological_split_preserves_time_order` | Kiểm tra chia train/validation/test theo thời gian. | Mốc cuối train trước validation và mốc cuối validation trước test. | Dữ liệu bị xáo trộn gây leakage. |
| `test_schema_rejects_missing_timestamp` | Kiểm tra timestamp là trường bắt buộc. | Thiếu timestamp phát sinh `ValueError`. | Dataframe thiếu timestamp vẫn qua schema. |

## `unit_integration/test_local_integration.py`

Kiểm thử local RAG/LLM contract bằng Ollama request giả và dữ liệu mock.

| Test | Nội dung | Đúng khi | Sai khi |
|---|---|---|---|
| `test_projection_privacy_and_history` | Kiểm tra mask PII và evidence lịch sử. | Evidence không chứa `SECRET`, không tạo feature thiếu dữ liệu và có `EV-HISTORY-GAP`. | PII còn xuất hiện hoặc evidence sai/thiếu. |
| `test_average_is_not_median` | Kiểm tra `avg_amt_24h` là trung bình, không phải median. | Evidence có feature trung bình và không diễn giải là median. | Feature bị diễn giải nhầm là median. |
| `test_generated_contract_and_unchanged_scores` | Kiểm tra payload gửi LLM, citation và score gốc. | Payload không có PII, context không đổi, token/citation đúng. | Payload lộ PII, context bị đổi hoặc kết quả sai. |
| `test_timeout_propagates` | Kiểm tra timeout Ollama được truyền lên caller. | `TimeoutError` xuất hiện đúng trong `assertRaises`. | Timeout bị nuốt hoặc trả kết quả giả. |
| `test_invalid_citation_rejected` | Kiểm tra citation không tồn tại bị từ chối. | Citation `FAKE` làm phát sinh `ValueError`. | Citation sai vẫn được chấp nhận. |
| `test_embedding_dimension_rejected` | Kiểm tra embedding sai kích thước. | Vector sai dimension làm phát sinh `ValueError`. | Vector sai kích thước vẫn được sử dụng. |

## `unit_integration/test_brief_processor.py`

Kiểm thử failure path của Brief Worker với database, retriever và LLM được
mock bằng `AsyncMock`.

| Test | Nội dung | Đúng khi | Sai khi |
|---|---|---|---|
| `test_stale_job_does_not_call_model` | Kiểm tra job stale không gọi model. | Job bị bỏ qua và LLM không bị gọi. | Model vẫn bị gọi cho job stale. |
| `test_replica_lag_propagates_for_retry` | Kiểm tra replica lag được retry. | `CaseNotReady` được phát ra và model không bị gọi. | Lỗi bị nuốt hoặc bị đánh dấu terminal. |
| `test_retrieval_timeout_propagates_for_retry` | Kiểm tra retrieval timeout. | `TimeoutError` được phát ra để retry. | Timeout bị coi là success hoặc dead ngay. |
| `test_generation_timeout_propagates_for_retry` | Kiểm tra generation timeout. | `TimeoutError` được phát ra và trạng thái retry được ghi. | Timeout bị nuốt hoặc hoàn tất sai. |
| `test_token_budget_records_terminal_fallback` | Kiểm tra vượt token budget. | Job tạo fallback `rule_based`, trạng thái `FAILED` và ghi token usage. | Job retry vô hạn hoặc không ghi fallback. |
| `test_visibility_timeout_is_classified_and_null_signals_supported` | Kiểm tra visibility timeout và `explanations=None`. | Trạng thái là `FAILED_TIMEOUT` và dữ liệu null không gây crash. | Timeout bị coi là success hoặc null gây lỗi. |

## `unit_integration/test_generation_sql.py`

Kiểm thử SQL lifecycle, generation fencing và deadline.

| Test | Nội dung | Đúng khi | Sai khi |
|---|---|---|---|
| `test_pending_running_ready_and_duplicate_delivery` | Kiểm tra chuyển trạng thái và delivery trùng. | State chuyển đúng; delivery trùng không ghi đè kết quả. | State sai hoặc duplicate làm thay đổi dữ liệu. |
| `test_regenerate_during_generation_fences_success_and_failure` | Kiểm tra regenerate khi generation đang chạy. | Attempt cũ bị fence; attempt mới được ghi kết quả. | Attempt cũ ghi đè generation mới. |
| `test_older_attempt_cannot_finish_after_newer_attempt_starts` | Kiểm tra attempt cũ không finish sau attempt mới. | Attempt cũ bị từ chối, attempt mới hoàn tất. | Attempt cũ thay đổi kết quả generation mới. |
| `test_new_generation_clears_previous_content_and_usage` | Kiểm tra generation mới xóa nội dung cũ. | Brief, citations, model, prompt version, token usage và lỗi cũ được reset. | Dữ liệu cũ ảnh hưởng generation mới. |
| `test_dead_letter_is_terminal` | Kiểm tra dead-letter là trạng thái cuối. | Job không retry hoặc chạy lại sau khi dead-letter. | Job dead-letter quay lại queue. |
| `test_legacy_pending_row_can_be_claimed_once` | Kiểm tra row legacy chỉ được claim một lần. | Worker đầu claim thành công, worker sau bị từ chối. | Nhiều worker cùng claim một row. |
| `test_unknown_case_is_not_created_by_worker` | Kiểm tra worker không tự tạo case lạ. | Case không tồn tại bị từ chối và không insert. | Worker tự tạo case mới. |
| `test_valid_stage_deadlines` | Kiểm tra deadline hợp lệ. | Cấu hình timeout hợp lệ được chấp nhận. | Cấu hình hợp lệ bị từ chối. |
| `test_invalid_stage_deadlines` | Kiểm tra deadline không hợp lệ. | Các cấu hình sai phát sinh `ValueError`. | Cấu hình sai vẫn được chấp nhận. |

## `unit_integration/test_queue_integration.py`

Kiểm thử Redis queue thật với priority, acknowledgement, retry, dead-letter và
reaper.

| Test | Nội dung | Đúng khi | Sai khi |
|---|---|---|---|
| `test_priority_order_ack_and_dead_letter` | Kiểm tra thứ tự priority, ack, retry và dead-letter. | Job priority cao ra trước, ack xóa job, retry đúng một lần rồi dead-letter; job hết visibility timeout được requeue. | Queue sai thứ tự hoặc retry/dead-letter/reaper sai. |

Test này **skip** nếu Redis tại `localhost:6379` không chạy.

## `unit_integration/test_brief_pipeline_integration.py`

Kiểm thử luồng Redis → Brief Worker → PostgreSQL bằng database schema tạm và
LLM/retriever stub.

| Test | Nội dung | Đúng khi | Sai khi |
|---|---|---|---|
| `test_brief_pipeline` | Kiểm tra enqueue brief, xử lý thành công, duplicate delivery, regenerate và fallback timeout. | Brief đạt `READY`, duplicate không làm sai trạng thái, generation mới được fence và timeout tạo fallback đúng. | Mất job, ghi sai state, duplicate ghi đè hoặc thiếu fallback. |

Test này **skip** nếu thiếu `FRAUD_TEST_DB_URL` hoặc
`FRAUD_TEST_REDIS_URL`.

## `e2e_smoke/test_transaction_e2e.py`

Kiểm thử transaction thật qua simulator, gateway/API, Redis, Scoring Worker và
PostgreSQL; nếu transaction tạo case thì kiểm tra tiếp case và brief.

| Test | Nội dung | Đúng khi | Sai khi |
|---|---|---|---|
| `test_simulator_transaction_reaches_scoring_result` | Gửi transaction thật và chờ kết quả scoring. | Kết quả được lưu trong thời gian chờ, status là `SCORED` hoặc `MANUAL_FALLBACK_UNSCORED`; case/brief có trạng thái hợp lệ nếu được tạo. | Transaction không đến DB, quá timeout hoặc status sai. |

Test này **skip** nếu chưa đặt `FRAUD_E2E_URL`.

## `unit_integration/test_demo_simulator.py`

Kiểm thử state và runner của simulator bằng Redis giả.

| Test | Nội dung | Đúng khi | Sai khi |
|---|---|---|---|
| `test_simulator_controls_share_state_and_preserve_progress` | Kiểm tra start, set speed và stop dùng chung state. | Offset được giữ, `running`, `ever_started`, TPS và version thay đổi đúng. | State bị reset, offset đổi sai hoặc version không tăng. |
| `test_simulator_runner_publishes_one_shared_batch` | Kiểm tra runner publish batch theo Redis state. | Batch có size/offset đúng, offset mới được cập nhật và lock được giải phóng. | Publish sai batch/offset hoặc lock còn sót. |

## `unit_integration/test_case_outcomes.py`

Kiểm thử mapping kết quả điều tra và quyền reclassify case.

| Test | Nội dung | Đúng khi | Sai khi |
|---|---|---|---|
| `test_case_outcomes_map_to_distinct_workflow_statuses` | Kiểm tra mapping outcome sang workflow status. | Confirmed fraud, dismissed và insufficient evidence map đúng. | Outcome map nhầm hoặc trùng status. |
| `test_outcome_request_accepts_audit_note` | Kiểm tra request outcome có audit note. | Outcome và note được parse đúng. | Note hoặc outcome bị sai. |
| `test_final_outcomes_can_be_reclassified_only_when_a_decision_is_in_history` | Kiểm tra reclassify khi có decision history. | Chỉ cho reclassify khi có quyết định trước đó. | Cho phép sai điều kiện history. |
| `test_closed_and_simulated_cases_cannot_be_reclassified` | Kiểm tra case terminal không được reclassify. | `CLOSED` và `CONFIRMED_SIMULATED` bị từ chối. | Case terminal vẫn bị cập nhật. |

## `unit_integration/test_case_summary.py`

Kiểm thử các chỉ số dashboard của review và analytics.

| Test | Nội dung | Đúng khi | Sai khi |
|---|---|---|---|
| `test_review_and_analytics_return_consistent_dashboard_metrics` | So sánh open count, high count, average probability và latency. | Hai API trả cùng metrics; `open_count` đúng và query không lọc theo `risk_level`. | Metrics lệch hoặc query lọc sai. |

## `unit_integration/test_auth_routes.py`

Kiểm thử đăng ký analyst và phát hành token.

| Test | Nội dung | Đúng khi | Sai khi |
|---|---|---|---|
| `test_register_creates_email_optional_user_and_returns_login_token` | Kiểm tra tạo user không cần email và trả login token. | SQL đúng, password được hash, token có type bearer và role analyst. | SQL, hash, token hoặc role sai. |

## Cách đọc kết quả pytest

```text
PASSED  Test đã chạy và mọi điều kiện kiểm tra đều đúng.
FAILED  Test đã chạy nhưng có assertion sai hoặc exception ngoài dự kiến.
SKIPPED Test chưa chạy vì dependency hoặc biến môi trường chưa có.
XFAIL   Test được phép thất bại theo chủ đích (hiện không dùng trong danh sách này).
```

## Chạy test

```powershell
# Tất cả unit/integration test
python -m pytest -q

# Test worker mock, không cần service ngoài
python -m pytest tests/unit_integration/test_brief_processor.py -q

# Bật Redis integration
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d redis
python -m pytest tests/unit_integration/test_queue_integration.py -q

# Bật brief integration với PostgreSQL và Redis
$env:FRAUD_TEST_DB_URL = "postgresql://fraud:fraud@localhost:5432/fraud"
$env:FRAUD_TEST_REDIS_URL = "redis://localhost:6379/0"
python -m pytest tests/unit_integration/test_brief_pipeline_integration.py -q

# Bật transaction E2E với gateway và các worker đang chạy
$env:FRAUD_E2E_URL = "http://localhost:8080"
$env:SIMULATOR_API_KEY = "dev-sim-key"
python -m pytest tests/e2e_smoke/test_transaction_e2e.py -q
```

Trong CI, job `tests` khởi động Redis và PostgreSQL bằng GitHub Actions
service containers. Job `e2e-smoke` khởi động API stack, Scoring Worker và
LLM Worker bằng Docker Compose, đặt `FRAUD_E2E_URL` rồi chạy transaction E2E
thật qua gateway.

## Test bổ sung từ PR #5

Các test được đặt trong nhóm hiện có để lệnh pytest của CI thu thập tự động.
API/Ollama dùng transport mock; workflow cần DB/Redis kiểm thử riêng.

| Đường dẫn |
|---|
| `unit_integration/test_api_client.py` |
| `unit_integration/test_expanded_playbook.py` |
| `unit_integration/test_kaggle_playbook.py` |
| `unit_integration/test_llm_benchmark.py` |
| `unit_integration/test_llm_ml_features.py` |
| `unit_integration/test_logging_utils.py` |
| `unit_integration/test_ollama_cache.py` |
| `unit_integration/test_online_configuration.py` |
| `unit_integration/test_playbook_v2_eval.py` |
| `unit_integration/test_required_gap.py` |
| `unit_integration/test_retrieval_policy.py` |
| `e2e_smoke/test_workflow_e2e.py` |

## Review queue cho LOW

`unit_integration/test_scoring_review.py` kiểm tra LOW/MEDIUM/HIGH đều tạo case,
nhưng chỉ MEDIUM/HIGH phát sự kiện tạo brief. Workflow integration kiểm tra LOW
xuất hiện trong review queue và không có bản ghi brief.

## Dashboard risk presentation

`ui/risk-review.test.cjs` chạy bằng Node test runner trong job `quality`.
Kiểm tra xác suất bị thiếu/làm tròn, chiều đóng góp tăng/giảm, giá trị đặc trưng
gốc, escape HTML, truy cập brief gốc và tích hợp drawer/sao chép mã giao dịch.
Không cần npm dependency, API key, PostgreSQL hoặc Redis.

"""Chỉ số Prometheus dùng chung (latency p95, queue depth, lỗi...)."""
from prometheus_client import Counter, Gauge, Histogram

_B = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60)

JOBS = Counter("fraud_jobs_total", "Số job đã xử lý", ["queue", "outcome"])
JOB_SECONDS = Histogram("fraud_job_seconds", "Thời gian xử lý job", ["queue"], buckets=_B)
STAGE_SECONDS = Histogram("fraud_stage_seconds", "Thời gian từng bước pipeline", ["stage"], buckets=_B)
QUEUE_DEPTH = Gauge("fraud_queue_depth", "Độ dài hàng đợi", ["queue", "state"])
ROUTED = Counter("fraud_routed_total", "Số giao dịch theo mức rủi ro", ["risk_level"])
INVALID_TXN = Counter("fraud_invalid_transactions_total", "Giao dịch không hợp lệ (MANUAL_FALLBACK)")
LABEL_LEAK_DROPPED = Counter("fraud_label_leak_dropped_total", "Payload có is_fraud bị loại bỏ")
BRIEFS = Counter("fraud_briefs_total", "Kết quả tạo brief", ["status"])
BREAKER_STATE = Gauge("fraud_llm_breaker_open", "1 nếu circuit breaker LLM đang mở")
# Drift monitoring (FR-10): refreshed by the Analyst API from recent scoring results.
FEATURE_PSI = Gauge("fraud_feature_psi", "PSI of a model feature vs the training baseline", ["feature"])
SCORE_PSI = Gauge("fraud_score_psi", "PSI of the calibrated score vs the validation baseline")
DRIFT_ALERT = Gauge("fraud_drift_alert", "1 if any monitored distribution has significant drift")
UNSCORED_RATE = Gauge("fraud_unscored_rate", "Share of recent transactions that could not be scored")
# Service fault injection (simulator / admin); counts only injected, not real, failures.
INJECTED_FAULTS = Counter("fraud_injected_faults_total", "Injected ML service faults", ["kind"])

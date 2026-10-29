-- Schema khởi tạo. Chủ sở hữu ghi:
--   scoring-worker : transactions, scoring_results, cases (tạo mới), unscored_queue
--   analyst-api    : case_actions, cases (đổi trạng thái), case_briefs (tạo PENDING), threshold_configs
--   llm-worker     : case_briefs (cập nhật kết quả)
--   audit_log      : append-only, mọi service được INSERT

CREATE TABLE threshold_configs (
  version     SERIAL PRIMARY KEY,
  low_max     NUMERIC(5,4) NOT NULL,   -- prob <  low_max  -> LOW
  high_min    NUMERIC(5,4) NOT NULL,   -- prob >= high_min -> HIGH, còn lại MEDIUM
  active      BOOLEAN NOT NULL DEFAULT false,
  created_by  TEXT,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK (low_max > 0 AND low_max <= high_min AND high_min < 1)
);
CREATE UNIQUE INDEX one_active_threshold ON threshold_configs (active) WHERE active;
INSERT INTO threshold_configs (low_max, high_min, active, created_by) VALUES (0.30, 0.70, true, 'seed');

CREATE TABLE transactions (
  txn_id          TEXT PRIMARY KEY,
  account_hash_id TEXT,
  payload_masked  JSONB NOT NULL,       -- đã bỏ/che PII, không có is_fraud
  txn_time        TIMESTAMPTZ,          -- NULL nếu giao dịch không hợp lệ
  trace_id        TEXT,
  received_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_txn_account_time ON transactions (account_hash_id, txn_time);

CREATE TABLE scoring_results (
  txn_id                   TEXT PRIMARY KEY REFERENCES transactions(txn_id),
  status                   TEXT NOT NULL,   -- SCORED | MANUAL_FALLBACK_UNSCORED
  raw_model_score          DOUBLE PRECISION,
  calibrated_prob          DOUBLE PRECISION,
  risk_level               TEXT,            -- LOW | MEDIUM | HIGH (NULL nếu unscored)
  model_version            TEXT,
  threshold_config_version INT,
  features                 JSONB,
  explanations             JSONB,
  validation_errors        JSONB,
  latency_ms               DOUBLE PRECISION,
  trace_id                 TEXT,
  scored_at                TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE unscored_queue (            -- hàng đợi kiểm tra đặc biệt (FR-07)
  txn_id     TEXT PRIMARY KEY REFERENCES transactions(txn_id),
  reasons    JSONB NOT NULL,
  status     TEXT NOT NULL DEFAULT 'PENDING',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE cases (
  case_id     BIGSERIAL PRIMARY KEY,
  txn_id      TEXT NOT NULL UNIQUE REFERENCES transactions(txn_id),
  risk_level  TEXT NOT NULL,
  status      TEXT NOT NULL DEFAULT 'OPEN',
  assigned_to TEXT,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_cases_queue ON cases (status, risk_level, created_at);

CREATE TABLE case_briefs (
  case_id        BIGINT PRIMARY KEY REFERENCES cases(case_id),
  generation_id  TEXT,
  status         TEXT NOT NULL DEFAULT 'PENDING',  -- PENDING|RUNNING|READY|FAILED_TIMEOUT|FAILED
  brief          JSONB,
  citations      JSONB,
  attempts       INT NOT NULL DEFAULT 0,
  last_error     TEXT,
  model_name     TEXT,
  prompt_version TEXT,
  tokens_used    INT,
  updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE case_actions (
  id         BIGSERIAL PRIMARY KEY,
  case_id    BIGINT NOT NULL REFERENCES cases(case_id),
  actor      TEXT NOT NULL,
  role       TEXT NOT NULL,
  action     TEXT NOT NULL,
  note       TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE audit_log (
  id        BIGSERIAL PRIMARY KEY,
  ts        TIMESTAMPTZ NOT NULL DEFAULT now(),
  actor     TEXT NOT NULL,
  event     TEXT NOT NULL,
  entity    TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  detail    JSONB,
  trace_id  TEXT
);
CREATE INDEX idx_audit_entity ON audit_log (entity, entity_id);

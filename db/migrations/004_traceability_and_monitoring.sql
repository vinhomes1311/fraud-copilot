-- FR-08 traceability + FR-10 monitoring. Idempotent; the Analyst API also runs
-- these statements at startup, so applying this file by hand is optional.
--   transactions.data_version  : input source, e.g. "fraudTest.csv@sha256:1a2b3c4d5e6f"
--   case_briefs.playbook_version: playbook used for the brief, "<document_id>@<version>#sha256:<12>"
--   idx_scoring_scored_at      : drift/data-quality window = most recent scoring results
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS data_version TEXT;
ALTER TABLE case_briefs ADD COLUMN IF NOT EXISTS playbook_version TEXT;
CREATE INDEX IF NOT EXISTS idx_scoring_scored_at ON scoring_results (scored_at DESC);

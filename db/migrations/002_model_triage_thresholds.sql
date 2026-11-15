-- Activate the triage thresholds recommended by model xgb-20261115040456
-- (`recommended_triage` in data/models/fraud_xgboost.json, calibrated scale):
--   HIGH   : prob >= 0.25   (F1-optimal threshold on validation)
--   MEDIUM : prob >= 0.006  (review queue ~1% of validation traffic)
-- Fresh databases already get these values from db/init.sql.
-- Scoring workers pick up the new row after THRESHOLD_CACHE_TTL_S.
BEGIN;
UPDATE threshold_configs SET active = false WHERE active;
INSERT INTO threshold_configs (low_max, high_min, active, created_by)
VALUES (0.0060, 0.2500, true, 'migration:xgb-20261115040456');
COMMIT;

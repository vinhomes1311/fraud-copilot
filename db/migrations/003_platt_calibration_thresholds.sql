-- Model xgb-20261119142500 replaces isotonic calibration with Platt scaling on
-- log-odds, so probabilities are no longer step-shaped (no exact 0% / 100%)
-- and the thresholds move to its `recommended_triage`
-- (data/models/fraud_xgboost.json, calibrated scale):
--   HIGH   : prob >= 0.30    (F1-optimal threshold on validation)
--   MEDIUM : prob >= 0.0072  (review queue ~1% of validation traffic)
-- Deploy the new model artifact together with this migration: the old
-- isotonic model's probabilities are not on the same scale.
-- Fresh databases already get these values from db/init.sql.
BEGIN;
UPDATE threshold_configs SET active = false WHERE active;
INSERT INTO threshold_configs (low_max, high_min, active, created_by)
VALUES (0.0072, 0.3000, true, 'migration:xgb-20261119142500');
COMMIT;

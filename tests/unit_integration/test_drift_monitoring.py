"""Drift monitoring (FR-10): PSI maths, training baseline and the monitoring API."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pandas as pd
import pytest
from fastapi import HTTPException

from analyst_api.routes import monitoring
from analyst_api.state import state
from fraud_common import drift
from fraud_common.metrics import DRIFT_ALERT, FEATURE_PSI, SCORE_PSI, UNSCORED_RATE
from ml_pipeline.drift import DRIFT_FEATURES, build_drift_baseline, categorical_spec, numeric_spec


def _reference(n=4000, seed=7):
    rng = np.random.default_rng(seed)
    return pd.DataFrame({
        "amt": rng.lognormal(3.5, 1.0, n),
        "trans_count_7d": rng.poisson(12, n).astype(float),
        "transaction_hour": rng.integers(0, 24, n).astype(float),
        "transaction_month": rng.integers(1, 13, n).astype(float),
        "category": rng.choice(["grocery_pos", "gas_transport", "shopping_net"], n),
    })


def _baseline(frame=None):
    frame = _reference() if frame is None else frame
    scores = np.random.default_rng(1).beta(0.3, 40, len(frame))
    return build_drift_baseline(frame, scores, {"low_max": 0.0072, "high_min": 0.3,
                                                "validation_review_rate": 0.01})


# ------------------------------------------------------------------ PSI maths
def test_numeric_bins_are_right_closed_and_missing_has_its_own_bin():
    spec = {"kind": "numeric", "edges": [0.0, 1.0, 3.0], "expected": {}}
    assert drift.assign_bin(spec, -5) == "0"
    assert drift.assign_bin(spec, 0) == "0"        # (-inf, 0]
    assert drift.assign_bin(spec, 0.5) == "1"      # (0, 1]
    assert drift.assign_bin(spec, 3) == "2"        # (1, 3]
    assert drift.assign_bin(spec, 3.01) == "3"     # (3, inf)
    for missing in (None, "abc", float("nan"), float("inf")):
        assert drift.assign_bin(spec, missing) == drift.MISSING


def test_unknown_category_goes_to_other_bin():
    spec = {"kind": "categorical", "expected": {"grocery_pos": 0.5, "travel": 0.5}}
    assert drift.assign_bin(spec, "travel") == "travel"
    assert drift.assign_bin(spec, "crypto") == drift.OTHER
    assert drift.assign_bin(spec, None) == drift.MISSING


def test_psi_is_zero_for_identical_and_grows_with_shift():
    expected = {"0": 0.5, "1": 0.5}
    assert drift.psi(expected, expected) == pytest.approx(0)
    small = drift.psi(expected, {"0": 0.55, "1": 0.45})
    large = drift.psi(expected, {"0": 0.95, "1": 0.05})
    assert 0 < small < drift.STABLE_MAX < drift.MODERATE_MAX < large
    # An empty bin is floored instead of producing infinity.
    assert np.isfinite(drift.psi(expected, {"0": 1.0}))


def test_status_cutoffs_and_minimum_rows():
    assert drift.drift_status(0.05, 1000) == "stable"
    assert drift.drift_status(0.15, 1000) == "moderate"
    assert drift.drift_status(0.40, 1000) == "significant"
    assert drift.drift_status(0.40, drift.MIN_ROWS - 1) == "insufficient_data"
    assert drift.drift_status(None, 1000) == "insufficient_data"


# --------------------------------------------------------- training baseline
def test_numpy_baseline_bins_match_the_online_assignment():
    frame = _reference()
    for name in ("amt", "trans_count_7d"):
        spec = numeric_spec(frame[name])
        actual, total = drift.proportions(spec, frame[name].tolist())
        assert total == len(frame)
        for key in set(actual) | set(spec["expected"]):
            assert actual.get(key, 0) == pytest.approx(spec["expected"].get(key, 0), abs=1e-12)


def test_baseline_skips_calendar_fields_and_includes_routing_edges():
    baseline = _baseline()
    json.dumps(baseline)  # stored in the lineage JSON
    assert "transaction_month" not in baseline["features"]
    assert set(baseline["features"]) <= set(DRIFT_FEATURES)
    assert baseline["features"]["category"]["kind"] == "categorical"
    assert {0.0072, 0.3} <= set(baseline["score"]["edges"])
    assert baseline["expected_review_rate"] == 0.01
    assert baseline["reference"] == "validation"
    for spec in baseline["features"].values():
        assert sum(spec["expected"].values()) == pytest.approx(1)


def test_categorical_spec_caps_categories_and_tracks_missing():
    values = [f"c{i % 40}" for i in range(400)] + [None] * 100
    spec = categorical_spec(values, max_categories=30)
    assert len([k for k in spec["expected"] if k.startswith("c")]) == 30
    assert spec["expected"][drift.OTHER] == pytest.approx(100 / 500)  # 10 extra categories x 10
    assert spec["expected"][drift.MISSING] == pytest.approx(0.2)


def test_same_distribution_is_stable_and_shifted_traffic_is_flagged():
    baseline = _baseline()
    live = _reference(n=3000, seed=99)
    rows = live.to_dict("records")
    scores = np.random.default_rng(2).beta(0.3, 40, len(rows)).tolist()
    report = drift.evaluate_drift(baseline, rows, scores, ["LOW"] * 2970 + ["HIGH"] * 30)
    assert report["overall_status"] == "stable"
    assert report["review_rate"] == {"expected_rate": 0.01, "actual_rate": 0.01, "rows": 3000}

    # Cold-start history (every card looks new) and 3x amounts must be flagged.
    shifted = [dict(row, trans_count_7d=0.0, amt=row["amt"] * 3) for row in rows]
    report = drift.evaluate_drift(baseline, shifted, scores)
    flagged = {item["feature"]: item for item in report["features"]}
    assert report["overall_status"] == "significant"
    assert flagged["trans_count_7d"]["status"] == "significant"
    assert flagged["amt"]["status"] == "significant"
    assert flagged["transaction_hour"]["status"] == "stable"
    assert report["features"][0]["feature"] in ("trans_count_7d", "amt")  # sorted by PSI
    assert flagged["trans_count_7d"]["top_shift"]["actual"] == 1.0


def test_too_few_rows_is_not_reported_as_drift():
    baseline = _baseline()
    rows = _reference(n=50).to_dict("records")
    report = drift.evaluate_drift(baseline, rows, [0.001] * 50)
    assert report["overall_status"] == "insufficient_data"


def test_train_model_and_lineage_carry_the_baseline(tmp_path):
    from ml_pipeline.model import save_artifact
    from ml_pipeline.preprocessing import chronological_split, extract_features
    from tests.ml_evaluation.test_calibration_explain import _synthetic_raw
    from ml_pipeline.model import train_model

    frame = extract_features(_synthetic_raw(1200))
    train, validation, test = chronological_split(frame, 0.6, 0.2)
    artifact = train_model(train, validation, test, n_estimators=20, early_stopping_rounds=5)
    baseline = artifact["drift_baseline"]
    assert baseline["rows"] == len(validation)
    assert "amt" in baseline["features"] and baseline["score"]["edges"]
    path = tmp_path / "model.joblib"
    save_artifact(artifact, path)
    lineage = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
    assert lineage["drift_baseline"]["features"].keys() == baseline["features"].keys()


# -------------------------------------------------------------- monitoring API
def _lineage(tmp_path, baseline, version="xgb-test"):
    path = tmp_path / "fraud_xgboost.json"
    path.write_text(json.dumps({"model_version": version, "drift_baseline": baseline}), encoding="utf-8")
    return path


def _rows(frame, version="xgb-test", unscored=0):
    now = datetime(2026, 11, 21, tzinfo=timezone.utc)
    rows = [{"status": "SCORED", "features": rec, "calibrated_prob": 0.001, "risk_level": "LOW",
             "model_version": version, "validation_errors": None,
             "scored_at": now - timedelta(seconds=i)} for i, rec in enumerate(frame.to_dict("records"))]
    for i in range(unscored):
        field = "_system" if i % 2 else "amt"
        rows.append({"status": "MANUAL_FALLBACK_UNSCORED", "features": None, "calibrated_prob": None,
                     "risk_level": None, "model_version": None,
                     "validation_errors": [{"field": field, "msg": "x"}], "scored_at": now})
    return rows


def test_report_uses_only_current_model_rows_for_scores_and_counts_input_errors(tmp_path):
    baseline = _baseline()
    live = _reference(n=600, seed=5)
    rows = _rows(live) + _rows(live.head(300), version="xgb-old") + []
    rows += _rows(live.head(0), unscored=10)
    report = monitoring.build_report({"model_version": "xgb-test", "drift_baseline": baseline}, rows)
    assert report["window"]["scored_rows"] == 900
    assert report["window"]["current_model_rows"] == 600
    assert report["score"]["rows"] == 600
    assert report["data_quality"]["unscored"] == 10
    assert report["data_quality"]["system_failures"] == 5
    assert {"field": "amt", "count": 5} in report["data_quality"]["by_field"]
    assert report["data_quality"]["unscored_rate"] == pytest.approx(10 / 910, abs=1e-5)


@pytest.mark.asyncio
async def test_drift_endpoint_reads_lineage_and_exports_gauges(tmp_path, monkeypatch):
    baseline = _baseline()
    path = _lineage(tmp_path, baseline)
    monkeypatch.setattr(monitoring, "lineage_path", lambda: path)
    shifted = _reference(n=500, seed=3).assign(trans_count_7d=0.0)
    db = MagicMock()
    db.rfetch = AsyncMock(return_value=_rows(shifted, unscored=4))
    previous = state.db
    state.db = db
    try:
        report = await monitoring.get_drift(None, window=1000)
    finally:
        state.db = previous
    assert db.rfetch.await_args.args[1] == 1000
    assert report["window"]["requested_rows"] == 1000
    assert report["model_version"] == "xgb-test"
    assert report["overall_status"] == "significant"
    assert FEATURE_PSI.labels("trans_count_7d")._value.get() >= 0.25
    assert DRIFT_ALERT._value.get() == 1
    assert SCORE_PSI._value.get() >= 0
    assert UNSCORED_RATE._value.get() == pytest.approx(4 / 504, abs=1e-5)


@pytest.mark.asyncio
async def test_drift_endpoint_without_baseline_is_503(tmp_path, monkeypatch):
    path = tmp_path / "fraud_xgboost.json"
    path.write_text(json.dumps({"model_version": "old"}), encoding="utf-8")
    monkeypatch.setattr(monitoring, "lineage_path", lambda: path)
    with pytest.raises(HTTPException) as exc:
        await monitoring.get_drift(None, window=500)
    assert exc.value.status_code == 503
    monkeypatch.setattr(monitoring, "lineage_path", lambda: tmp_path / "missing.json")
    with pytest.raises(HTTPException):
        await monitoring.get_drift(None, window=500)


@pytest.mark.asyncio
async def test_baseline_cache_reloads_when_the_model_file_changes(tmp_path, monkeypatch):
    path = _lineage(tmp_path, _baseline(), version="v1")
    monkeypatch.setattr(monitoring, "lineage_path", lambda: path)
    assert monitoring.load_baseline()["model_version"] == "v1"
    path.write_text(json.dumps({"model_version": "v2-longer", "drift_baseline": _baseline()}), encoding="utf-8")
    assert monitoring.load_baseline()["model_version"] == "v2-longer"


@pytest.mark.asyncio
async def test_drift_loop_survives_errors_and_stops(monkeypatch):
    calls = []

    async def failing(window):
        calls.append(window)
        raise RuntimeError("db down")

    monkeypatch.setattr(monitoring, "compute_report", failing)
    stop = asyncio.Event()
    task = asyncio.create_task(monitoring.drift_loop(stop, interval_s=0.01))
    await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, timeout=1)
    assert len(calls) >= 2 and calls[0] == monitoring.DEFAULT_WINDOW

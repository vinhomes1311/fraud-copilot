"""Model monitoring: data/score drift against the training baseline (FR-10).

Live rows come from ``scoring_results`` (most recent first). Feature drift uses
every scored row in the window; score drift and the review rate use only rows
scored by the model version the baseline belongs to, since another version's
scores are not comparable.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections import Counter
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from fraud_common.config import get_settings
from fraud_common.drift import evaluate_drift
from fraud_common.metrics import DRIFT_ALERT, FEATURE_PSI, SCORE_PSI, UNSCORED_RATE
from fraud_common.schemas import Role, ScoringStatus

from ..auth import User, require_roles
from ..state import state

router = APIRouter(prefix="/monitoring", tags=["monitoring"])
monitor_reader = require_roles(Role.ANALYST, Role.RISK_LEAD, Role.ADMIN)
log = logging.getLogger(__name__)

DEFAULT_WINDOW = 5000
_baseline_cache: dict[str, Any] = {"key": None, "value": None}


def lineage_path() -> Path:
    return Path(get_settings().model_artifact_path).with_suffix(".json")


def load_baseline() -> dict[str, Any] | None:
    """Model lineage JSON with its drift baseline; reloaded when the file changes."""
    path = lineage_path()
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    key = (str(path), stat.st_mtime_ns, stat.st_size)
    if _baseline_cache["key"] != key:
        lineage = json.loads(path.read_text(encoding="utf-8"))
        _baseline_cache.update(key=key, value=lineage)
    lineage = _baseline_cache["value"]
    return lineage if lineage.get("drift_baseline") else None


async def recent_rows(window: int) -> list[dict[str, Any]]:
    rows = await state.db.rfetch(
        """SELECT status, features, calibrated_prob, risk_level, model_version,
                  validation_errors, scored_at
           FROM scoring_results ORDER BY scored_at DESC LIMIT $1""", window)
    return [dict(row) for row in rows]


def data_quality(rows: list[dict[str, Any]]) -> dict[str, Any]:
    unscored = [row for row in rows if row["status"] != ScoringStatus.SCORED.value]
    fields: Counter = Counter()
    for row in unscored:
        for error in row.get("validation_errors") or []:
            fields[str(error.get("field") or "unknown")] += 1
    return {
        "rows": len(rows),
        "unscored": len(unscored),
        "unscored_rate": round(len(unscored) / len(rows), 5) if rows else None,
        "system_failures": fields.get("_system", 0),
        "by_field": [{"field": field, "count": count} for field, count in fields.most_common(8)],
    }


def build_report(lineage: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    baseline = lineage["drift_baseline"]
    version = lineage.get("model_version")
    scored = [row for row in rows if row["status"] == ScoringStatus.SCORED.value and row.get("features")]
    current = [row for row in scored if row.get("model_version") == version]
    report = evaluate_drift(
        baseline,
        [row["features"] for row in scored],
        [row["calibrated_prob"] for row in current],
        [row["risk_level"] for row in current],
    )
    times = [row["scored_at"] for row in rows if row.get("scored_at") is not None]
    report.update({
        "model_version": version,
        "baseline": {"reference": baseline.get("reference"), "rows": baseline.get("rows"),
                     "created_at": baseline.get("created_at")},
        "window": {"requested_rows": None, "rows": len(rows), "scored_rows": len(scored),
                   "current_model_rows": len(current),
                   "from": min(times).isoformat() if times else None,
                   "to": max(times).isoformat() if times else None},
        "data_quality": data_quality(rows),
    })
    return report


def export_metrics(report: dict[str, Any]) -> None:
    for item in report["features"]:
        if item["psi"] is not None:
            FEATURE_PSI.labels(item["feature"]).set(item["psi"])
    if report.get("score") and report["score"]["psi"] is not None:
        SCORE_PSI.set(report["score"]["psi"])
    DRIFT_ALERT.set(1 if report["overall_status"] == "significant" else 0)
    rate = report["data_quality"]["unscored_rate"]
    if rate is not None:
        UNSCORED_RATE.set(rate)


async def compute_report(window: int) -> dict[str, Any]:
    lineage = load_baseline()
    if lineage is None:
        raise LookupError("The model artifact has no drift baseline; retrain or run python -m ml_pipeline.drift")
    report = build_report(lineage, await recent_rows(window))
    report["window"]["requested_rows"] = window
    export_metrics(report)
    return report


@router.get("/drift")
async def get_drift(
    _: User = Depends(monitor_reader),
    window: int = Query(DEFAULT_WINDOW, ge=100, le=50000),
):
    try:
        return await compute_report(window)
    except LookupError as exc:
        raise HTTPException(503, str(exc)) from exc


async def drift_loop(stop: asyncio.Event, interval_s: float | None = None) -> None:
    """Refresh the Prometheus gauges so alerts fire without anyone opening the UI."""
    interval = interval_s or get_settings().drift_monitor_interval_s
    last_status = None
    while not stop.is_set():
        try:
            report = await compute_report(DEFAULT_WINDOW)
            status = report["overall_status"]
            if status == "significant" and last_status != "significant":
                drifted = [f["feature"] for f in report["features"] if f["status"] == "significant"]
                log.warning("drift_detected", extra={"ctx": {
                    "features": drifted, "score": report.get("score"), "model_version": report["model_version"]}})
            last_status = status
        except LookupError:
            pass
        except Exception:
            log.exception("drift_monitor_failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass

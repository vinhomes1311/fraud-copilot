"""Bước 4 - ML inference backed by the versioned XGBoost artifact."""
from __future__ import annotations

from functools import lru_cache

import pandas as pd

from fraud_common.config import get_settings
from fraud_common.schemas import Explanation, ScoringOutput

from ml_pipeline.explain import explain_prediction
from ml_pipeline.model import load_artifact


@lru_cache(maxsize=1)
def _artifact() -> dict:
    return load_artifact(get_settings().model_artifact_path)


def predict(features: dict[str, float | str | None]) -> ScoringOutput:
    artifact = _artifact()
    frame = pd.DataFrame([features]).reindex(columns=artifact["feature_columns"])
    raw_probability = float(artifact["pipeline"].predict_proba(frame)[0, 1])
    calibrated_probability = float(artifact["calibrator"].transform([raw_probability])[0])
    factors = explain_prediction(artifact, frame, features)
    explanations = [Explanation(**factor) for factor in factors] or [
        Explanation(feature="model_probability", value=calibrated_probability,
                    contribution=calibrated_probability)
    ]
    return ScoringOutput(
        raw_model_score=raw_probability,
        calibrated_prob=calibrated_probability,
        model_version=str(artifact["model_version"]),
        explanations=explanations,
    )

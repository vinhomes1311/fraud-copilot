"""Bước 4 - ML inference backed by the versioned XGBoost artifact."""
from __future__ import annotations

from functools import lru_cache

import pandas as pd
import xgboost as xgb

from fraud_common.config import get_settings
from fraud_common.schemas import Explanation, ScoringOutput

from ml_pipeline.model import load_artifact


@lru_cache(maxsize=1)
def _artifact() -> dict:
    return load_artifact(get_settings().model_artifact_path)


def predict(features: dict[str, float | str | None]) -> ScoringOutput:
    artifact = _artifact()
    frame = pd.DataFrame([features]).reindex(columns=artifact["feature_columns"])
    raw_probability = float(artifact["pipeline"].predict_proba(frame)[0, 1])
    calibrated_probability = float(artifact["calibrator"].transform([raw_probability])[0])
    model_version = str(artifact["model_version"])
    contributions = _rank_feature_contributions(artifact, frame, calibrated_probability)
    return ScoringOutput(
        raw_model_score=raw_probability,
        calibrated_prob=calibrated_probability,
        model_version=model_version,
        explanations=[
            Explanation(
                feature=name,
                value=_explanation_value(features.get(name)),
                contribution=value,
            )
            for name, value in contributions
        ],
    )


def _rank_feature_contributions(
    artifact: dict, frame: pd.DataFrame, probability: float
) -> list[tuple[str, float]]:
    estimator = artifact["pipeline"].named_steps["model"]
    preprocessor = artifact["pipeline"].named_steps["preprocessor"]
    transformed = preprocessor.transform(frame)
    names = preprocessor.get_feature_names_out()
    contributions = estimator.get_booster().predict(
        xgb.DMatrix(transformed), pred_contribs=True
    )[0][:-1]
    grouped: dict[str, float] = {}
    for name, contribution in zip(names, contributions):
        source_name = str(name).split("__", 1)[-1]
        if source_name not in frame.columns:
            source_name = source_name.split("_", 1)[0]
        grouped[source_name] = grouped.get(source_name, 0.0) + float(contribution)
    ranked = sorted(grouped.items(), key=lambda item: abs(item[1]), reverse=True)
    return ranked[:3] or [("model_probability", probability)]


def _explanation_value(value: float | str | None) -> float:
    if value is None:
        return 0.0
    if isinstance(value, str):
        return 1.0
    return float(value)

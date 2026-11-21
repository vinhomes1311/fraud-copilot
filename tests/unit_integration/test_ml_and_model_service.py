"""Unit & Integration tests for model_service, ml_pipeline/config.py, and ml_pipeline/evaluate_artifact.py.

Covers:
- model_service/main.py:
  * GET /healthz (returns {"status": "ok"})
  * POST /v1/predict (successful inference and schema return)
  * POST /v1/predict (model artifact unavailable or invalid -> 503 Service Unavailable)
- ml_pipeline/config.py:
  * FileNotFoundError when config file does not exist
  * Validation errors: missing required sections, invalid train_ratio, invalid validation_ratio,
    train + validation ratio >= 1
  * Valid config parsing
- ml_pipeline/evaluate_artifact.py:
  * evaluate_artifact logic with mocked models & features
  * quality gates evaluation in main()
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from ml_pipeline.config import PipelineConfig, _validate_config, load_config
from ml_pipeline.evaluate_artifact import evaluate_artifact, main
from model_service.main import InferenceRequest, healthz, predict_route


# ===========================================================================
# 1. Tests for model_service/main.py
# ===========================================================================

def test_model_service_healthz():
    res = healthz()
    assert res == {"status": "ok"}


def test_model_service_predict_success():
    dummy_pred = SimpleNamespace(
        model_dump=lambda mode="json": {
            "raw_model_score": 0.88,
            "calibrated_prob": 0.85,
            "model_version": "v1",
            "explanations": [],
        }
    )

    with patch("model_service.main.predict", return_value=dummy_pred):
        req = InferenceRequest(features={"amt": 150.0, "category": "grocery_pos"})
        res = predict_route(req)
        assert res["raw_model_score"] == 0.88
        assert res["calibrated_prob"] == 0.85


def test_model_service_predict_model_unavailable_503():
    # 1. FileNotFoundError -> 503
    with patch("model_service.main.predict", side_effect=FileNotFoundError("Artifact missing")):
        req = InferenceRequest(features={"amt": 150.0})
        with pytest.raises(HTTPException) as exc_info:
            predict_route(req)
        assert exc_info.value.status_code == 503
        assert "model artifact unavailable or invalid" in exc_info.value.detail

    # 2. ValueError -> 503
    with patch("model_service.main.predict", side_effect=ValueError("Corrupt booster")):
        with pytest.raises(HTTPException) as exc_info:
            predict_route(req)
        assert exc_info.value.status_code == 503

    # 3. KeyError -> 503
    with patch("model_service.main.predict", side_effect=KeyError("pipeline")):
        with pytest.raises(HTTPException) as exc_info:
            predict_route(req)
        assert exc_info.value.status_code == 503


# ===========================================================================
# 2. Tests for ml_pipeline/config.py
# ===========================================================================

def test_load_config_file_not_found(tmp_path: Path):
    non_existent = tmp_path / "does_not_exist.json"
    with pytest.raises(FileNotFoundError) as exc_info:
        load_config(non_existent)
    assert "ML configuration file does not exist" in str(exc_info.value)


def test_validate_config_missing_sections():
    dummy_path = Path("config.json")
    # Missing 'model' and 'evaluation'
    bad_config = {
        "data": {},
        "split": {"train_ratio": 0.7, "validation_ratio": 0.15},
    }
    with pytest.raises(ValueError) as exc_info:
        _validate_config(bad_config, dummy_path)
    assert "missing configuration sections" in str(exc_info.value)


def test_validate_config_ratio_bounds():
    dummy_path = Path("config.json")
    base = {
        "data": {},
        "model": {},
        "evaluation": {},
    }

    # train_ratio <= 0
    bad_train = {**base, "split": {"train_ratio": 0.0, "validation_ratio": 0.2}}
    with pytest.raises(ValueError) as exc_info:
        _validate_config(bad_train, dummy_path)
    assert "split.train_ratio must be between 0 and 1" in str(exc_info.value)

    # validation_ratio <= 0
    bad_val = {**base, "split": {"train_ratio": 0.7, "validation_ratio": 0.0}}
    with pytest.raises(ValueError) as exc_info:
        _validate_config(bad_val, dummy_path)
    assert "split.validation_ratio must be between 0 and 1" in str(exc_info.value)

    # train + validation >= 1
    bad_sum = {**base, "split": {"train_ratio": 0.8, "validation_ratio": 0.3}}
    with pytest.raises(ValueError) as exc_info:
        _validate_config(bad_sum, dummy_path)
    assert "train_ratio + validation_ratio must be less than 1" in str(exc_info.value)


def test_load_valid_config(tmp_path: Path):
    valid_data = {
        "data": {"raw": "data/train.csv"},
        "split": {"train_ratio": 0.7, "validation_ratio": 0.15},
        "model": {"max_depth": 6},
        "evaluation": {"threshold": 0.5},
    }
    config_file = tmp_path / "test_config.json"
    config_file.write_text(json.dumps(valid_data), encoding="utf-8")

    cfg = load_config(config_file)
    assert isinstance(cfg, PipelineConfig)
    assert cfg.split["train_ratio"] == 0.7
    assert cfg.model["max_depth"] == 6


# ===========================================================================
# 3. Tests for ml_pipeline/evaluate_artifact.py
# ===========================================================================

def test_evaluate_artifact_execution(tmp_path: Path):
    import numpy as np
    import pandas as pd

    dummy_artifact = {
        "model_version": "test-v1",
        "threshold": 0.45,
        "lineage": {"test_metrics": {"pr_auc": 0.95}},
        "calibrator": MagicMock(transform=lambda x: np.array([0.8, 0.2])),
        "pipeline": MagicMock(predict_proba=lambda x: np.array([[0.2, 0.8], [0.8, 0.2]])),
        "feature_columns": ["amt"],
    }

    dummy_df = pd.DataFrame({
        "is_fraud": [1, 0],
        "_sort_time": [100, 200],
        "amt": [50.0, 10.0],
    })

    dummy_metrics = {"pr_auc": 0.96, "recall": 0.88, "precision": 0.90, "f1": 0.89}
    output_json = tmp_path / "output_metrics.json"

    with patch("ml_pipeline.evaluate_artifact.load_artifact", return_value=dummy_artifact), \
         patch("ml_pipeline.evaluate_artifact.read_csv_safely", return_value=dummy_df), \
         patch("ml_pipeline.evaluate_artifact.extract_features", return_value=dummy_df), \
         patch("ml_pipeline.evaluate_artifact.evaluate_model", return_value=dummy_metrics):

        report = evaluate_artifact(
            artifact_path="data/model.joblib",
            test_path="data/test.csv",
            output_path=output_json,
        )

        assert report["model_version"] == "test-v1"
        assert report["metrics"]["pr_auc"] == 0.96
        assert output_json.exists()
        saved = json.loads(output_json.read_text(encoding="utf-8"))
        assert saved["threshold"] == 0.45


def test_evaluate_artifact_main_cli():
    # 1. Quality gate passes
    good_report = {
        "metrics": {"pr_auc": 0.95, "recall": 0.85, "precision": 0.85, "f1": 0.88}
    }
    with patch("ml_pipeline.evaluate_artifact.evaluate_artifact", return_value=good_report), \
         patch.object(sys, "argv", ["evaluate_artifact", "--min-pr-auc", "0.90"]):
        main()

    # 2. Quality gate fails -> SystemExit
    bad_report = {
        "metrics": {"pr_auc": 0.70, "recall": 0.50, "precision": 0.50, "f1": 0.50}
    }
    with patch("ml_pipeline.evaluate_artifact.evaluate_artifact", return_value=bad_report), \
         patch.object(sys, "argv", ["evaluate_artifact", "--min-pr-auc", "0.90"]), \
         pytest.raises(SystemExit) as exc_info:
        main()
    assert "ML quality gate failed" in str(exc_info.value)

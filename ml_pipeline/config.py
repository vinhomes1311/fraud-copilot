from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class PipelineConfig:
    data: dict[str, Any]
    split: dict[str, float]
    model: dict[str, Any]
    evaluation: dict[str, float]


def load_config(path: str | Path | None = None) -> PipelineConfig:
    config_path = Path(path or os.getenv("ML_CONFIG_PATH", "ml_pipeline/config.json"))
    if not config_path.exists():
        raise FileNotFoundError(f"ML configuration file does not exist: {config_path.resolve()}")
    with config_path.open(encoding="utf-8") as handle:
        raw = json.load(handle)
    _validate_config(raw, config_path)
    return PipelineConfig(**raw)


def _validate_config(raw: dict[str, Any], path: Path) -> None:
    required = {"data", "split", "model", "evaluation"}
    missing = required - raw.keys()
    if missing:
        raise ValueError(f"{path} is missing configuration sections: {sorted(missing)}")
    if not 0 < raw["split"]["train_ratio"] < 1:
        raise ValueError("split.train_ratio must be between 0 and 1")
    if not 0 < raw["split"]["validation_ratio"] < 1:
        raise ValueError("split.validation_ratio must be between 0 and 1")
    if raw["split"]["train_ratio"] + raw["split"]["validation_ratio"] >= 1:
        raise ValueError("train_ratio + validation_ratio must be less than 1")

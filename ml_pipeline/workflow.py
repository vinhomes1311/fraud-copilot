from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from .config import PipelineConfig, load_config
from .model import save_artifact, train_model
from .preprocessing import (
    chronological_split,
    chronological_train_validation_split,
    extract_features,
    read_csv_safely,
)

log = logging.getLogger(__name__)


@dataclass
class PreparedData:
    train: pd.DataFrame
    validation: pd.DataFrame
    test: pd.DataFrame
    source_mode: str
    source_files: list[Path] = field(default_factory=list)


def prepare_datasets(config: PipelineConfig) -> PreparedData:
    """Build features and chronological train/validation/test splits.

    Pre-split mode (fraudTrain + fraudTest): features are computed on the
    concatenated data so test rows see card history from the train period, then
    fraudTrain is split into train / validation (``split.validation_ratio`` of
    the most recent rows) and fraudTest stays the untouched hold-out set.

    Single-file mode: the file is split chronologically with ``train_ratio`` and
    ``validation_ratio``; the rest becomes test.
    """
    data = config.data
    target = data["target"]
    train_path = Path(data["train_file"])
    test_path = Path(data["test_file"])
    if train_path.exists() and test_path.exists():
        raw = read_csv_safely(train_path)
        raw_test = read_csv_safely(test_path)
        raw["_split_tag"] = "train"
        raw_test["_split_tag"] = "test"
        prepared = extract_features(pd.concat([raw, raw_test], ignore_index=True), target)
        train_source = prepared[prepared["_split_tag"] == "train"].drop(columns="_split_tag")
        test = (
            prepared[prepared["_split_tag"] == "test"]
            .drop(columns="_split_tag")
            .reset_index(drop=True)
        )
        train, validation = chronological_train_validation_split(
            train_source, config.split["validation_ratio"]
        )
        return PreparedData(train, validation, test, "pre_split", [train_path, test_path])
    single_path = Path(data["single_file"])
    prepared = extract_features(read_csv_safely(single_path), target)
    train, validation, test = chronological_split(
        prepared, config.split["train_ratio"], config.split["validation_ratio"]
    )
    return PreparedData(train, validation, test, "single_file", [single_path])


def load_processed_datasets(config: PipelineConfig) -> PreparedData:
    """Reload the splits written by a previous ``run_pipeline`` call."""
    processed = Path(config.data["processed_dir"])
    frames = {}
    for name in ("train", "validation", "test"):
        path = processed / f"{name}.csv"
        frames[name] = read_csv_safely(path)
        if "_sort_time" in frames[name]:
            frames[name]["_sort_time"] = pd.to_datetime(frames[name]["_sort_time"])
    return PreparedData(
        frames["train"], frames["validation"], frames["test"], "processed",
        [processed / f"{name}.csv" for name in frames],
    )


def run_pipeline(config_path: str | Path | None = None) -> Path:
    config = load_config(config_path)
    data = config.data
    prepared = prepare_datasets(config)
    log.info(
        "datasets_prepared",
        extra={
            "source_mode": prepared.source_mode,
            "train_rows": len(prepared.train),
            "validation_rows": len(prepared.validation),
            "test_rows": len(prepared.test),
        },
    )
    output_dir = Path(data["processed_dir"])
    output_dir.mkdir(parents=True, exist_ok=True)
    prepared.train.to_csv(output_dir / "train.csv", index=False)
    prepared.validation.to_csv(output_dir / "validation.csv", index=False)
    prepared.test.to_csv(output_dir / "test.csv", index=False)
    model_options = {key: value for key, value in config.model.items() if key != "artifact_path"}
    artifact = train_model(
        prepared.train,
        prepared.validation,
        prepared.test,
        target=data["target"],
        evaluation=config.evaluation,
        **model_options,
    )
    artifact_path = Path(config.model["artifact_path"])
    save_artifact(artifact, artifact_path, prepared.source_files)
    log.info("pipeline_completed", extra={"model_path": str(artifact_path), "model_version": artifact["model_version"]})
    return artifact_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Run preprocessing, training, evaluation, and artifact publishing")
    parser.add_argument("--config", default=None)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run_pipeline(args.config)


if __name__ == "__main__":
    main()

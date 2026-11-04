from __future__ import annotations

import argparse
import logging
from pathlib import Path

import pandas as pd

from .config import load_config
from .model import save_artifact, train_model
from .preprocessing import chronological_split, extract_features, read_csv_safely

log = logging.getLogger(__name__)


def run_pipeline(config_path: str | Path | None = None) -> Path:
    config = load_config(config_path)
    data = config.data
    train_path = Path(data["train_file"])
    test_path = Path(data["test_file"])
    source_files = [path for path in (train_path, test_path) if path.exists()]
    if train_path.exists() and test_path.exists():
        raw = read_csv_safely(train_path)
        raw_test = read_csv_safely(test_path)
        raw["_split_tag"] = "train"
        raw_test["_split_tag"] = "test"
        prepared = extract_features(
            pd.concat([raw, raw_test], ignore_index=True),
            data["target"],
        )
        train_source = prepared[prepared["_split_tag"] == "train"].drop(columns="_split_tag")
        test = prepared[prepared["_split_tag"] == "test"].drop(columns="_split_tag")
        train, validation, _ = chronological_split(
            train_source, config.split["train_ratio"], config.split["validation_ratio"]
        )
    else:
        single_path = Path(data["single_file"])
        source_files = [single_path]
        prepared = extract_features(read_csv_safely(single_path), data["target"])
        train, validation, test = chronological_split(
            prepared, config.split["train_ratio"], config.split["validation_ratio"]
        )
    output_dir = Path(data["processed_dir"])
    output_dir.mkdir(parents=True, exist_ok=True)
    train.to_csv(output_dir / "train.csv", index=False)
    validation.to_csv(output_dir / "validation.csv", index=False)
    test.to_csv(output_dir / "test.csv", index=False)
    model_options = {key: value for key, value in config.model.items() if key != "artifact_path"}
    artifact = train_model(
        train,
        validation,
        test,
        target=data["target"],
        **model_options,
    )
    artifact_path = Path(config.model["artifact_path"])
    save_artifact(artifact, artifact_path, source_files)
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

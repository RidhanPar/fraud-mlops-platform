"""Export the registry champion into a self contained folder the service loads.

The Docker image copies this folder, so each image is pinned to exactly one
model version. Deploying a new model is an explicit, reviewable image build.

Usage:
    python -m fraud_mlops.export_model                 # current champion
    python -m fraud_mlops.export_model --alias previous
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

import mlflow
import numpy as np
import pandas as pd
from mlflow import MlflowClient

from fraud_mlops import data as data_mod
from fraud_mlops.config import ROOT, load_params
from fraud_mlops.features import MODEL_COLUMNS, RAW_COLUMNS
from fraud_mlops.monitoring.drift import feature_drift
from fraud_mlops.train import tracking_uri

DEFAULT_OUT = ROOT / "serving_model"

# Drift alert thresholds are calibrated, not taken from a textbook. This dataset
# has real day over day shift in several PCA features (V1 PSI ~1.5 between days)
# while the model stays accurate, so a flat 0.25 would alert every day.
CALIBRATION_WINDOWS = 10
CALIBRATION_WINDOW_ROWS = 5000
PSI_FLOOR = 0.25
PSI_MARGIN = 1.5


def calibrate_drift_thresholds(reference: pd.DataFrame, params: dict) -> dict:
    """Per feature PSI threshold = max(floor, margin x worst PSI seen on known good
    traffic). Known good = the validation slice, which the model never trained on
    and on which its performance was verified."""
    df = data_mod.load(ROOT / params["data"]["path"])
    valid = data_mod.time_split(df, params["data"]["train_frac"], params["data"]["valid_frac"]).valid
    runs = [
        feature_drift(reference, valid.sample(CALIBRATION_WINDOW_ROWS, random_state=i)).set_index("feature").psi
        for i in range(CALIBRATION_WINDOWS)
    ]
    worst = pd.concat(runs, axis=1).max(axis=1)
    return {
        "method": f"max({PSI_FLOOR}, {PSI_MARGIN} x max PSI over {CALIBRATION_WINDOWS} "
        f"windows of {CALIBRATION_WINDOW_ROWS} validation rows)",
        "window_rows": CALIBRATION_WINDOW_ROWS,
        "thresholds": {f: float(np.maximum(PSI_FLOOR, PSI_MARGIN * v)) for f, v in worst.items()},
        "baseline_max_psi": {f: float(v) for f, v in worst.items()},
    }


def export(alias: str, out: Path) -> dict:
    mlflow.set_tracking_uri(tracking_uri())
    client = MlflowClient()
    name = load_params()["registry"]["model_name"]

    mv = client.get_model_version_by_alias(name, alias)
    run = client.get_run(mv.run_id)

    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    model_dir = Path(mlflow.artifacts.download_artifacts(f"models:/{name}/{mv.version}"))
    shutil.copy(model_dir / "model.pkl", out / "model.pkl")
    ref_dir = mlflow.artifacts.download_artifacts(run_id=mv.run_id, artifact_path="reference")
    reference = pd.read_parquet(Path(ref_dir) / "reference.parquet")
    # CSV keeps the serving image free of a parquet engine.
    reference.to_csv(out / "reference.csv.gz", index=False)
    drift_cfg = calibrate_drift_thresholds(reference, load_params())
    (out / "drift_thresholds.json").write_text(json.dumps(drift_cfg, indent=2), encoding="utf-8")

    meta = {
        "model_name": name,
        "model_version": str(mv.version),
        "alias": alias,
        "run_id": mv.run_id,
        "threshold": float(mv.tags["threshold"]),
        "git_sha": run.data.tags.get("git_sha"),
        "data_sha256": run.data.params.get("data.sha256"),
        "holdout_metrics": {
            k.removeprefix("holdout_"): v
            for k, v in run.data.metrics.items()
            if k.startswith("holdout_")
        },
        "input_columns": RAW_COLUMNS,
        "model_columns": MODEL_COLUMNS,
    }
    (out / "metadata.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return meta


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--alias", default="champion")
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    args = parser.parse_args()
    meta = export(args.alias, Path(args.out))
    print(f"exported {meta['model_name']} v{meta['model_version']} ({args.alias}) to {args.out}")


if __name__ == "__main__":
    main()

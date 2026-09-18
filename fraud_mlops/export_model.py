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
from mlflow import MlflowClient

from fraud_mlops.config import ROOT, load_params
from fraud_mlops.features import MODEL_COLUMNS, RAW_COLUMNS
from fraud_mlops.train import tracking_uri

DEFAULT_OUT = ROOT / "serving_model"


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
    ref = mlflow.artifacts.download_artifacts(run_id=mv.run_id, artifact_path="reference")
    shutil.copy(Path(ref) / "reference.parquet", out / "reference.parquet")

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

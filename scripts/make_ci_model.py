"""Write a small stand in serving_model/ so CI can build and smoke test the image.

The real model folder comes from the MLflow registry and is not in git. CI
trains a tiny model on synthetic data with the same schema and file layout,
which proves the image builds, starts, verifies its model hash and serves
correctly. This image is never pushed or deployed.

    python scripts/make_ci_model.py
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import cloudpickle

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fraud_mlops.audit.hashing import file_sha256  # noqa: E402
from fraud_mlops.config import load_params  # noqa: E402
from fraud_mlops.features import PCA_COLUMNS, RAW_COLUMNS  # noqa: E402
from fraud_mlops.train import build_pipeline  # noqa: E402
from tests.conftest import make_transactions  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "serving_model"))
    out = Path(ap.parse_args().out)
    out.mkdir(parents=True, exist_ok=True)
    df = make_transactions(4000)
    model = build_pipeline(load_params()["model"] | {"n_estimators": 20, "max_depth": 3}, 42)
    model.fit(df[RAW_COLUMNS], df["Class"])
    with open(out / "model.pkl", "wb") as f:
        cloudpickle.dump(model, f)

    ref = df[RAW_COLUMNS].assign(score=model.predict_proba(df[RAW_COLUMNS])[:, 1])
    ref.to_csv(out / "reference.csv.gz", index=False)
    (out / "drift_thresholds.json").write_text(json.dumps(
        {"method": "ci placeholder", "thresholds": {f: 0.25 for f in [*PCA_COLUMNS, "Amount"]}}))
    (out / "metadata.json").write_text(json.dumps({
        "model_name": "fraud-detector", "model_version": "ci", "threshold": 0.5,
        "model_sha256": file_sha256(out / "model.pkl"), "note": "synthetic CI model, never deployed",
    }, indent=2))
    print(f"wrote CI model to {out}")


if __name__ == "__main__":
    main()

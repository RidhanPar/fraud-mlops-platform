"""Loads the exported model folder. No MLflow dependency at serving time."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import cloudpickle
import numpy as np
import pandas as pd

from fraud_mlops.audit.hashing import file_sha256
from fraud_mlops.features import RAW_COLUMNS


class FraudModel:
    def __init__(self, model_dir: str | Path) -> None:
        model_dir = Path(model_dir)
        self.meta: dict[str, Any] = json.loads((model_dir / "metadata.json").read_text("utf-8"))
        # Every audit record carries this hash, tying each decision to these exact bytes.
        self.sha256 = file_sha256(model_dir / "model.pkl")
        if self.sha256 != self.meta["model_sha256"]:
            raise RuntimeError("model.pkl does not match the hash in metadata.json; refusing to serve")
        with open(model_dir / "model.pkl", "rb") as f:
            self.pipeline = cloudpickle.load(f)
        self.version = str(self.meta["model_version"])
        self.threshold = float(self.meta["threshold"])

        # One thread per request. Throughput comes from uvicorn workers, which
        # avoids threads from concurrent requests fighting over the same cores.
        self.pipeline.named_steps["xgb"].set_params(n_jobs=1)
        self.predict_proba([{c: 0.0 for c in RAW_COLUMNS}])  # warm up before traffic

    def predict_proba(self, rows: list[dict[str, float]]) -> np.ndarray:
        frame = pd.DataFrame.from_records(rows, columns=RAW_COLUMNS)
        return self.pipeline.predict_proba(frame)[:, 1]

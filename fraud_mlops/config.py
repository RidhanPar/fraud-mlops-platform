"""Load params.yaml into a plain dict and flatten it for MLflow logging."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PARAMS = ROOT / "params.yaml"


def load_params(path: str | Path = DEFAULT_PARAMS) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def flatten(d: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in d.items():
        name = f"{prefix}{key}"
        if isinstance(value, dict):
            out.update(flatten(value, f"{name}."))
        else:
            out[name] = value
    return out

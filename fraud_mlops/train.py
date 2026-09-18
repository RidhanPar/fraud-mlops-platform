"""Reproducible training run: data -> model -> MLflow -> registry -> gate.

Usage:
    python -m fraud_mlops.train
    python -m fraud_mlops.train --set model.n_estimators=20 --set model.max_depth=2
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import mlflow
import mlflow.sklearn
import numpy as np
import yaml
from imblearn.over_sampling import SMOTE
from imblearn.pipeline import Pipeline
from mlflow import MlflowClient
from mlflow.models import infer_signature
from sklearn.preprocessing import FunctionTransformer
from xgboost import XGBClassifier

from fraud_mlops import data as data_mod
from fraud_mlops.config import ROOT, flatten, load_params
from fraud_mlops.evaluate import classification_metrics, paired_bootstrap_delta, pick_threshold
from fraud_mlops.features import RAW_COLUMNS, TARGET, add_time_features
from fraud_mlops.gate import decide

log = logging.getLogger("train")

CHAMPION = "champion"
PREVIOUS = "previous"


def tracking_uri() -> str:
    return os.environ.get("MLFLOW_TRACKING_URI", f"sqlite:///{(ROOT / 'mlflow.db').as_posix()}")


def build_pipeline(model_cfg: dict[str, Any], seed: int) -> Pipeline:
    steps: list[tuple[str, Any]] = [("features", FunctionTransformer(add_time_features))]
    if model_cfg.get("use_smote", True):
        # imblearn applies SMOTE during fit only, so scoring never sees synthetic rows.
        steps.append(("smote", SMOTE(random_state=seed)))
    steps.append(
        (
            "xgb",
            XGBClassifier(
                n_estimators=model_cfg["n_estimators"],
                max_depth=model_cfg["max_depth"],
                learning_rate=model_cfg["learning_rate"],
                subsample=model_cfg["subsample"],
                colsample_bytree=model_cfg["colsample_bytree"],
                objective="binary:logistic",
                eval_metric="logloss",
                tree_method="hist",
                random_state=seed,
                n_jobs=4,
            ),
        )
    )
    return Pipeline(steps)


def apply_overrides(params: dict[str, Any], overrides: list[str]) -> dict[str, Any]:
    for item in overrides:
        key, raw = item.split("=", 1)
        node = params
        *parents, leaf = key.split(".")
        for p in parents:
            node = node[p]
        node[leaf] = yaml.safe_load(raw)
    return params


def git_state() -> dict[str, str]:
    """Commit the code came from, and whether uncommitted edits were present."""
    try:
        sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, stderr=subprocess.DEVNULL
        ).strip()
        dirty = subprocess.check_output(
            ["git", "status", "--porcelain", "--", "fraud_mlops", "params.yaml"],
            cwd=ROOT,
            text=True,
        ).strip()
        return {"git_sha": sha, "git_dirty": str(bool(dirty)).lower()}
    except Exception:
        return {"git_sha": "unknown", "git_dirty": "unknown"}


def ensure_experiment(name: str) -> str:
    exp = mlflow.get_experiment_by_name(name)
    if exp is not None:
        return exp.experiment_id
    artifacts = os.environ.get("MLFLOW_ARTIFACT_ROOT", (ROOT / "mlartifacts").as_uri())
    return mlflow.create_experiment(name, artifact_location=artifacts)


def score(model: Pipeline, df) -> np.ndarray:
    return model.predict_proba(df[RAW_COLUMNS])[:, 1]


def run(params: dict[str, Any]) -> dict[str, Any]:
    mlflow.set_tracking_uri(tracking_uri())
    reg = params["registry"]
    gate_cfg = params["gate"]
    seed = params["seed"]
    client = MlflowClient()

    data_path = ROOT / params["data"]["path"]
    df = data_mod.load(data_path)
    splits = data_mod.time_split(df, params["data"]["train_frac"], params["data"]["valid_frac"])

    mlflow.set_experiment(experiment_id=ensure_experiment(reg["experiment"]))
    with mlflow.start_run() as active:
        mlflow.log_params(flatten(params))
        mlflow.log_params(
            {
                "data.sha256": data_mod.file_sha256(data_path),
                "data.rows": len(df),
                **{f"data.{k}_rows": len(v) for k, v in vars(splits).items()},
                **{f"data.{k}_frauds": int(v[TARGET].sum()) for k, v in vars(splits).items()},
            }
        )
        mlflow.set_tags(git_state())

        model = build_pipeline(params["model"], seed)
        model.fit(splits.train[RAW_COLUMNS], splits.train[TARGET])

        threshold = pick_threshold(splits.valid[TARGET].to_numpy(), score(model, splits.valid))
        y_hold = splits.holdout[TARGET].to_numpy()
        cand_scores = score(model, splits.holdout)
        cand = classification_metrics(y_hold, cand_scores, threshold)
        mlflow.log_param("threshold", round(threshold, 6))
        mlflow.log_metrics({f"holdout_{k}": v for k, v in cand.items()})

        # Reference sample for drift monitoring: what "normal" input looked like at training time.
        with tempfile.TemporaryDirectory() as tmp:
            ref = splits.train[RAW_COLUMNS].sample(n=min(20_000, len(splits.train)), random_state=seed)
            ref = ref.assign(score=score(model, ref))
            ref.to_parquet(Path(tmp) / "reference.parquet", index=False)
            mlflow.log_artifact(str(Path(tmp) / "reference.parquet"), artifact_path="reference")

        example = splits.valid[RAW_COLUMNS].head(3)
        info = mlflow.sklearn.log_model(
            model,
            name="model",
            signature=infer_signature(example, model.predict_proba(example)),
            input_example=example,
            code_paths=[str(ROOT / "fraud_mlops")],
            registered_model_name=reg["model_name"],
        )
        version = str(info.registered_model_version)
        client.set_model_version_tag(reg["model_name"], version, "threshold", f"{threshold:.6f}")
        client.set_model_version_tag(reg["model_name"], version, "run_id", active.info.run_id)

        # Champion is re-scored on today's holdout rather than trusting its old logged metric.
        champ_metrics = None
        champ_version = None
        try:
            champ_mv = client.get_model_version_by_alias(reg["model_name"], CHAMPION)
            champ_version = str(champ_mv.version)
            champ_model = mlflow.sklearn.load_model(f"models:/{reg['model_name']}@{CHAMPION}")
            champ_scores = score(champ_model, splits.holdout)
            champ_metrics = classification_metrics(
                y_hold, champ_scores, float(champ_mv.tags["threshold"])
            )
            lo, hi = paired_bootstrap_delta(
                y_hold, cand_scores, champ_scores, gate_cfg["bootstrap_rounds"], seed
            )
            mlflow.log_metrics(
                {
                    "champion_holdout_pr_auc": champ_metrics["pr_auc"],
                    "champion_holdout_recall": champ_metrics["recall"],
                    "pr_auc_delta_ci_low": lo,
                    "pr_auc_delta_ci_high": hi,
                }
            )
        except mlflow.exceptions.MlflowException:
            log.info("No champion registered yet")

        decision = decide(cand, champ_metrics, gate_cfg)
        status = "promoted" if decision.promote else "rejected"
        client.set_model_version_tag(reg["model_name"], version, "gate_status", status)
        client.set_model_version_tag(reg["model_name"], version, "gate_reason", decision.reason)
        mlflow.set_tags({"gate_status": status, "gate_reason": decision.reason})

        if decision.promote:
            if champ_version is not None:
                client.set_registered_model_alias(reg["model_name"], PREVIOUS, champ_version)
            client.set_registered_model_alias(reg["model_name"], CHAMPION, version)

    result = {
        "version": version,
        "status": status,
        "reason": decision.reason,
        "threshold": threshold,
        "candidate": cand,
        "champion_version": champ_version,
        "champion": champ_metrics,
    }
    return result


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--params", default=str(ROOT / "params.yaml"))
    parser.add_argument("--set", action="append", default=[], help="override, e.g. model.max_depth=3")
    args = parser.parse_args()

    params = apply_overrides(load_params(args.params), args.set)
    r = run(params)

    print(f"\nmodel version  : v{r['version']}")
    print(f"gate decision  : {r['status'].upper()} ({r['reason']})")
    print(f"threshold      : {r['threshold']:.4f}")
    for name, m in (("candidate", r["candidate"]), (f"champion v{r['champion_version']}", r["champion"])):
        if m:
            print(f"{name:<15}: " + "  ".join(f"{k}={v:.4f}" for k, v in m.items()))


if __name__ == "__main__":
    main()

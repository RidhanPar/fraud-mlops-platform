"""End to end: train twice against a throwaway MLflow registry."""

import copy

import pytest

from fraud_mlops import train
from fraud_mlops.config import load_params


@pytest.fixture
def params(tmp_path, monkeypatch, transactions):
    csv = tmp_path / "tx.csv"
    transactions.to_csv(csv, index=False)
    monkeypatch.setenv("MLFLOW_TRACKING_URI", f"sqlite:///{(tmp_path / 'mlflow.db').as_posix()}")
    monkeypatch.setenv("MLFLOW_ARTIFACT_ROOT", (tmp_path / "artifacts").as_uri())
    p = load_params()
    p["data"]["path"] = str(csv)
    p["model"].update(n_estimators=20, max_depth=3)
    p["gate"].update(min_pr_auc_floor=0.1, bootstrap_rounds=20)
    p["registry"].update(model_name="test-model", experiment="test-exp")
    return p


def test_first_run_promoted_rerun_rejected_and_reproducible(params):
    first = train.run(copy.deepcopy(params))
    second = train.run(copy.deepcopy(params))

    assert first["status"] == "promoted"
    assert second["status"] == "rejected"
    assert second["champion_version"] == first["version"]
    # Same data, same seed, same config: identical holdout metrics.
    assert second["candidate"] == pytest.approx(first["candidate"])

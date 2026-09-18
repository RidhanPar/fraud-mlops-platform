import json

import cloudpickle
import pytest
from fastapi.testclient import TestClient

from fraud_mlops.api import main
from fraud_mlops.api.schemas import MAX_BATCH
from fraud_mlops.audit.hashing import file_sha256
from fraud_mlops.config import load_params
from fraud_mlops.features import RAW_COLUMNS
from fraud_mlops.train import build_pipeline
from tests.conftest import make_transactions


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    out = tmp_path_factory.mktemp("serving_model")
    df = make_transactions()
    cfg = load_params()["model"] | {"n_estimators": 20, "max_depth": 3}
    model = build_pipeline(cfg, 42).fit(df[RAW_COLUMNS], df["Class"])
    with open(out / "model.pkl", "wb") as f:
        cloudpickle.dump(model, f)
    (out / "metadata.json").write_text(json.dumps(
        {"model_version": "7", "threshold": 0.5, "model_sha256": file_sha256(out / "model.pkl")}))

    main.MODEL_DIR = out
    main.DATABASE_URL = f"sqlite:///{(out / 'log.db').as_posix()}"
    with TestClient(main.app) as c:
        yield c


@pytest.fixture
def tx():
    row = make_transactions(n=1).iloc[0]
    return {c: float(row[c]) for c in RAW_COLUMNS} | {"transaction_id": "tx-1"}


def test_ready_reports_model_version(client):
    assert client.get("/ready").json() == {"status": "ready", "model_version": "7"}


def test_single_prediction(client, tx):
    r = client.post("/predict", json=tx)
    assert r.status_code == 200
    body = r.json()
    assert 0.0 <= body["fraud_probability"] <= 1.0
    assert body["is_fraud"] == (body["fraud_probability"] >= 0.5)
    assert body["transaction_id"] == "tx-1"
    assert body["model_version"] == r.headers["X-Model-Version"] == "7"


def test_batch_matches_single(client, tx):
    single = client.post("/predict", json=tx).json()["fraud_probability"]
    batch = client.post("/predict/batch", json={"transactions": [tx, tx, tx]}).json()
    assert batch["count"] == 3
    assert all(p["fraud_probability"] == pytest.approx(single) for p in batch["predictions"])


@pytest.mark.parametrize(
    "mutate",
    [
        lambda t: t.pop("Amount"),                      # missing field
        lambda t: t.update(amount=t.pop("Amount")),     # renamed field
        lambda t: t.update(Amount=-5.0),                # impossible value
        lambda t: t.update(Amount="12.50"),             # string instead of number
        lambda t: t.update(V3=1e9),                     # corrupted value
        lambda t: t.update(merchant="x"),               # unexpected field
    ],
    ids=["missing", "renamed", "negative", "string", "out_of_range", "extra"],
)
def test_bad_input_rejected_not_scored(client, tx, mutate):
    mutate(tx)
    assert client.post("/predict", json=tx).status_code == 422


def test_nan_rejected(client, tx):
    body = json.dumps(tx).replace(str(tx["V1"]), "NaN", 1)
    r = client.post("/predict", content=body, headers={"Content-Type": "application/json"})
    assert r.status_code == 422
    assert r.json()["detail"][0]["field"] == "V1"


def test_errors_do_not_echo_submitted_values(client, tx):
    tx["Amount"] = -123.456
    r = client.post("/predict", json=tx)
    assert r.status_code == 422
    assert "-123.456" not in r.text


def test_empty_and_oversized_batch_rejected(client, tx):
    assert client.post("/predict/batch", json={"transactions": []}).status_code == 422
    too_many = {"transactions": [tx] * (MAX_BATCH + 1)}
    assert client.post("/predict/batch", json=too_many).status_code == 422


def test_every_prediction_is_logged_with_model_version(client, tx):
    store = main.state["store"]
    before = len(store.recent_predictions(100_000))
    client.post("/predict", json=tx)
    client.post("/predict/batch", json={"transactions": [tx, tx]})
    logged = store.recent_predictions(100_000)
    assert len(logged) == before + 3
    assert set(logged.columns) >= set(RAW_COLUMNS) | {"score", "is_fraud"}


def test_feedback_joins_labels_to_predictions(client, tx):
    tx["transaction_id"] = "fb-1"
    client.post("/predict", json=tx)
    r = client.post("/feedback", json={"labels": [{"transaction_id": "fb-1", "is_fraud": True}]})
    assert r.status_code == 202
    labelled = main.state["store"].recent_labelled(10)
    assert labelled.iloc[0]["label"] == True  # noqa: E712


def test_prediction_refused_when_log_unavailable(client, tx, monkeypatch):
    def broken(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(main.state["store"], "record_predictions", broken)
    assert client.post("/predict", json=tx).status_code == 503


def test_metrics_exposed(client, tx):
    client.post("/predict", json=tx)
    body = client.get("/metrics").text
    for name in ("fraud_http_request_duration_seconds_bucket", "fraud_score_bucket",
                 "fraud_rows_scored_total", 'fraud_model_info{model_version="7"}'):
        assert name in body


def test_audit_lookup_returns_decision_with_integrity(client, tx):
    tx["transaction_id"] = "audit-1"
    client.post("/predict", json=tx, headers={"X-Client-ID": "checkout-svc"})
    d = client.get("/audit/transactions/audit-1").json()["decisions"][0]
    assert d["caller"] == "checkout-svc"
    assert d["model_version"] == "7" and d["integrity_ok"] is True
    assert d["input"]["Amount"] == tx["Amount"]
    assert client.get("/audit/transactions/nope").status_code == 404


def test_explain_accounts_for_the_whole_score_and_is_audited(client, tx):
    tx["transaction_id"] = "explain-1"
    score = client.post("/predict", json=tx).json()["fraud_probability"]
    r = client.get("/explain/explain-1?top=5", headers={"X-Client-ID": "analyst-42"})
    assert r.status_code == 200
    e = r.json()["explanation"]
    assert len(e["top_contributions"]) == 5
    assert e["score_from_contributions"] == pytest.approx(score, rel=1e-4)
    asked = client.get("/audit/transactions/explain-1").json()["decisions"][0]["explanations_requested"]
    assert asked[0]["requested_by"] == "analyst-42"


def test_explain_refuses_decisions_from_another_model(client, tx, monkeypatch):
    tx["transaction_id"] = "old-model-1"
    client.post("/predict", json=tx)
    monkeypatch.setattr(main.state["model"], "sha256", "f" * 64)
    r = client.get("/explain/old-model-1")
    assert r.status_code == 409 and "explain_cli" in r.json()["detail"]


def test_bad_client_id_rejected(client, tx):
    assert client.post("/predict", json=tx, headers={"X-Client-ID": "bad id!"}).status_code == 400

"""FastAPI inference service.

Run locally:
    uvicorn fraud_mlops.api.main:app --port 8000
"""

from __future__ import annotations

import logging
import os
import re
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from time import perf_counter

import numpy as np
from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from fraud_mlops.api import metrics
from fraud_mlops.api.model import FraudModel
from fraud_mlops.api.schemas import (
    BatchRequest,
    BatchResponse,
    FeedbackRequest,
    Prediction,
    PredictionResponse,
    Transaction,
)
from fraud_mlops.dburl import app_database_url
from fraud_mlops.api.store import PredictionStore
from fraud_mlops.audit.hashing import record_hash
from fraud_mlops.explain import explain
from fraud_mlops.features import RAW_COLUMNS

log = logging.getLogger("fraud_api")

MODEL_DIR = Path(os.environ.get("MODEL_DIR", Path(__file__).resolve().parents[2] / "serving_model"))
DATABASE_URL = app_database_url()

state: dict = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    model = FraudModel(MODEL_DIR)
    state["model"] = model
    store = PredictionStore(DATABASE_URL) if DATABASE_URL else None
    if store is not None and store.engine.dialect.name == "sqlite":
        store.create_schema()  # tests; in Postgres the migration job owns the schema
    state["store"] = store
    metrics.MODEL_INFO.labels(model.version).set(1)
    yield
    state.clear()


app = FastAPI(title="Fraud Detection Service", version="1.1.0", lifespan=lifespan)


@app.middleware("http")
async def observe(request: Request, call_next):
    if request.url.path == "/metrics":
        return await call_next(request)
    start = perf_counter()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        return response
    finally:
        # Label by route template, never raw path, so label cardinality stays bounded.
        route = request.scope.get("route")
        name = route.path if route is not None else "unmatched"
        metrics.REQUESTS.labels(name, str(status)).inc()
        metrics.LATENCY.labels(name).observe(perf_counter() - start)


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Say which field failed and why, without echoing the submitted values.

    The default handler reflects the input back, which leaks transaction data
    into error logs and crashes outright on NaN because NaN is not valid JSON.
    """
    errors = []
    for e in exc.errors():
        field = ".".join(str(p) for p in e["loc"][1:])
        errors.append({"field": field, "error": e["msg"]})
        leaf = str(e["loc"][-1])
        metrics.VALIDATION_ERRORS.labels(leaf if leaf in RAW_COLUMNS else "other").inc()
    return JSONResponse(status_code=422, content={"detail": errors})


def _model() -> FraudModel:
    model = state.get("model")
    if model is None:
        raise HTTPException(status_code=503, detail="model not loaded")
    return model


_CALLER = re.compile(r"[A-Za-z0-9._:-]{1,64}")


def _caller(value: str | None) -> str:
    """Who asked. Real deployments take this from the authenticated identity
    (mTLS or a gateway token); here it is a header, recorded as given."""
    if value is None:
        return "unidentified"
    if not _CALLER.fullmatch(value):
        raise HTTPException(status_code=400, detail="X-Client-ID must be 1 to 64 of A-Z a-z 0-9 . _ : -")
    return value


def _store() -> PredictionStore:
    store = state.get("store")
    if store is None:
        raise HTTPException(status_code=503, detail="prediction log not configured")
    return store


def _score(txs: list, response: Response, caller: str) -> tuple[str, FraudModel, np.ndarray]:
    """Score, log every prediction, record metrics. Shared by single and batch."""
    model = _model()
    request_id = str(uuid.uuid4())
    rows = [t.model_dump(exclude={"transaction_id"}) for t in txs]
    probs = model.predict_proba(rows)

    store: PredictionStore | None = state.get("store")
    if store is not None:
        try:
            store.record_predictions(
                request_id, caller, model.version, model.sha256, model.threshold, rows,
                [t.transaction_id for t in txs], probs,
            )
        except Exception:
            # Fail closed: a decision that cannot be traced later is not returned.
            metrics.LOG_FAILURES.inc()
            log.exception("prediction log write failed")
            raise HTTPException(status_code=503, detail="prediction log unavailable")

    flagged = int((probs >= model.threshold).sum())
    metrics.ROWS_SCORED.labels("fraud").inc(flagged)
    metrics.ROWS_SCORED.labels("legit").inc(len(probs) - flagged)
    for p in probs:
        metrics.SCORES.observe(float(p))

    response.headers["X-Request-ID"] = request_id
    response.headers["X-Model-Version"] = model.version
    return request_id, model, probs


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness: the process is up."""
    return {"status": "ok"}


@app.get("/ready")
def ready() -> dict[str, str]:
    """Readiness: the model is loaded and traffic can be routed here."""
    return {"status": "ready", "model_version": _model().version}


@app.get("/model")
def model_info() -> dict:
    """Which model version is serving, its lineage and its holdout metrics."""
    return _model().meta


@app.get("/metrics", include_in_schema=False)
def prometheus_metrics() -> Response:
    body, content_type = metrics.render()
    return Response(body, media_type=content_type)


# Plain `def` handlers run in FastAPI's thread pool, so a CPU bound predict
# never blocks the event loop that accepts new connections.
@app.post("/predict", response_model=PredictionResponse)
def predict(
    tx: Transaction,  # type: ignore[valid-type]
    response: Response,
    x_client_id: str | None = Header(default=None),
) -> PredictionResponse:
    request_id, model, probs = _score([tx], response, _caller(x_client_id))
    prob = float(probs[0])
    return PredictionResponse(
        request_id=request_id,
        transaction_id=tx.transaction_id,
        fraud_probability=prob,
        is_fraud=prob >= model.threshold,
        threshold=model.threshold,
        model_version=model.version,
    )


@app.post("/predict/batch", response_model=BatchResponse)
def predict_batch(
    req: BatchRequest, response: Response, x_client_id: str | None = Header(default=None)
) -> BatchResponse:
    request_id, model, probs = _score(req.transactions, response, _caller(x_client_id))
    return BatchResponse(
        request_id=request_id,
        model_version=model.version,
        threshold=model.threshold,
        count=len(probs),
        predictions=[
            Prediction(
                transaction_id=t.transaction_id,
                fraud_probability=float(p),
                is_fraud=bool(p >= model.threshold),
            )
            for t, p in zip(req.transactions, probs)
        ],
    )


@app.post("/feedback", status_code=202)
def feedback(req: FeedbackRequest) -> dict[str, int]:
    """Ground truth arriving later (chargebacks, analyst decisions).

    Joined to logged predictions by transaction_id to measure real recall and
    precision, which is the only way to see performance decay.
    """
    _store().record_labels([(item.transaction_id, item.is_fraud) for item in req.labels])
    return {"accepted": len(req.labels)}


def _public(rec: dict) -> dict:
    return {
        "prediction_id": rec["id"],
        "request_id": rec["request_id"],
        "decided_at": rec["created_at"].isoformat(),
        "caller": rec["caller"],
        "model_version": rec["model_version"],
        "model_sha256": rec["model_sha256"],
        "score": rec["score"],
        "threshold": rec["threshold"],
        "decision": "fraud" if rec["is_fraud"] else "legitimate",
        "input": rec["features"],
        "record_hash": rec["record_hash"],
        "integrity_ok": record_hash(rec) == rec["record_hash"],
    }


@app.get("/audit/transactions/{transaction_id}")
def audit_transaction(transaction_id: str) -> dict:
    """Every decision ever made for a transaction, with inputs and an integrity check."""
    records = _store().predictions_for_transaction(transaction_id)
    if not records:
        raise HTTPException(status_code=404, detail="no decision logged for this transaction")
    out = []
    for rec in records:
        item = _public(rec)
        item["explanations_requested"] = [
            {"requested_at": e["requested_at"].isoformat(), "requested_by": e["requested_by"]}
            for e in _store().explanations_for_prediction(rec["id"])
        ]
        out.append(item)
    return {"transaction_id": transaction_id, "decisions": out}


@app.get("/explain/{transaction_id}")
def explain_decision(
    transaction_id: str, top: int = 10, x_client_id: str | None = Header(default=None)
) -> dict:
    """Justify a logged decision: exact per feature contributions to the score.

    Explains the logged input with the logged model, never a fresh request, so
    the answer is about the decision that was actually made. The request is
    itself recorded, so the audit trail shows who asked for justification and when.
    """
    if not 1 <= top <= 31:
        raise HTTPException(status_code=422, detail="top must be between 1 and 31")
    caller = _caller(x_client_id)
    store, model = _store(), _model()
    records = store.predictions_for_transaction(transaction_id)
    if not records:
        raise HTTPException(status_code=404, detail="no decision logged for this transaction")
    rec = records[-1]
    if record_hash(rec) != rec["record_hash"]:
        raise HTTPException(status_code=409, detail="logged decision fails its integrity check")
    if rec["model_sha256"] != model.sha256:
        raise HTTPException(
            status_code=409,
            detail=(f"decided by model v{rec['model_version']}, this service runs v{model.version}. "
                    "Explain it offline: python -m fraud_mlops.audit.explain_cli "
                    f"--transaction-id {transaction_id}"),
        )
    result = explain(model.pipeline, rec["features"], top=top)
    store.record_explanation(rec["id"], caller, model.version, result)
    return _public(rec) | {"explanation": result}

"""FastAPI inference service.

Run locally:
    uvicorn fraud_mlops.api.main:app --port 8000
"""

from __future__ import annotations

import logging
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from time import perf_counter

import numpy as np
from fastapi import FastAPI, HTTPException, Request, Response
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
from fraud_mlops.api.store import PredictionStore
from fraud_mlops.features import RAW_COLUMNS

log = logging.getLogger("fraud_api")

MODEL_DIR = Path(os.environ.get("MODEL_DIR", Path(__file__).resolve().parents[2] / "serving_model"))
DATABASE_URL = os.environ.get("DATABASE_URL")

state: dict = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    model = FraudModel(MODEL_DIR)
    state["model"] = model
    state["store"] = PredictionStore(DATABASE_URL) if DATABASE_URL else None
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


def _score(txs: list, response: Response) -> tuple[str, FraudModel, np.ndarray]:
    """Score, log every prediction, record metrics. Shared by single and batch."""
    model = _model()
    request_id = str(uuid.uuid4())
    rows = [t.model_dump(exclude={"transaction_id"}) for t in txs]
    probs = model.predict_proba(rows)

    store: PredictionStore | None = state.get("store")
    if store is not None:
        try:
            store.record_predictions(
                request_id, model.version, model.threshold, rows,
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
def predict(tx: Transaction, response: Response) -> PredictionResponse:  # type: ignore[valid-type]
    request_id, model, probs = _score([tx], response)
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
def predict_batch(req: BatchRequest, response: Response) -> BatchResponse:
    request_id, model, probs = _score(req.transactions, response)
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
    store: PredictionStore | None = state.get("store")
    if store is None:
        raise HTTPException(status_code=503, detail="label store not configured")
    store.record_labels([(item.transaction_id, item.is_fraud) for item in req.labels])
    return {"accepted": len(req.labels)}

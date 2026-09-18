"""FastAPI inference service.

Run locally:
    uvicorn fraud_mlops.api.main:app --port 8000
"""

from __future__ import annotations

import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from fraud_mlops.api.model import FraudModel
from fraud_mlops.api.schemas import (
    BatchRequest,
    BatchResponse,
    Prediction,
    PredictionResponse,
    Transaction,
)

MODEL_DIR = Path(os.environ.get("MODEL_DIR", Path(__file__).resolve().parents[2] / "serving_model"))

state: dict[str, FraudModel] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    state["model"] = FraudModel(MODEL_DIR)
    yield
    state.clear()


app = FastAPI(title="Fraud Detection Service", version="1.0.0", lifespan=lifespan)


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Say which field failed and why, without echoing the submitted values.

    The default handler reflects the input back, which leaks transaction data
    into error logs and crashes outright on NaN because NaN is not valid JSON.
    """
    errors = [
        {"field": ".".join(str(p) for p in e["loc"][1:]), "error": e["msg"]}
        for e in exc.errors()
    ]
    return JSONResponse(status_code=422, content={"detail": errors})


def _model() -> FraudModel:
    model = state.get("model")
    if model is None:
        raise HTTPException(status_code=503, detail="model not loaded")
    return model


def _features(tx) -> dict[str, float]:
    return tx.model_dump(exclude={"transaction_id"})


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


# Plain `def` handlers run in FastAPI's thread pool, so a CPU bound predict
# never blocks the event loop that accepts new connections.
@app.post("/predict", response_model=PredictionResponse)
def predict(tx: Transaction, response: Response) -> PredictionResponse:  # type: ignore[valid-type]
    model = _model()
    request_id = str(uuid.uuid4())
    prob = float(model.predict_proba([_features(tx)])[0])
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Model-Version"] = model.version
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
    model = _model()
    request_id = str(uuid.uuid4())
    probs = model.predict_proba([_features(t) for t in req.transactions])
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Model-Version"] = model.version
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

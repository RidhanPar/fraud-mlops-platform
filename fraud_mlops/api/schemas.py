"""Request and response contracts. Anything that does not match is rejected with 422."""

from __future__ import annotations

import os
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, create_model

from fraud_mlops.features import PCA_COLUMNS

MAX_BATCH = int(os.environ.get("MAX_BATCH", "1000"))

# strict: "12.5" as a string is a schema break upstream, not something to coerce.
# forbid: an unknown field usually means a renamed one, so fail loudly.
# allow_inf_nan=False: NaN or Infinity would still produce a confident score.
_STRICT = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

# PCA components in the training data sit well inside +/-150. The wide bound only
# catches corrupted values; genuine outliers still get scored and flagged by drift.
_pca_fields: dict[str, Any] = {
    name: (float, Field(ge=-1_000, le=1_000)) for name in PCA_COLUMNS
}

Transaction = create_model(
    "Transaction",
    __config__=_STRICT,
    transaction_id=(str | None, Field(default=None, max_length=64)),
    Time=(float, Field(ge=0, description="Seconds since the capture window started")),
    Amount=(float, Field(ge=0, le=100_000)),
    **_pca_fields,
)


class BatchRequest(BaseModel):
    model_config = _STRICT
    transactions: list[Transaction] = Field(min_length=1, max_length=MAX_BATCH)  # type: ignore[valid-type]


class Label(BaseModel):
    model_config = _STRICT
    transaction_id: str = Field(min_length=1, max_length=64)
    is_fraud: bool


class FeedbackRequest(BaseModel):
    model_config = _STRICT
    labels: list[Label] = Field(min_length=1, max_length=10_000)


class Prediction(BaseModel):
    transaction_id: str | None
    fraud_probability: float
    is_fraud: bool


class PredictionResponse(Prediction):
    request_id: str
    threshold: float
    model_version: str


class BatchResponse(BaseModel):
    request_id: str
    model_version: str
    threshold: float
    count: int
    predictions: list[Prediction]

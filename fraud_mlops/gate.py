"""Promotion gate: a candidate only becomes champion if it beats the current one.

The decision is a pure function so it can be unit tested without MLflow.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class GateDecision:
    promote: bool
    reason: str


def decide(
    candidate: dict[str, float],
    champion: dict[str, float] | None,
    cfg: dict[str, Any],
) -> GateDecision:
    metric = cfg["primary_metric"]

    if champion is None:
        if candidate[metric] >= cfg["min_pr_auc_floor"]:
            return GateDecision(True, f"no champion; {metric} {candidate[metric]:.4f} meets floor")
        return GateDecision(False, f"no champion; {metric} {candidate[metric]:.4f} below floor")

    gain = candidate[metric] - champion[metric]
    if gain < cfg["min_improvement"]:
        return GateDecision(
            False,
            f"{metric} gain {gain:+.4f} below required {cfg['min_improvement']:+.4f}",
        )

    recall_drop = champion["recall"] - candidate["recall"]
    if recall_drop > cfg["max_recall_drop"]:
        return GateDecision(
            False, f"recall dropped {recall_drop:.4f}, limit {cfg['max_recall_drop']:.4f}"
        )

    return GateDecision(True, f"{metric} gain {gain:+.4f}, recall change {-recall_drop:+.4f}")

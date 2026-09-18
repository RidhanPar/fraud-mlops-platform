# Retraining trigger policy

Owner: fraud model team. Enforced by [`fraud_mlops/retrain_policy.py`](../fraud_mlops/retrain_policy.py),
which reads the monitoring signals from Prometheus and returns one action with its reasons.

## Principle

**A retrain is a response to the world changing, not to an alert firing.** The same symptom
can need opposite responses:

- If an upstream fault sends bad inputs, the model is fine and the data is broken. Retraining
  on that data would teach the model to ignore a real signal (in the V14 incident, it would
  learn that V14 carries no information). **Fix the data.**
- If fraudsters change behaviour, the inputs are genuine and the model is out of date.
  **Retrain.**

## Decision order

The policy checks conditions in this order and stops at the first match.

| # | Condition (sustained over the window) | Action |
|---|---|---|
| 1 | A feature holds one exact value in >50% of traffic while it almost never repeats in training (`FeatureStuckValue`), **or** >5% of requests are rejected as invalid | **FIX_DATA.** Do not retrain. Page the upstream owner, exclude the window from future training data, route decisions to fallback rules or manual review, rescore the window after recovery |
| 2 | Recall on labelled traffic stays below 0.5 (release: 0.70), with at least 10 confirmed frauds in the window, and no data fault | **RETRAIN_NOW.** Train on data including matured recent labels. The Phase 1 promotion gate still decides, then shadow before switching the champion alias |
| 3 | A feature's PSI stays above its calibrated threshold, **or** score PSI stays above 0.1, **or** the flag rate is outside 0.2x to 5x the training rate, without label confirmation | **RETRAIN_CANDIDATE.** Train a candidate and let the gate decide; keep watching recall as labels mature |
| 4 | Model older than 30 days | **SCHEDULED_RETRAIN** through the gate |
| 5 | None of the above | **NO_ACTION.** Notes when performance is unconfirmed for lack of labels |

## Why each rule is shaped this way

- **Data faults come first.** In the V14 incident recall fell to 0.00, which on its own
  would say "retrain". Checking for faults first prevents the worst possible response.
- **Sustained, not momentary.** Every condition must hold for the whole window
  (`min_over_time` or `max_over_time` in Prometheus). A single noisy cycle never triggers
  a retrain.
- **Recall needs enough labels.** With about 0.1% fraud, a window of 3 confirmed frauds
  gives recall of 0, 0.33, 0.67 or 1.0. The policy refuses to act on that.
- **Drift alone is a candidate, not a command.** This data drifts every day (V1 PSI ~1.5)
  without hurting the model. A retrain costs review time and carries risk, so drift only
  produces a candidate, and the gate must see a real improvement on held out data.
- **Retrains always go through the gate.** No trigger bypasses the Phase 1 rule that a
  candidate must beat the champion on the same holdout.

## Windows

| Setting | Production | Demo |
|---|---|---|
| Persistence window | 24 hours | 2 minutes |
| Label lag | weeks (chargebacks) | 45 seconds |
| Label maturity before training | 60 to 90 days | n/a |

Label maturity matters for training data: a transaction from last week labelled "legitimate"
may still get a chargeback. Training only on matured labels avoids teaching the model that
recent fraud was legitimate.

## Running it

```bash
python -m fraud_mlops.retrain_policy                                  # now, 24h window
python -m fraud_mlops.retrain_policy --window 2m --at 2026-09-18T11:40:00Z
```

## Evidence: two incidents, same alert, opposite decisions

Both incidents were replayed against the live stack on 2026-09-18 with
`scripts/simulate_drift.py` (real holdout transactions, labels 45 s late). The policy was then
evaluated with a 2 minute window at the end of each phase. Raw outputs are in
[`docs/retraining/`](retraining/).

| | V14 upstream outage | Fraudster evasion |
|---|---|---|
| What changed | V14 zero filled for every transaction | fraud transactions shifted 70% towards the average legitimate one |
| Alerts | FeatureStuckValue (+59 s), FeatureDrift (+89 s), FraudFlagRateCollapsed, ModelRecallDegraded | **only** ModelRecallDegraded (+2 min 38 s) |
| Stuck features / drifting features | V14 / V14 | none / none |
| Score PSI | flat | flat (0.007) |
| Recall over the window | best 0.07 (26 labelled frauds) | best 0.46 (25 labelled frauds) |
| **Policy decision** | **FIX_DATA**: "recall 0.07 is a symptom of the fault, not model decay" | **RETRAIN_NOW**: "recall stayed below 0.5 for 2m with no data fault detected" |
| At end of baseline | NO_ACTION | NO_ACTION |

The recall alert fired in both. A policy of "retrain when recall drops" would have retrained
on zero-filled V14 data in the first case and taught the model to ignore its strongest feature.

Honest caveats: the evasion scenario is synthetic (a linear shift of fraud rows), and at 0.46
it cleared the 0.5 floor by a small margin. The first baseline evaluation reported "performance
unconfirmed" because the database had just been reset and labels had not accumulated.

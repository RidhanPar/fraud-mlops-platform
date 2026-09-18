# Drift event: upstream feature zero filled

A simulated incident run against the full local stack on 2026-09-18. All numbers come from
Prometheus and the alert log ([summary.json](summary.json)). The simulation is
[`scripts/simulate_drift.py`](../../scripts/simulate_drift.py).

![Drift event](drift_event.png)

## Setup

- 179,250 real holdout transactions replayed at 250 per second through the API.
- Fraud labels posted 45 seconds after each prediction, standing in for chargebacks.
- Three phases: baseline 3 min, incident 5 min, recovery 4 min.
- **Incident:** the upstream feature service fails and sends `V14 = 0` for every transaction.
  V14 is the model's most important feature (44% of total gain).

## What each safeguard saw

| Safeguard | Result | Why |
|---|---|---|
| Request validation | **passed every request** | 0 is a valid number within range |
| Prediction drift (score PSI) | **stayed at 0.016, no alert** | 99.9% of traffic is legitimate and still scores near 0, so the overall distribution barely moves |
| Feature drift on V14 | PSI 0.12 → **8.28**, alert at **+85 s** | direct view of the broken input |
| Fraud flag rate | 0.09% → **0%**, alert at **+130 s** | the model stopped flagging anything |
| Recall on labelled traffic | 0.67 → **0.00**, alert at **+150 s** | confirmed by ground truth once labels arrived |
| False alarms during baseline | **none** | calibrated per feature thresholds |

Times are from incident start to the alert firing, including each rule's `for:` duration.

After the fix, all three alerts resolved within 40 to 120 seconds. Recall was last to
clear because it waits for fresh labels.

## The lessons

1. **Validation is necessary but not sufficient.** A schema check can't tell a real 0 from
   a default 0.
2. **Prediction drift missed it.** This is the counter-intuitive result. For a rare event
   model, the score distribution is dominated by the majority class, so a model can go
   completely blind to fraud while its output distribution looks normal. Watch the flag rate
   (the tail), not just the shape of all scores.
3. **Feature drift caught it first, but only because it was calibrated.** Several PCA
   features drift day to day with PSI up to 1.5 while the model stays accurate. A flat 0.25
   threshold would page every day and be ignored. Per-feature thresholds from known good
   traffic kept V1 and V3 quiet and still caught V14 within a minute and a half.
4. **Labels are the ground truth, but they are late.** In this simulation the lag was 45
   seconds. For real card fraud, chargebacks take weeks, so recall alerts confirm an incident
   long after drift alerts have raised it. You need both: fast proxies to detect, and labels
   to confirm.
5. **Ranking held up while decisions failed.** Once labels caught up, PR AUC on labelled
   traffic ranged between 0.55 and 0.75 during the incident, while recall sat at 0. Without V14, fraud still ranks above most legitimate traffic, but
   no longer reaches the 0.994 threshold. A metric that ignores the threshold would have
   missed the business impact.

## What a responder would do

1. FeatureDrift names V14, so check the upstream feature service first. That's minutes of
   diagnosis instead of hours.
2. Mitigate: with fraud detection effectively off, route transactions to fallback rules
   or manual review until V14 is restored.
3. After recovery, rescore the transactions logged during the incident. The prediction log
   holds every input, so the affected window can be found and reviewed.
4. Follow up: add a "too many exact zeros" check for key features at the feature service.
   That's a cheaper, earlier signal than drift.

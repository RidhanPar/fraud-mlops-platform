# Auditing, explainability and governance

Everything below was run against the live docker compose stack on 2026-09-18. Raw
outputs: [tamper_demo.json](audit/tamper_demo.json), [explanations_demo.json](audit/explanations_demo.json).

## What is recorded for every prediction

| Field | Why it is there |
|---|---|
| `request_id`, `transaction_id` | find the decision later, join chargebacks to it |
| `created_at` (UTC) | when the decision was made |
| `caller` | who asked (`X-Client-ID`; a real deployment takes this from mTLS or a gateway token) |
| `features` | the exact input as received, after validation |
| `score`, `threshold`, `is_fraud` | the output and the rule that turned it into a decision |
| `model_version`, `model_sha256` | which model, and proof it was these exact bytes |
| `record_hash` | SHA-256 over all of the above, computed at write time |

The row is written **before** the response is returned. If the write fails, the API returns
503 (fail closed): a decision that cannot be traced is not made. Explanation requests are
recorded too, in `explanations`, with who asked and when.

> **Correction (2026-09-19).** The first sealer cut off rows by time, which leaves holes in a
> sealed range under concurrent load: rows with lower ids can be stamped slightly later. It passed
> the checks below, but later load runs produced seals that no longer verified, locally and on AWS.
> It now seals by id behind a lock barrier. Full diagnosis:
> [ISSUES_AND_FIXES.md](ISSUES_AND_FIXES.md#the-sealer-race-rows-appeared-inside-an-already-sealed-range).

## Three layers of protection

1. **Least privilege.** The schema is created by a one-shot migration job running as the
   database admin. The API, monitor and auditor connect as `fraud_app`, which has `INSERT` and
   `SELECT` only. A compromised service cannot rewrite history.
2. **Append-only triggers.** `UPDATE`, `DELETE` and `TRUNCATE` on the audit tables raise an
   error, even for the table owner.
3. **Hash chain seals.** An auditor service seals new rows every 30 seconds:
   `rows_digest = sha256(record hashes in id order)` and
   `seal_hash = sha256(previous seal | first id | last id | rows_digest)`. Editing, deleting or
   inserting a row inside a sealed range, or rewriting a seal, is detected by
   `python -m fraud_mlops.audit.verify`.

Sealing runs off the request path. A single global chain written by the API would force
every worker to serialise on one lock. The sealer cuts off by id behind an advisory lock
barrier, so no row below the cut off can still be uncommitted (see the correction above).

## Tamper demo

Target: prediction 239173, a fraud decision with score 0.9996, sealed in seal 37 of 37
(239,200 rows in the log).

| Attempt | Result |
|---|---|
| App role runs `UPDATE ... SET score = 0.01, is_fraud = false` | **refused:** permission denied for table predictions |
| Database admin runs the same | **refused:** audit table predictions is append only |
| Admin disables the trigger, edits the row **and recomputes its record hash** | edit lands; verifier: **"seal 37: rows in ids 236401..239200 changed since sealing"** |
| Original values restored | verifier passes again |

The third attempt is the realistic threat: someone with full database control who knows how
the row hash is built. The seal chain catches it because the seals were written before the
edit. In production the seal hashes would also be copied somewhere the database admin cannot
reach (S3 Object Lock, or a transparency log), so rewriting the whole chain is caught too.

## Explainability on demand

`GET /explain/{transaction_id}` explains the **logged** input with the **logged** model. It
never takes a fresh payload, so the answer is about the decision that was actually made.

- **Method:** exact TreeSHAP through XGBoost's `pred_contribs`. A test shows it matches the
  `shap` library exactly. It needs no extra dependency in the serving image and takes about
  6 ms per row, against 57 ms for the `shap` library.
- **Contributions add up exactly.** The base value plus the contributions equals the logged
  score's log odds, so the explanation accounts for the whole score.
- **Why on demand is enough:** the same model bytes plus the same input always give the same
  explanation. Storing the input and `model_sha256` means any past decision can be explained
  later without paying for SHAP on every request.
- **Decisions by older models:** the API returns 409 and points to
  `python -m fraud_mlops.audit.explain_cli`. That tool loads the named version from the MLflow
  registry, **refuses unless its file hash matches the hash logged with the decision**, then
  explains it. It reproduced the API's answer and the logged score (0.874121) exactly.

### Two real decisions from the drift runs

**Fraud caught during normal traffic** (`sim-142815-2539`, score 0.9995, flagged):

| Feature | Value | Contribution (log odds) |
|---|---|---|
| V14 | −8.72 | **+5.18** towards fraud |
| V17 | −7.16 | +1.57 |
| V28 | 1.41 | −1.10 |
| V12 | −5.97 | +0.94 |

**Fraud missed during the V14 outage** (`sim-142815-41108`, score 0.874, below 0.994, passed as legitimate):

| Feature | Value | Contribution (log odds) |
|---|---|---|
| V14 | **0.00** | **−2.21** towards legitimate |
| V17 | −2.08 | +2.16 |
| V4 | 3.25 | +1.56 |
| V12 | −3.81 | +1.34 |

The second explanation is the kind an investigator needs. Several features still pointed at
fraud, but the zero-filled V14 pulled the score down by 2.2 log odds and it fell short of the
threshold. It pins the missed fraud on the upstream fault, not on the model.

**Limitation:** V1 to V28 are anonymised PCA components, so "V14 pushed the score up" is not
something a customer or regulator can act on. A production system maps contributions onto
named, business-meaningful features and approved reason codes.

## Cost

Load test at 2 CPUs, 2 workers (same setup as Phases 2 and 3):

| | Phase 3 (log only) | Phase 4 (log + hash + auditor) |
|---|---|---|
| single, 1 client, p50 | 10.7 ms | 9.1 ms |
| single, 8 clients, p99 | 90 ms | 90 ms |
| batch of 1000, rows/s | 5,951 | 5,059 |

Single requests show no measurable cost (the difference is run-to-run noise). The 1,000-row
batch is about 15% slower. It was not profiled whether that is the hashing or the larger
table (240k rows by then).

## What a regulated deployment would add

- Identity from mTLS or an API gateway, not a self-declared header.
- Seal hashes shipped to WORM storage or an external transparency log.
- Retention and deletion rules. GDPR erasure conflicts with append-only storage; the usual
  answer is to encrypt personal fields per subject and delete the key (crypto-shredding).
- Schema migrations with Alembic instead of `create_all`.
- Secrets from a secrets manager instead of compose environment variables.

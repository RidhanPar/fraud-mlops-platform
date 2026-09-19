# Issues faced and how they were solved

A record of every real problem hit while building this project: what was seen, how it was
diagnosed, the root cause, the fix, and the evidence. Several first guesses were wrong; those are
kept in, because how a wrong guess got caught is often the most useful part of the story.

## The five best interview stories

| # | Story | Why it lands |
|---|---|---|
| 1 | [The sealer race](#the-sealer-race-rows-appeared-inside-an-already-sealed-range) | Found in production, first hypothesis wrong, root cause proven exactly, reproduced locally (288 failures vs 0), fix verified on 553,440 rows |
| 2 | [Prediction drift was blind to a broken model](#prediction-drift-did-not-fire-when-the-model-went-blind) | Counter intuitive: recall 0.70 → 0.00 while the score distribution stayed normal |
| 3 | [The 0.999 AUC did not reproduce](#the-claimed-0999-auc-did-not-reproduce) | Honesty about my own earlier work; the real number is 0.963 / PR AUC 0.776 |
| 4 | [Same alert, opposite fixes](#one-recall-alert-two-opposite-correct-responses) | Retraining on the outage data would have made the model worse |
| 5 | [My load test lied twice](#the-first-load-test-measured-the-wrong-thing) | Knowing how to check that a benchmark measured the service, not the client |

---

## Phase 1: model, registry and gate

### The claimed 0.999 AUC did not reproduce
- **Seen:** the existing model was described as 0.999 ROC AUC; retraining it on a time ordered holdout gave 0.963.
- **Diagnosed:** scored the same model three ways. Random 80/20 split: 0.965. Time ordered split: 0.963. Its own training data: 1.000.
- **Root cause:** the 0.999 was measured on data the model had already seen (most likely the SMOTE resampled training set).
- **Fix:** every metric is now computed on a held out, time ordered slice and logged to MLflow with the data hash.
- **Say:** "My own earlier number was wrong, and I can show exactly why."

### Pickled data from the old project would not load
- **Seen:** `TypeError` from pandas when unpickling the saved train and test splits.
- **Root cause:** pickles are tied to library versions; the splits were saved with a different pandas.
- **Fix:** the pipeline retrains from the raw CSV every time; processed data is never a hand-off format.

### Two features could not exist in production
- **Seen:** the scaler was fit on the full dataset before splitting (test data leaked into training), and `Time` meant "seconds since the dataset started", which a live transaction does not have.
- **Fix:** removed the scaler (trees do not need it) and replaced `Time` with hour of day.

### ROC AUC would have promoted a worse model
- **Seen:** a weaker candidate (20 trees, depth 2) scored **higher** ROC AUC (0.974 vs 0.963) but lower PR AUC (0.739 vs 0.776).
- **Root cause:** with 0.17% fraud, ROC AUC is flattered by the huge number of easy negatives.
- **Fix:** the promotion gate uses PR AUC with a minimum gain and a recall guard, plus a paired bootstrap interval.

### Lineage pointed at the wrong repository
- **Seen:** runs logged a git commit from the parent folder's repository.
- **Fix:** the project got its own repository, and every run also records whether code had uncommitted edits (`git_dirty`). Registry rebuilt from committed code.

## Phase 2: serving

### NaN input crashed the error handler
- **Seen:** a test sending NaN got a 500, not a 422.
- **Root cause:** FastAPI's default validation handler echoes the input back, and NaN is not valid JSON, so building the error response failed. Echoing input also leaks transaction data into logs.
- **Fix:** custom handler that names the field and the problem, never the value. Tests cover NaN, strings, renamed and extra fields.

### A 1.97 GB image for a CPU service
- **Diagnosed:** `du` inside the image showed 454 MB of NVIDIA libraries pulled in by the XGBoost wheel.
- **Fix:** `xgboost-cpu` wheel, same version and API: 801 MB.

### The first load test measured the wrong thing
- **Seen (1):** 48 ms p50 for one client, while 100-row batches returned faster.
- **Root cause:** `localhost` on Docker Desktop for Windows adds about 40 ms; `127.0.0.1` gave 6.7 ms.
- **Seen (2):** throughput *fell* as clients were added, while the container used only 144% of its 400% CPU budget.
- **Root cause:** the single-process load generator on the host was the bottleneck.
- **Fix:** generator moved into its own multi-process container. Result: a flat ~275 req/s ceiling with latency rising in line with queueing theory (32 clients / 274 req/s ≈ 117 ms, measured 113 ms).

## Phase 3: monitoring and drift

### Real data drifts every day without hurting the model
- **Seen:** V1 PSI ~1.5 and V3 ~1.1 between training and holdout, yet score PSI 0.01 and holdout PR AUC fine.
- **Diagnosed:** tested whether time of day explained it by comparing against the same hours of day 1. Drift got *larger*, so the hypothesis was rejected; V3's median genuinely moved from +0.73 to −0.72 between two evenings.
- **Fix:** per feature thresholds calibrated on known good traffic, `max(0.25, 1.5 × worst PSI seen)`. Zero false alarms on the holdout.

### Prediction drift did not fire when the model went blind
- **Seen:** with V14 zero filled, recall fell from 0.70 to 0.00 but score PSI stayed at 0.016 (local) and 0.0205 (AWS).
- **Root cause:** 99.9% of traffic is legitimate and still scores near zero, so the overall score distribution barely moves when fraud stops being caught.
- **Fix:** alert on the flag rate (the tail), feature drift and stuck values, and confirm with recall on labels.
- **First scenario tried was weak:** sending Amount in pence was the first idea, but Amount barely matters to the model (recall 0.703 → 0.689), so it was replaced with the V14 outage after measuring impact offline.

### The monitor crashed on start
- **Root cause:** it inherited the API image's multi-process metrics setting but runs as a single process.
- **Fix:** unset `PROMETHEUS_MULTIPROC_DIR` for the monitor.

### Stale gauges could keep an alert firing
- **Seen:** when the window had too little traffic, the monitor left old values in place.
- **Fix:** publish "unknown" (NaN) instead, with a test. Found because my own profiling rows (5,000 identical synthetic inserts) showed up as massive drift.

### Measured the cost of logging every decision
- **Seen:** synchronous, fail closed logging added about 4 ms at p50 (6.5 → 10.7 ms) and slowed 1,000-row batches 5x.
- **Decision:** kept it synchronous; an untraceable decision is worse than 4 ms. At higher volume the pattern would be a durable queue.

### A claim in my own write-up was checked and corrected
- I first wrote that PR AUC stayed "about 0.7" during the incident from a screenshot; querying Prometheus showed 0.55 to 0.75. Numbers in docs come from queries, not from reading charts.

## Phase 4: audit and governance

### One recall alert, two opposite correct responses
- **Seen:** the V14 outage and a fraudster evasion scenario both fired the recall alert.
- **Fix:** the retraining policy checks data faults first, using a new stuck value signal. V14 outage → FIX_DATA ("recall is a symptom of the fault"); evasion → RETRAIN_NOW. Retraining on the outage data would have taught the model to ignore its best feature.

### The auditor made endless one-row seals
- **Root cause:** its loop kept sealing each row as it aged past the settle window.
- **Fix:** continue only while chunks come back full.

### The verifier could skip rows
- **Seen (code review):** a row inserted between two sealed ranges was silently ignored.
- **Fix:** it is now reported as "added after sealing".

### Unpickling before checking the hash
- **Fix:** the model file's SHA-256 is checked against its metadata **before** unpickling, since a tampered pickle can run code.

## Phase 5: AWS and CI

### The sealer race: rows appeared inside an already sealed range
- **Seen:** on AWS, the verifier reported "seal 2: expected 6650 rows, found 6659" **before any tampering**.
- **First hypothesis (wrong):** slow commits beyond the 10 s settle window. The load test's worst request was 6.6 s, so that did not fit.
- **Second check (flawed, then fixed):** I tested a time filter hole using `sealed_at − 10 s` as the cut off; it found nothing. The code actually computed the cut off at the *start* of the pass. Searching for the cut off that reproduces the stored digest found it: excluding exactly rows 8571 to 8581 reproduces the seal's digest bit for bit.
- **Root cause:** under concurrency, id order and timestamp order diverge. Those rows were stamped 0 to 20 ms after the cut off but received lower ids than row 8582, stamped before it. A time filter over an id range leaves holes.
- **Fix:** a lock barrier. Each insert takes a shared advisory lock until commit; the sealer takes it exclusively (waiting for in-flight inserts, briefly holding new ones), reads the highest id, and seals by id only. A snapshot-based approach was considered and rejected because sequence values are drawn slightly before a transaction gets its id.
- **Evidence:** local race test on real Postgres with 16 writers and random commit delays: old algorithm **288 failures in 20 s**, barrier **0** (and 0 again with 24 writers and 1 s delays). On AWS after deploying: **553,440 rows, 57 seals, only the historical seal 2 hole**. The same bug was found in the local Phase 4 database too.
- **Also fixed:** the race test harness itself hung at first; 16 writers shared a pool of 10 connections, the sealer thread starved and died, and non-daemon threads kept the process alive.
- **Say:** "The tamper detector caught a real bug in my own sealer. I proved which nine rows and why, reproduced it, fixed it, and verified the fix at scale."

### Credentials saved under the wrong profile
- **Seen:** boto3 found no credentials for the new profile.
- **Diagnosed:** printed only section and key names of the AWS files: the new key was under `[default]`, replacing an older key.
- **Fix:** pointed Terraform and scripts at `default` rather than editing credential files.

### IAM policy missing one permission
- **Seen:** load balancer creation denied: `ec2:GetSecurityGroupsForVpc`.
- **Root cause:** `ec2:Describe*` does not cover actions that start with `Get`.
- **Fix:** one line added; Terraform resumed from where it stopped (49 resources already in place).

### CloudWatch metric math rejected
- **Seen:** `MAX([requests, 1])` is invalid; scalars cannot be mixed into a metric array.
- **Fix:** `IF(requests > 0, 100 * errors / requests, 0)`.

### The image scanner silently scanned nothing, then found critical CVEs
- **Seen:** no scan results despite scan on push.
- **Root cause:** Docker's builder pushed an OCI image index with provenance attestations, which ECR basic scanning cannot scan.
- **Fix:** build with `--provenance=false`. The scan then showed 2 critical and 2 high findings, three in curl, which was only there for the health check. Replaced with a Python check and applied OS security updates: **0 critical**, 1 high (zlib in the base image, no fix available yet, accepted and documented).

### Large batches were slow, and my first guess was wrong
- **Guess:** the small database.
- **Metrics said:** RDS CPU under 20%, API tasks at 100% CPU. Validating, hashing and inserting 1,000 rows is CPU work; the fix is more or bigger API tasks.

### p99 spikes while the API was idle (unresolved)
- **Seen:** p50 14 ms but p99 0.2 to 5 s during light traffic, with API CPU at 5 to 10%.
- **Diagnosed:** RDS swapping up to 148 MB with ~55 MB free; the log (326 MB) larger than Postgres's cache (180 MB); the monitor's queries fast on the server (12 ms, 141 ms).
- **Blocked:** resizing to db.t4g.small was refused by the account's Free plan, so the cause is likely but not proven. Documented as such.

### A near-miss false alarm
- **Seen:** during normal traffic the flag rate ratio dipped to 0.19, under the 0.2 collapse threshold; only the two-period rule stopped the alarm.
- **Root cause:** the threshold is relative to the *training* flag rate, and live traffic normally runs at 0.2 to 0.4 of it.
- **Not yet fixed:** it should be calibrated on known good traffic, like the drift thresholds.

### An integration that was never exercised was removed
- A GitHub OIDC role for CI was written in Terraform, but CI never pushed images (the real model is not in git). Rather than ship an untested integration, it was removed. CI builds and smoke tests the image around a synthetic stand-in model instead.

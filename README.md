# Document Automation Agent

An accounts-payable pipeline for scanned supplier invoices. A vision model reads
the page, a business-rule engine checks what it read against the vendor master,
the purchase orders and the ledger, and a confidence-plus-rules router decides
whether to post the invoice, hand it to a human, or reject it. Every per-field
confidence is logged, and each batch is scored against a baseline so a drop in
extraction quality raises an alert instead of quietly filling the review queue.

Runs with **no API key** — the vision step falls back to a deterministic
text-layer extractor, so the whole pipeline, the eval suite and CI are
reproducible. Set the Azure OpenAI variables to use `gpt-4o` vision instead.

```
 landing zone (page images)
          │
          ▼
   ┌─────────────┐   per-field value + confidence + evidence
   │  extraction │   Azure OpenAI vision, or the offline text-layer parser
   └──────┬──────┘
          ▼
   ┌─────────────┐   12 rules over the vendor master, the POs and the ledger
   │    rules    │   blocker / review / warning, with not_evaluated as its own outcome
   └──────┬──────┘
          ▼
   ┌─────────────┐
   │   routing   │──► auto_post ────► posting ledger
   │             │──► needs_review ─► human review console ──► posts on approval
   │             │──► rejected ─────► returned to the vendor
   └──────┬──────┘
          ▼
   ┌─────────────┐   PSI on the confidence distribution, null rate, auto-post rate
   │    drift    │   alerts persisted; a critical alert fails the batch job
   └─────────────┘
```

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python data/generate_corpus.py           # 200 synthetic invoices: pages, text layer, truth
python -m docagent.cli run               # process every batch
python -m docagent.cli queue             # what stopped, and why
python -m docagent.cli doc DOC-0014      # one document end to end
python evals/run_eval.py                 # the eval suite
streamlit run console/app.py             # human review console
uvicorn docagent.api:app --port 8080     # the service Kubernetes runs
pytest -q                                # 61 tests
```

## What one document looks like

```
$ python -m docagent.cli doc DOC-0014
DOC-0014   action: needs_review   batch: batch-01
FIELDS
    invoice_number   INV-2026-3014      0.98  exact invoice_number format match
    po_number        PO-96680           0.98  exact po_number format match
    total_amount     166705.68          0.98  exact total_amount format match
RULES
  [ok ] R01 Critical fields present      all critical fields were read
  [ok ] R02 Totals reconcile             subtotal + tax matches the total (166,705.68)
  [FAIL] R06 Purchase order exists        PO-96680 is not an open purchase order
  [n/a] R07 PO belongs to the vendor     PO-96680 is not in the purchase order master
ROUTING
  - R06 Purchase order exists: PO-96680 is not an open purchase order
```

## Measured results

`python evals/run_eval.py` over 200 documents, offline backend:

| metric | threshold | measured |
|---|---|---|
| escaped critical errors (auto-posted with a wrong critical field) | 0 | **0** |
| field accuracy on auto-posted documents | ≥ 0.995 | **1.000** |
| field accuracy overall | ≥ 0.90 | 0.944 |
| review recall (documents needing a human that got one) | ≥ 0.95 | 0.988 |
| review precision (reviewed documents that needed it) | — | 0.933 |
| auto-post rate (steady-state batches) | — | 61% |
| handling-time speedup (steady-state) | — | **6.0×** |
| drift detected on the degraded batch | yes | 9 alerts |
| false drift alerts on healthy batches | 0 | **0** |

The headline number is the first row. For a pipeline that posts to a ledger,
"how accurate is the model" is the wrong question — what matters is what got
through without a human that should not have. Nothing did.

Handling time is modelled explicitly, not asserted: 210 s to key and match an
invoice by hand against 90 s for an exception review where the fields are
pre-filled and the reason is on screen. Both are config values
(`DOCAGENT_MANUAL_SECONDS`, `DOCAGENT_REVIEW_SECONDS`), so the speedup moves when
you change your assumptions.

## The four pieces worth reading

### 1. Confidence that is computed, not asserted (`docagent/extraction/offline.py`)

The offline extractor reads the page's text layer and scores each field from
evidence it can actually see: whether the value matches that field's strict
format, how many glyph repairs it had to make (`'O'` read as `'0'` in a digit
slot), and how much of the value is illegible. It never consults ground truth,
so the accuracy the evals report is real. The Azure backend asks `gpt-4o` for the
same shape — value, confidence, evidence per field — and a field whose value
could not be parsed has its confidence forced to zero whatever the model claimed.

The corpus plants two different kinds of noise on purpose. A glyph swap is
recoverable — in a digit slot it has one sensible reading, so the extractor
repairs it and lowers its confidence. A **digit transposition** is not: the
result is still a valid number, the extractor cannot know it misread, and only a
business rule or a human catches it. That is what keeps field accuracy honestly
below 100%.

### 2. Rules that say when they could not run (`docagent/rules/`)

Twelve rules over the vendor master, the purchase orders and the posting ledger:
totals reconcile, line items sum to the subtotal, the tax rate is one we know,
the vendor is active, the PO exists and belongs to that vendor, the total is
within its authorisation, the currency matches, it is not a duplicate, the dates
are sane, it is under the auto-approval limit.

Each returns `pass`, `fail` **or `not_evaluated`** — the third is its own
outcome, not a silent pass, because a rule that could not run is exactly the case
a human needs to see. Severity is `blocker`, `review` or `warning`.

### 3. Routing that knows when not to trust itself (`docagent/routing.py`)

Confidence alone is not enough: a confident reading of a duplicate invoice is
still a duplicate. Rules alone are not enough either — a rule evaluated on a
misread field is a wrong answer stated firmly. So:

- a **blocker only rejects when the reading behind it is confident**. On a faint
  page the right answer is a human, not a refusal the vendor has to chase;
- any review-severity failure, any unevaluated blocker, any critical field under
  0.92 or any field under 0.80 routes to a human;
- everything else posts.

Every trigger is recorded, so the queue explains itself and the console shows the
reviewer exactly why the document stopped.

### 4. Drift that fires on the right thing (`docagent/drift.py`)

There are no labels at run time, so the alarm is not "the model is wrong", it is
"the model is behaving differently from the batch we accepted". The baseline is a
per-field confidence histogram plus the null rate and the auto-post rate; each
batch is scored with the population stability index.

PSI on its own fires on distributions that moved *for the better* — the first
version of this alerted on three healthy batches. An alert now also requires the
share of readings under the routing floor to have **increased**, which took false
alarms on healthy batches to zero while still catching all nine real signals on
the degraded batch, including the auto-post rate falling from 55% to 30%.

## Deployment

`deploy/` holds a non-root, read-only-root-filesystem container and the
Kubernetes manifests: a 2-replica API Deployment with startup, readiness and
liveness probes, a Service, an HPA on CPU, a PVC for state, and a nightly
CronJob that runs the batch ingest. The CronJob passes
`--fail-on-critical-drift`, so a drifted batch fails the job and surfaces in job
alerting instead of quietly filling the review queue. `/metrics` is Prometheus
text exposition and the pods carry the scrape annotations. The manifests are
linted by the test suite — probes, resource bounds, security context and image
consistency are all asserted.

`jobs/databricks_ingest.py` is the batch task: it reads a batch, runs the agent,
writes `document_results`, `field_confidence` and `drift_alerts` to the
lakehouse, then scores the batch against the baseline. Spark is optional — with
`pyspark` on the path it writes Delta, without it Parquet, so the task is
testable off-cluster. `jobs/databricks_job.json` is the Jobs API 2.1 definition
with the schedule, cluster spec and secret-scope references.

```bash
docker build -f deploy/Dockerfile -t document-automation-agent:dev .
kubectl apply -k deploy/k8s                      # after filling in secret.yaml
python jobs/databricks_ingest.py --batch-id batch-05 --fail-on-critical-drift
```

## The API

| endpoint | purpose |
|---|---|
| `GET /healthz`, `GET /readyz` | liveness and readiness for the probes |
| `POST /v1/batches/{batch_id}/process` | run a batch, then score it for drift |
| `POST /v1/documents/{id}/process` | run one document |
| `GET /v1/documents/{id}` | fields, rule outcomes and routing for one document |
| `GET /v1/review` | the open queue, weakest critical field first |
| `POST /v1/review/{id}/resolve` | a reviewer's verdict; posts on approval |
| `GET /v1/metrics` | throughput, queue depth, review effort, alerts |
| `GET /metrics` | Prometheus text exposition |

## Layout

```
docagent/
  pipeline.py           extract → validate → route → post or queue
  routing.py            the auto-post / review / reject decision
  drift.py              PSI, null-rate and auto-post-rate monitoring
  store.py              SQLite: extractions, decisions, queue, ledger, alerts
  api.py                FastAPI service with probes and Prometheus metrics
  cli.py                run, doc, queue, resolve, baseline, drift, metrics
  config.py             environment-driven thresholds
  extraction/
    offline.py          deterministic text-layer extractor with computed confidence
    vlm_azure.py        Azure OpenAI vision backend
    base.py             FieldValue / ExtractionResult
  rules/
    engine.py           rule registry, severities, pass/fail/not_evaluated
    invoice_rules.py    the twelve accounts-payable rules
console/app.py          human review console (page, fields, rules, corrections)
jobs/                   Databricks batch ingest task + job definition
deploy/                 Dockerfile, entrypoint, Kubernetes manifests
data/generate_corpus.py seeded invoice generator (pages, text layer, truth)
evals/run_eval.py       escaped errors, routing recall, handling time, drift
tests/                  61 tests across extraction, rules, routing, pipeline,
                        drift, API, console and the deployment manifests
```

## The corpus

`data/generate_corpus.py` renders 200 invoices as page images with a matching
text layer and ground truth, seeded so every run is identical. Documents are
assigned a degradation mode — clean, faint scan, dropped field, vendor
arithmetic error, PO mismatch, over-authorised, duplicate, illegible total — and
the final batch is deliberately degraded so the drift monitor has something real
to catch. Master data (vendors, purchase orders) is generated alongside, which is
what gives the rule engine something to validate against.

## Limitations

- The offline extractor parses a text layer; it is a stand-in for the VLM, not an
  OCR engine. On the Azure backend the page image goes to the model.
- The ledger is SQLite and the landing zone is a directory. A real deployment
  swaps both for the ERP and object storage; the pipeline boundary is `store.py`.
- The handling-time model is two constants. It is explicit and configurable
  rather than measured from a real AP team.
- Line-item extraction is parsed but only used by the subtotal rule; per-line GL
  coding is not modelled.

MIT licensed.

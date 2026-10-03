"""FastAPI service: the deployable surface of the pipeline.

Kubernetes probes hit /healthz and /readyz, the review console and the CronJob
call the v1 endpoints, and /metrics is Prometheus text exposition.
"""

from __future__ import annotations

from functools import lru_cache

from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from docagent import drift
from docagent.config import load_config
from docagent.pipeline import Pipeline, summarise

app = FastAPI(
    title="Document Automation Agent",
    version="1.0.0",
    summary="VLM field extraction, business-rule validation and human-in-the-loop routing for supplier invoices.",
)


@lru_cache(maxsize=1)
def get_pipeline() -> Pipeline:
    return Pipeline(load_config())


class ReviewResolution(BaseModel):
    resolution: str = Field(..., pattern="^(approved|corrected|rejected)$")
    reviewer: str = Field(..., min_length=1, max_length=120)
    corrections: dict = Field(default_factory=dict)
    seconds_spent: float | None = None


@app.get("/healthz")
def healthz() -> dict:
    config = load_config()
    return {"status": "ok", "backend": config.backend}


@app.get("/readyz")
def readyz() -> dict:
    config = load_config()
    manifest_path = config.corpus_directory / "manifest.jsonl"
    if not manifest_path.exists():
        raise HTTPException(status_code=503, detail=f"corpus not found at {config.corpus_directory}")
    get_pipeline()
    return {"status": "ready", "backend": config.backend, "corpus": str(config.corpus_directory)}


@app.post("/v1/documents/{document_id}/process")
def process_document(document_id: str) -> dict:
    pipeline = get_pipeline()
    entry = next(
        (item for item in pipeline.manifest.entries if item["document_id"] == document_id), None
    )
    if entry is None:
        raise HTTPException(status_code=404, detail=f"unknown document {document_id}")
    return pipeline.process(entry).as_dict()


@app.post("/v1/batches/{batch_id}/process")
def process_batch(batch_id: str) -> dict:
    pipeline = get_pipeline()
    if batch_id not in pipeline.manifest.batch_ids():
        raise HTTPException(status_code=404, detail=f"unknown batch {batch_id}")
    records = pipeline.process_batch(batch_id)
    alerts = drift.monitor_batch(pipeline.store, batch_id, pipeline.config)
    return {
        "batch_id": batch_id,
        "summary": summarise(records, pipeline.config),
        "alerts": [alert.as_dict() for alert in alerts],
    }


@app.get("/v1/documents/{document_id}")
def get_document(document_id: str) -> dict:
    store = get_pipeline().store
    decision = store.decision_row(document_id)
    if decision is None:
        raise HTTPException(status_code=404, detail=f"{document_id} has not been processed")
    return {
        "document": store.document_row(document_id),
        "extraction": store.extraction_payload(document_id),
        "validation": store.validation_payload(document_id),
        "decision": decision,
    }


@app.get("/v1/review")
def list_reviews(limit: int = 50) -> dict:
    reviews = get_pipeline().store.open_reviews(limit=limit)
    return {"open": len(reviews), "items": reviews}


@app.post("/v1/review/{document_id}/resolve")
def resolve_review(document_id: str, body: ReviewResolution) -> dict:
    pipeline = get_pipeline()
    try:
        return pipeline.apply_review(
            document_id=document_id,
            resolution=body.resolution,
            reviewer=body.reviewer,
            corrections=body.corrections,
            seconds_spent=body.seconds_spent,
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@app.get("/v1/metrics")
def metrics() -> dict:
    pipeline = get_pipeline()
    store = pipeline.store
    return {
        "backend": pipeline.config.backend,
        "overall": store.batch_summary(),
        "batches": [store.batch_summary(batch_id) for batch_id in store.batch_ids()],
        "open_reviews": len(store.open_reviews(limit=10000)),
        "review_effort": store.review_effort(),
        "alerts": store.alerts(limit=25),
    }


@app.get("/metrics", response_class=PlainTextResponse)
def prometheus_metrics() -> str:
    store = get_pipeline().store
    overall = store.batch_summary()
    open_reviews = len(store.open_reviews(limit=10000))
    actionable_alerts = sum(1 for alert in store.alerts(limit=10000) if alert["severity"] != "info")
    lines = [
        "# HELP docagent_documents_total Documents processed.",
        "# TYPE docagent_documents_total counter",
        f"docagent_documents_total {overall['documents']}",
        "# HELP docagent_auto_post_rate Share of documents posted without a human.",
        "# TYPE docagent_auto_post_rate gauge",
        f"docagent_auto_post_rate {overall['auto_post_rate']}",
        "# HELP docagent_review_queue_open Documents waiting on a reviewer.",
        "# TYPE docagent_review_queue_open gauge",
        f"docagent_review_queue_open {open_reviews}",
        "# HELP docagent_drift_alerts_actionable Drift alerts above info severity.",
        "# TYPE docagent_drift_alerts_actionable gauge",
        f"docagent_drift_alerts_actionable {actionable_alerts}",
    ]
    return "\n".join(lines) + "\n"

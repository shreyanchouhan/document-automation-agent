"""Databricks batch ingest task.

Runs as a job task on a Databricks cluster: read the landing-zone manifest for a
batch, run the agent over every document, write the per-document results and the
per-field confidence log to the lakehouse, then score the batch against the drift
baseline and fail the task on a critical alert so the job alerting picks it up.

Spark is optional. With pyspark on the path it writes Delta tables; without it
(local runs, CI) it writes the same frames as Parquet, so the task is testable
off-cluster.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from docagent import drift  # noqa: E402
from docagent.config import load_config  # noqa: E402
from docagent.pipeline import Pipeline, summarise  # noqa: E402

RESULTS_TABLE = "document_results"
CONFIDENCE_TABLE = "field_confidence"
ALERTS_TABLE = "drift_alerts"


def _spark_session():
    try:
        from pyspark.sql import SparkSession
    except ImportError:
        return None
    return SparkSession.builder.appName("docagent-ingest").getOrCreate()


def _write(frames: dict[str, list[dict]], output_path: str, fmt: str) -> list[str]:
    written = []
    spark = _spark_session()
    if spark is not None and fmt == "delta":
        for name, rows in frames.items():
            if not rows:
                continue
            target = f"{output_path.rstrip('/')}/{name}"
            spark.createDataFrame(rows).write.format("delta").mode("append").save(target)
            written.append(target)
        return written

    import pandas as pd

    directory = Path(output_path)
    directory.mkdir(parents=True, exist_ok=True)
    for name, rows in frames.items():
        if not rows:
            continue
        target = directory / f"{name}.parquet"
        frame = pd.DataFrame(rows)
        if target.exists():
            frame = pd.concat([pd.read_parquet(target), frame], ignore_index=True)
        frame.to_parquet(target, index=False)
        written.append(str(target))
    return written


def run(batch_id: str, output_path: str, fmt: str, fail_on_critical: bool) -> dict:
    config = load_config()
    pipeline = Pipeline(config)
    if batch_id not in pipeline.manifest.batch_ids():
        raise SystemExit(f"unknown batch {batch_id}; have {pipeline.manifest.batch_ids()}")

    records = pipeline.process_batch(batch_id)
    alerts = drift.monitor_batch(pipeline.store, batch_id, config)

    result_rows = [
        {
            "document_id": record.document_id,
            "batch_id": record.batch_id,
            "action": record.action,
            "minimum_confidence": record.decision.minimum_confidence,
            "minimum_critical_confidence": record.decision.minimum_critical_confidence,
            "weakest_field": record.decision.weakest_field,
            "failed_rules": ",".join(record.report.as_dict()["failed"]),
            "elapsed_ms": record.elapsed_ms,
            "model": record.extraction.model,
            "cost_usd": record.extraction.cost_usd,
        }
        for record in records
    ]
    confidence_rows = [
        {
            "document_id": record.document_id,
            "batch_id": record.batch_id,
            "field": name,
            "confidence": value.confidence,
            "has_value": int(value.value is not None),
        }
        for record in records
        for name, value in record.extraction.fields.items()
    ]
    alert_rows = [{"batch_id": batch_id, **alert.as_dict()} for alert in alerts]

    written = _write(
        {RESULTS_TABLE: result_rows, CONFIDENCE_TABLE: confidence_rows, ALERTS_TABLE: alert_rows},
        output_path,
        fmt,
    )

    summary = summarise(records, config)
    critical = [alert for alert in alerts if alert.severity == "critical"]
    payload = {
        "batch_id": batch_id,
        "summary": summary,
        "alerts": alert_rows,
        "written": written,
    }
    print(json.dumps(payload, indent=2, default=str))

    if critical and fail_on_critical:
        raise SystemExit(
            f"{len(critical)} critical drift alert(s) on {batch_id}: "
            + "; ".join(alert.detail for alert in critical)
        )
    return payload


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Databricks batch ingest for the document agent.")
    parser.add_argument("--batch-id", required=True)
    parser.add_argument("--output-path", default="./data/lakehouse")
    parser.add_argument("--format", choices=["delta", "parquet"], default="parquet")
    parser.add_argument("--fail-on-critical-drift", action="store_true")
    args = parser.parse_args()
    run(args.batch_id, args.output_path, args.format, args.fail_on_critical_drift)

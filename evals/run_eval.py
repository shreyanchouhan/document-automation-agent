"""Eval harness.

The question that matters for a posting pipeline is not "how accurate is the
model" but "what got posted without a human that should not have". So the
headline metric is escaped errors: documents the agent auto-posted whose fields
disagree with ground truth. Routing recall, review precision, handling time and
drift detection are measured alongside it.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from docagent import drift  # noqa: E402
from docagent.config import ALL_FIELDS, CRITICAL_FIELDS, load_config  # noqa: E402
from docagent.pipeline import Pipeline, summarise  # noqa: E402
from docagent.routing import AUTO_POST  # noqa: E402

RESULTS_PATH = Path(__file__).resolve().parent / "results.json"
MONEY_TOLERANCE = 0.01
MONEY_FIELDS = {"subtotal", "tax_amount", "total_amount"}
# Modes that put a genuine defect on the page, so a human must see the document
# whatever the extractor's confidence says.
DEFECT_MODES = {"arithmetic_error", "po_mismatch", "duplicate", "over_authorised", "missing_field"}

THRESHOLDS = {
    "escaped_critical_errors": 0,
    "auto_post_field_accuracy": 0.995,
    "review_recall": 0.95,
    "field_accuracy": 0.90,
    "drift_detected_on_drifted_batch": 1,
    "false_drift_alerts_on_healthy_batches": 0,
}


def field_matches(name: str, extracted, truth) -> bool:
    if extracted is None:
        return truth is None
    if name in MONEY_FIELDS:
        try:
            return abs(float(extracted) - float(truth)) <= MONEY_TOLERANCE
        except (TypeError, ValueError):
            return False
    return str(extracted) == str(truth)


def run(reset: bool = True) -> dict:
    config = load_config()
    if reset:
        Path(config.database_path).unlink(missing_ok=True)
        Path(config.baseline_path).unlink(missing_ok=True)

    pipeline = Pipeline(config)
    manifest = {entry["document_id"]: entry for entry in pipeline.manifest.entries}
    batch_ids = pipeline.manifest.batch_ids()
    healthy_batches, drifted_batch = batch_ids[:-1], batch_ids[-1]

    records = []
    alerts_by_batch: dict[str, list] = {}
    for index, batch_id in enumerate(batch_ids):
        batch_records = pipeline.process_batch(batch_id)
        records.extend(batch_records)
        # The first two batches are the accepted reference; everything after is scored.
        if index == 1:
            drift.save_baseline(
                drift.snapshot(pipeline.store, batch_ids[:2], config), config.baseline_path
            )
        if index >= 2:
            alerts_by_batch[batch_id] = drift.monitor_batch(pipeline.store, batch_id, config)

    # -- field accuracy ---------------------------------------------------
    rows = []
    compared = correct = 0
    auto_compared = auto_correct = 0
    escaped_documents = []
    escaped_critical = 0

    for record in records:
        entry = manifest[record.document_id]
        truth = json.loads(
            (config.corpus_directory / entry["truth_path"]).read_text()
        )
        wrong_fields = []
        for name in ALL_FIELDS:
            extracted = record.extraction.value(name)
            expected = truth.get(name)
            # A field the generator deliberately removed from the page has no
            # ground truth to hit, so it is not scored as an extraction error.
            if entry.get("dropped_field") and name in _dropped_field_names(entry["dropped_field"]):
                continue
            compared += 1
            ok = field_matches(name, extracted, expected)
            correct += int(ok)
            if record.action == AUTO_POST:
                auto_compared += 1
                auto_correct += int(ok)
            if not ok:
                wrong_fields.append(name)

        needed_human = bool(wrong_fields) or entry["degradation_mode"] in DEFECT_MODES
        if record.action == AUTO_POST and wrong_fields:
            escaped_documents.append({"document_id": record.document_id, "fields": wrong_fields})
            escaped_critical += sum(1 for name in wrong_fields if name in CRITICAL_FIELDS)

        rows.append(
            {
                "document_id": record.document_id,
                "batch_id": record.batch_id,
                "mode": entry["degradation_mode"],
                "action": record.action,
                "wrong_fields": wrong_fields,
                "needed_human": needed_human,
                "minimum_confidence": round(record.decision.minimum_confidence, 4),
            }
        )

    needed = [row for row in rows if row["needed_human"]]
    caught = [row for row in needed if row["action"] != AUTO_POST]
    reviewed = [row for row in rows if row["action"] != AUTO_POST]
    genuinely_reviewed = [row for row in reviewed if row["needed_human"]]

    # -- drift ------------------------------------------------------------
    actionable = {
        batch_id: [alert for alert in alerts if alert.severity != "info"]
        for batch_id, alerts in alerts_by_batch.items()
    }
    false_alerts = sum(
        len(alerts) for batch_id, alerts in actionable.items() if batch_id in healthy_batches
    )

    throughput = summarise(records, config)
    steady = summarise([r for r in records if r.batch_id != drifted_batch], config)

    summary = {
        "backend": config.backend,
        "documents": len(records),
        "field_accuracy": round(correct / compared, 4) if compared else 0.0,
        "auto_post_field_accuracy": round(auto_correct / auto_compared, 4) if auto_compared else 1.0,
        "escaped_documents": len(escaped_documents),
        "escaped_critical_errors": escaped_critical,
        "review_recall": round(len(caught) / len(needed), 4) if needed else 1.0,
        "review_precision": round(len(genuinely_reviewed) / len(reviewed), 4) if reviewed else 1.0,
        "auto_post_rate": throughput["auto_post_rate"],
        "auto_post_rate_steady_state": steady["auto_post_rate"],
        "handling_time_speedup": throughput["handling_time_speedup"],
        "handling_time_speedup_steady_state": steady["handling_time_speedup"],
        "machine_ms_p50": throughput["machine_ms_p50"],
        "drift_detected_on_drifted_batch": int(bool(actionable.get(drifted_batch))),
        "drift_alerts_on_drifted_batch": len(actionable.get(drifted_batch, [])),
        "false_drift_alerts_on_healthy_batches": false_alerts,
    }
    def misses_threshold(metric: str, threshold: float) -> bool:
        # Error counts must stay at or under their limit; every other metric is a floor.
        if metric.startswith(("escaped", "false")):
            return summary[metric] > threshold
        return summary[metric] < threshold

    summary["failed_thresholds"] = {
        metric: {"value": summary[metric], "threshold": threshold}
        for metric, threshold in THRESHOLDS.items()
        if misses_threshold(metric, threshold)
    }

    RESULTS_PATH.write_text(
        json.dumps(
            {"summary": summary, "escaped": escaped_documents, "rows": rows}, indent=2, default=str
        )
    )

    print("=" * 78)
    for key, value in summary.items():
        if key != "failed_thresholds":
            print(f"{key:>38}: {value}")
    if escaped_documents:
        print("\nESCAPED (auto-posted with a wrong field):")
        for item in escaped_documents[:10]:
            print(f"  {item['document_id']}: {', '.join(item['fields'])}")
    if summary["failed_thresholds"]:
        print("\nTHRESHOLDS NOT MET:")
        for metric, detail in summary["failed_thresholds"].items():
            print(f"  {metric}: {detail['value']} vs {detail['threshold']}")
    print(f"\nwrote {RESULTS_PATH}")
    return summary


def _dropped_field_names(label: str) -> set[str]:
    return {
        "PO Number": {"po_number"},
        "Due Date": {"due_date"},
        "Vendor Tax ID": {"vendor_tax_id"},
    }.get(label, set())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the document pipeline eval suite.")
    parser.add_argument("--strict", action="store_true", help="exit non-zero when a threshold is missed")
    parser.add_argument("--no-reset", action="store_true", help="keep the existing database")
    args = parser.parse_args()
    result = run(reset=not args.no_reset)
    raise SystemExit(1 if (args.strict and result["failed_thresholds"]) else 0)

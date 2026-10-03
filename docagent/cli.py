"""Command line entry point: ``python -m docagent.cli <command>``."""

from __future__ import annotations

import argparse
import json
import sys

from docagent import drift
from docagent.config import load_config
from docagent.pipeline import Pipeline, summarise

SEPARATOR = "-" * 78


def _pipeline() -> Pipeline:
    config = load_config()
    if not (config.corpus_directory / "manifest.jsonl").exists():
        print(
            f"no corpus at {config.corpus_directory}\nrun: python data/generate_corpus.py",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return Pipeline(config)


def command_run(args) -> int:
    pipeline = _pipeline()
    batches = [args.batch] if args.batch else pipeline.manifest.batch_ids()
    all_records = []
    for batch_id in batches:
        records = pipeline.process_batch(batch_id, progress=args.verbose)
        all_records.extend(records)
        summary = summarise(records, pipeline.config)
        print(
            f"{batch_id}: {summary['documents']:>3} docs  "
            f"auto {summary['auto_post']:>3}  review {summary['needs_review']:>3}  "
            f"rejected {summary['rejected']:>2}  auto-post {summary['auto_post_rate']:.0%}"
        )
        for alert in drift.monitor_batch(pipeline.store, batch_id, pipeline.config):
            if alert.severity != "info":
                print(f"    ! [{alert.severity}] {alert.detail}")

    print(SEPARATOR)
    print(json.dumps(summarise(all_records, pipeline.config), indent=2))
    return 0


def command_document(args) -> int:
    pipeline = _pipeline()
    store = pipeline.store
    decision = store.decision_row(args.document_id)
    if decision is None:
        print(f"{args.document_id} has not been processed yet", file=sys.stderr)
        return 1
    extraction = store.extraction_payload(args.document_id)
    validation = store.validation_payload(args.document_id)

    print(SEPARATOR)
    print(f"{args.document_id}   action: {decision['action']}   batch: {decision['batch_id']}")
    print(SEPARATOR)
    print("FIELDS")
    for name, field in extraction["fields"].items():
        marker = "  " if field["confidence"] >= 0.92 else ("~ " if field["value"] is not None else "! ")
        print(f"  {marker}{name:<16} {str(field['value'])[:32]:<34} {field['confidence']:.2f}  {field['evidence'][:40]}")
    print(SEPARATOR)
    print("RULES")
    for outcome in validation["outcomes"]:
        mark = {"pass": "ok ", "fail": "FAIL", "not_evaluated": "n/a"}[outcome["status"]]
        print(f"  [{mark}] {outcome['rule_id']} {outcome['title']:<38} {outcome['detail'][:70]}")
    print(SEPARATOR)
    print("ROUTING")
    for reason in json.loads(decision["reasons"]):
        print(f"  - {reason}")
    return 0


def command_queue(args) -> int:
    reviews = _pipeline().store.open_reviews(limit=args.limit)
    print(f"{len(reviews)} open review(s), weakest critical field first\n")
    for review in reviews:
        print(f"{review['document_id']}  crit-conf {review['minimum_critical_confidence']:.2f}  {review['batch_id']}")
        for reason in review["reasons"][:3]:
            print(f"    - {reason[:100]}")
    return 0


def command_resolve(args) -> int:
    corrections = {}
    for pair in args.field or []:
        name, _, value = pair.partition("=")
        try:
            corrections[name] = float(value)
        except ValueError:
            corrections[name] = value
    result = _pipeline().apply_review(
        document_id=args.document_id,
        resolution=args.resolution,
        reviewer=args.reviewer,
        corrections=corrections,
        seconds_spent=args.seconds,
    )
    print(json.dumps(result, indent=2))
    return 0


def command_baseline(args) -> int:
    pipeline = _pipeline()
    batches = args.batches.split(",") if args.batches else pipeline.store.batch_ids()[:2]
    profile = drift.snapshot(pipeline.store, batches, pipeline.config)
    path = drift.save_baseline(profile, pipeline.config.baseline_path)
    print(f"baseline written to {path}")
    print(f"  batches        {', '.join(batches)}")
    print(f"  documents      {profile['documents']}")
    print(f"  auto-post rate {profile['auto_post_rate']:.1%}")
    return 0


def command_drift(args) -> int:
    pipeline = _pipeline()
    alerts = drift.monitor_batch(pipeline.store, args.batch, pipeline.config)
    if not alerts:
        print(f"{args.batch}: no drift against the baseline")
        return 0
    for alert in alerts:
        print(f"[{alert.severity:<8}] {alert.kind:<22} {alert.detail}")
    return 0


def command_metrics(args) -> int:
    store = _pipeline().store
    print(json.dumps(
        {
            "overall": store.batch_summary(),
            "batches": [store.batch_summary(batch_id) for batch_id in store.batch_ids()],
            "open_reviews": len(store.open_reviews(limit=10000)),
            "review_effort": store.review_effort(),
        },
        indent=2,
    ))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="docagent", description="Document automation agent.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run", help="process a batch, or every batch")
    run.add_argument("--batch")
    run.add_argument("--verbose", action="store_true")
    run.set_defaults(handler=command_run)

    document = subparsers.add_parser("doc", help="show one document's extraction, rules and routing")
    document.add_argument("document_id")
    document.set_defaults(handler=command_document)

    queue = subparsers.add_parser("queue", help="list the open human review queue")
    queue.add_argument("--limit", type=int, default=20)
    queue.set_defaults(handler=command_queue)

    resolve = subparsers.add_parser("resolve", help="resolve one review")
    resolve.add_argument("document_id")
    resolve.add_argument("--resolution", choices=["approved", "corrected", "rejected"], required=True)
    resolve.add_argument("--reviewer", default="cli")
    resolve.add_argument("--field", action="append", help="name=value, repeatable")
    resolve.add_argument("--seconds", type=float)
    resolve.set_defaults(handler=command_resolve)

    baseline = subparsers.add_parser("baseline", help="save the drift baseline from known-good batches")
    baseline.add_argument("--batches", help="comma separated, e.g. batch-01,batch-02")
    baseline.set_defaults(handler=command_baseline)

    drift_parser = subparsers.add_parser("drift", help="score a batch against the baseline")
    drift_parser.add_argument("--batch", required=True)
    drift_parser.set_defaults(handler=command_drift)

    metrics = subparsers.add_parser("metrics", help="throughput and queue metrics")
    metrics.set_defaults(handler=command_metrics)

    args = parser.parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())

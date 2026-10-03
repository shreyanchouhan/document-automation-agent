"""The agent loop: extract → validate → route → post or queue, one document at a time."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from docagent.config import Config, load_config
from docagent.extraction import extract_document
from docagent.extraction.base import ExtractionResult, FieldValue
from docagent.routing import AUTO_POST, NEEDS_REVIEW, REJECTED, RoutingDecision, decide
from docagent.rules.engine import RuleContext, ValidationReport, run_rules
from docagent.rules.invoice_rules import RULES, today_iso
from docagent.store import Store


@dataclass
class DocumentRecord:
    document_id: str
    batch_id: str
    action: str
    decision: RoutingDecision
    extraction: ExtractionResult
    report: ValidationReport
    elapsed_ms: float = 0.0

    def as_dict(self) -> dict:
        return {
            "document_id": self.document_id,
            "batch_id": self.batch_id,
            "action": self.action,
            "decision": self.decision.as_dict(),
            "extraction": self.extraction.as_dict(),
            "validation": self.report.as_dict(),
            "elapsed_ms": round(self.elapsed_ms, 2),
        }


@dataclass
class Manifest:
    entries: list[dict] = field(default_factory=list)

    @classmethod
    def load(cls, corpus_directory: Path) -> "Manifest":
        path = corpus_directory / "manifest.jsonl"
        if not path.exists():
            raise FileNotFoundError(f"no manifest at {path}; run python data/generate_corpus.py")
        return cls([json.loads(line) for line in path.read_text().splitlines() if line.strip()])

    def batch(self, batch_id: str) -> list[dict]:
        return [entry for entry in self.entries if entry["batch_id"] == batch_id]

    def batch_ids(self) -> list[str]:
        return sorted({entry["batch_id"] for entry in self.entries})


def load_masters(corpus_directory: Path) -> tuple[dict, dict]:
    masters = json.loads((corpus_directory / "masters.json").read_text())
    vendors = {vendor["vendor_tax_id"]: vendor for vendor in masters["vendors"]}
    purchase_orders = {po["po_number"]: po for po in masters["purchase_orders"]}
    return vendors, purchase_orders


class Pipeline:
    def __init__(self, config: Config | None = None, store: Store | None = None):
        self.config = config or load_config()
        self.store = store or Store(self.config.database_path)
        self.vendors, self.purchase_orders = load_masters(self.config.corpus_directory)
        self.manifest = Manifest.load(self.config.corpus_directory)

    # -- single document --------------------------------------------------
    def process(self, entry: dict) -> DocumentRecord:
        started = time.perf_counter()
        document_id = entry["document_id"]
        batch_id = entry["batch_id"]
        image_path = self.config.corpus_directory / entry["image_path"]
        text_path = self.config.corpus_directory / entry["text_path"]

        extraction = extract_document(document_id, image_path, text_path, self.config)

        context = RuleContext(
            extraction=extraction,
            vendors=self.vendors,
            purchase_orders=self.purchase_orders,
            posted_invoices=self.store.posted_invoice_keys(exclude_document_id=document_id),
            config=self.config,
            today=today_iso(),
        )
        report = run_rules(RULES, context)
        decision = decide(extraction, report, self.config)

        self.store.record_document(document_id, batch_id, str(image_path), str(text_path))
        self.store.record_extraction(extraction)
        self.store.record_field_confidence(document_id, batch_id, extraction)
        self.store.record_validation(document_id, report)
        self.store.record_decision(document_id, batch_id, decision)

        if decision.action == AUTO_POST:
            self.store.record_posting(document_id, extraction, source="auto")
        elif decision.action == NEEDS_REVIEW:
            self.store.enqueue_review(document_id, batch_id)
        self.store.commit()

        return DocumentRecord(
            document_id=document_id,
            batch_id=batch_id,
            action=decision.action,
            decision=decision,
            extraction=extraction,
            report=report,
            elapsed_ms=(time.perf_counter() - started) * 1000,
        )

    # -- batches ----------------------------------------------------------
    def process_batch(self, batch_id: str, progress: bool = False) -> list[DocumentRecord]:
        records = []
        for entry in self.manifest.batch(batch_id):
            record = self.process(entry)
            records.append(record)
            if progress:
                print(f"  {record.document_id}  {record.action:<13} {record.elapsed_ms:6.1f} ms")
        return records

    def process_all(self, progress: bool = False) -> list[DocumentRecord]:
        records = []
        for batch_id in self.manifest.batch_ids():
            if progress:
                print(f"{batch_id}")
            records.extend(self.process_batch(batch_id, progress=progress))
        return records

    # -- human in the loop -------------------------------------------------
    def apply_review(
        self,
        document_id: str,
        resolution: str,
        reviewer: str,
        corrections: dict | None = None,
        seconds_spent: float | None = None,
    ) -> dict:
        """Record a reviewer's verdict and, when they approve, post the invoice.

        Corrections are written back onto the stored extraction so the field-level
        accuracy numbers reflect what was actually posted, not what was first read.
        """
        payload = self.store.extraction_payload(document_id)
        if payload is None:
            raise KeyError(f"no extraction for {document_id}")

        corrections = corrections or {}
        for name, value in corrections.items():
            if name in payload["fields"]:
                payload["fields"][name]["value"] = value
                payload["fields"][name]["confidence"] = 1.0
                payload["fields"][name]["evidence"] = f"corrected by {reviewer}"

        extraction = ExtractionResult(
            document_id=document_id,
            fields={
                name: FieldValue(
                    name=name,
                    value=entry["value"],
                    raw=entry.get("raw"),
                    confidence=entry["confidence"],
                    evidence=entry["evidence"],
                    repairs=entry.get("repairs", 0),
                )
                for name, entry in payload["fields"].items()
            },
            line_items=payload.get("line_items", []),
            backend=payload["backend"],
            model=payload["model"],
        )

        seconds = seconds_spent if seconds_spent is not None else self.config.review_handling_seconds
        self.store.resolve_review(document_id, resolution, reviewer, corrections, seconds)
        if resolution in ("approved", "corrected"):
            self.store.record_extraction(extraction)
            self.store.record_posting(document_id, extraction, source="human")
        self.store.commit()
        return {
            "document_id": document_id,
            "resolution": resolution,
            "corrections": corrections,
            "seconds_spent": seconds,
        }


def summarise(records: list[DocumentRecord], config: Config) -> dict:
    """Throughput numbers, including the handling time this routing actually saves."""
    total = len(records)
    if not total:
        return {"documents": 0}
    counts = {action: 0 for action in (AUTO_POST, NEEDS_REVIEW, REJECTED)}
    for record in records:
        counts[record.action] += 1
    touched = counts[NEEDS_REVIEW] + counts[REJECTED]

    manual_seconds = total * config.manual_handling_seconds
    automated_seconds = touched * config.review_handling_seconds
    return {
        "documents": total,
        **counts,
        "auto_post_rate": round(counts[AUTO_POST] / total, 4),
        "touched_by_a_human": touched,
        "machine_ms_p50": round(sorted(r.elapsed_ms for r in records)[total // 2], 2),
        "manual_handling_hours": round(manual_seconds / 3600, 2),
        "pipeline_handling_hours": round(automated_seconds / 3600, 2),
        "handling_time_speedup": round(manual_seconds / automated_seconds, 2) if automated_seconds else None,
    }

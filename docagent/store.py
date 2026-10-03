"""SQLite persistence: documents, extractions, validations, routing decisions,
the human review queue, the posting ledger, per-field confidence and alerts."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    document_id TEXT PRIMARY KEY,
    batch_id    TEXT NOT NULL,
    image_path  TEXT NOT NULL,
    text_path   TEXT NOT NULL,
    ingested_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS extractions (
    document_id TEXT PRIMARY KEY REFERENCES documents(document_id),
    backend     TEXT NOT NULL,
    model       TEXT NOT NULL,
    payload     TEXT NOT NULL,
    latency_ms  REAL NOT NULL,
    tokens      INTEGER NOT NULL,
    cost_usd    REAL NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS validations (
    document_id    TEXT PRIMARY KEY REFERENCES documents(document_id),
    worst_severity TEXT,
    payload        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS decisions (
    document_id                TEXT PRIMARY KEY REFERENCES documents(document_id),
    batch_id                   TEXT NOT NULL,
    action                     TEXT NOT NULL,
    reasons                    TEXT NOT NULL,
    minimum_confidence         REAL NOT NULL,
    minimum_critical_confidence REAL NOT NULL,
    weakest_field              TEXT,
    created_at                 TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS review_queue (
    document_id   TEXT PRIMARY KEY REFERENCES documents(document_id),
    batch_id      TEXT NOT NULL,
    status        TEXT NOT NULL,
    queued_at     TEXT NOT NULL,
    resolved_at   TEXT,
    resolution    TEXT,
    reviewer      TEXT,
    corrections   TEXT,
    seconds_spent REAL
);

CREATE TABLE IF NOT EXISTS postings (
    posting_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id    TEXT NOT NULL REFERENCES documents(document_id),
    vendor_tax_id  TEXT,
    invoice_number TEXT,
    total_amount   REAL,
    currency       TEXT,
    source         TEXT NOT NULL,
    posted_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS field_confidence (
    document_id TEXT NOT NULL REFERENCES documents(document_id),
    batch_id    TEXT NOT NULL,
    field       TEXT NOT NULL,
    confidence  REAL NOT NULL,
    has_value   INTEGER NOT NULL,
    PRIMARY KEY (document_id, field)
);

CREATE TABLE IF NOT EXISTS alerts (
    alert_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id   TEXT NOT NULL,
    kind       TEXT NOT NULL,
    severity   TEXT NOT NULL,
    detail     TEXT NOT NULL,
    metric     REAL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_decisions_batch ON decisions(batch_id);
CREATE INDEX IF NOT EXISTS idx_queue_status ON review_queue(status);
CREATE INDEX IF NOT EXISTS idx_confidence_batch ON field_confidence(batch_id, field);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, database_path: Path):
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.database_path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(SCHEMA)
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    # -- writes -----------------------------------------------------------
    def record_document(self, document_id: str, batch_id: str, image_path: str, text_path: str) -> None:
        self.connection.execute(
            "INSERT OR REPLACE INTO documents VALUES (?,?,?,?,?)",
            (document_id, batch_id, image_path, text_path, _now()),
        )

    def record_extraction(self, extraction) -> None:
        self.connection.execute(
            "INSERT OR REPLACE INTO extractions VALUES (?,?,?,?,?,?,?,?)",
            (
                extraction.document_id,
                extraction.backend,
                extraction.model,
                json.dumps(extraction.as_dict()),
                extraction.latency_ms,
                extraction.prompt_tokens + extraction.completion_tokens,
                extraction.cost_usd,
                _now(),
            ),
        )

    def record_field_confidence(self, document_id: str, batch_id: str, extraction) -> None:
        self.connection.executemany(
            "INSERT OR REPLACE INTO field_confidence VALUES (?,?,?,?,?)",
            [
                (document_id, batch_id, name, value.confidence, int(value.value is not None))
                for name, value in extraction.fields.items()
            ],
        )

    def record_validation(self, document_id: str, report) -> None:
        self.connection.execute(
            "INSERT OR REPLACE INTO validations VALUES (?,?,?)",
            (document_id, report.worst_severity, json.dumps(report.as_dict())),
        )

    def record_decision(self, document_id: str, batch_id: str, decision) -> None:
        self.connection.execute(
            "INSERT OR REPLACE INTO decisions VALUES (?,?,?,?,?,?,?,?)",
            (
                document_id,
                batch_id,
                decision.action,
                json.dumps(decision.reasons),
                decision.minimum_confidence,
                decision.minimum_critical_confidence,
                decision.weakest_field,
                _now(),
            ),
        )

    def enqueue_review(self, document_id: str, batch_id: str) -> None:
        self.connection.execute(
            "INSERT OR REPLACE INTO review_queue "
            "(document_id, batch_id, status, queued_at) VALUES (?,?,?,?)",
            (document_id, batch_id, "open", _now()),
        )

    def resolve_review(
        self,
        document_id: str,
        resolution: str,
        reviewer: str,
        corrections: dict | None = None,
        seconds_spent: float | None = None,
    ) -> None:
        self.connection.execute(
            "UPDATE review_queue SET status='resolved', resolved_at=?, resolution=?, "
            "reviewer=?, corrections=?, seconds_spent=? WHERE document_id=?",
            (_now(), resolution, reviewer, json.dumps(corrections or {}), seconds_spent, document_id),
        )

    def record_posting(self, document_id: str, extraction, source: str) -> None:
        self.connection.execute(
            "INSERT INTO postings (document_id, vendor_tax_id, invoice_number, total_amount, "
            "currency, source, posted_at) VALUES (?,?,?,?,?,?,?)",
            (
                document_id,
                extraction.value("vendor_tax_id"),
                extraction.value("invoice_number"),
                extraction.value("total_amount"),
                extraction.value("currency"),
                source,
                _now(),
            ),
        )

    def record_alert(self, batch_id: str, kind: str, severity: str, detail: str, metric: float | None) -> None:
        self.connection.execute(
            "INSERT INTO alerts (batch_id, kind, severity, detail, metric, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (batch_id, kind, severity, detail, metric, _now()),
        )

    def commit(self) -> None:
        self.connection.commit()

    # -- reads ------------------------------------------------------------
    def posted_invoice_keys(self, exclude_document_id: str | None = None) -> set[tuple[str, str]]:
        """Vendor + invoice number of everything already posted.

        A document's own earlier posting is excluded so reprocessing a batch is
        idempotent: a duplicate means a different page carrying the same number.
        """
        query = (
            "SELECT vendor_tax_id, invoice_number FROM postings "
            "WHERE vendor_tax_id IS NOT NULL AND invoice_number IS NOT NULL"
        )
        parameters: tuple = ()
        if exclude_document_id is not None:
            query += " AND document_id != ?"
            parameters = (exclude_document_id,)
        return {
            (row["vendor_tax_id"], row["invoice_number"])
            for row in self.connection.execute(query, parameters)
        }

    def extraction_payload(self, document_id: str) -> dict | None:
        row = self.connection.execute(
            "SELECT payload FROM extractions WHERE document_id = ?", (document_id,)
        ).fetchone()
        return json.loads(row["payload"]) if row else None

    def validation_payload(self, document_id: str) -> dict | None:
        row = self.connection.execute(
            "SELECT payload FROM validations WHERE document_id = ?", (document_id,)
        ).fetchone()
        return json.loads(row["payload"]) if row else None

    def decision_row(self, document_id: str) -> dict | None:
        row = self.connection.execute(
            "SELECT * FROM decisions WHERE document_id = ?", (document_id,)
        ).fetchone()
        return dict(row) if row else None

    def document_row(self, document_id: str) -> dict | None:
        row = self.connection.execute(
            "SELECT * FROM documents WHERE document_id = ?", (document_id,)
        ).fetchone()
        return dict(row) if row else None

    def open_reviews(self, limit: int = 100) -> list[dict]:
        rows = self.connection.execute(
            "SELECT q.document_id, q.batch_id, q.queued_at, d.action, d.reasons, "
            "d.minimum_confidence, d.minimum_critical_confidence, d.weakest_field "
            "FROM review_queue q JOIN decisions d ON d.document_id = q.document_id "
            "WHERE q.status = 'open' ORDER BY d.minimum_critical_confidence ASC LIMIT ?",
            (limit,),
        )
        reviews = []
        for row in rows:
            review = dict(row)
            review["reasons"] = json.loads(review["reasons"])
            reviews.append(review)
        return reviews

    def batch_summary(self, batch_id: str | None = None) -> dict:
        where = "WHERE batch_id = ?" if batch_id else ""
        parameters = (batch_id,) if batch_id else ()
        rows = self.connection.execute(
            f"SELECT action, COUNT(*) AS n FROM decisions {where} GROUP BY action", parameters
        ).fetchall()
        counts = {row["action"]: row["n"] for row in rows}
        total = sum(counts.values())
        return {
            "batch_id": batch_id or "all",
            "documents": total,
            "auto_post": counts.get("auto_post", 0),
            "needs_review": counts.get("needs_review", 0),
            "rejected": counts.get("rejected", 0),
            "auto_post_rate": round(counts.get("auto_post", 0) / total, 4) if total else 0.0,
        }

    def batch_ids(self) -> list[str]:
        return [
            row["batch_id"]
            for row in self.connection.execute(
                "SELECT DISTINCT batch_id FROM decisions ORDER BY batch_id"
            )
        ]

    def field_confidences(self, batch_id: str | None = None) -> dict[str, list[float]]:
        where = "WHERE batch_id = ?" if batch_id else ""
        parameters = (batch_id,) if batch_id else ()
        confidences: dict[str, list[float]] = {}
        for row in self.connection.execute(
            f"SELECT field, confidence FROM field_confidence {where}", parameters
        ):
            confidences.setdefault(row["field"], []).append(row["confidence"])
        return confidences

    def alerts(self, limit: int = 50) -> list[dict]:
        return [
            dict(row)
            for row in self.connection.execute(
                "SELECT * FROM alerts ORDER BY alert_id DESC LIMIT ?", (limit,)
            )
        ]

    def review_effort(self) -> dict:
        row = self.connection.execute(
            "SELECT COUNT(*) AS resolved, COALESCE(SUM(seconds_spent), 0) AS seconds "
            "FROM review_queue WHERE status = 'resolved'"
        ).fetchone()
        return {"resolved": row["resolved"], "seconds": row["seconds"]}

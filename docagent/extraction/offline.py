"""Deterministic extractor used when no vision model is configured.

It reads the page's text layer - what an OCR pass would hand a VLM - and parses
fields with format-aware rules. Confidence is computed from the evidence it can
see: whether the value matches the field's strict format, how many glyph
repairs it had to make ('O' read as '0' in a digit slot), and how much of the
value is illegible. It never consults the ground truth, so the accuracy the
evals report is real.
"""

from __future__ import annotations

import re
import time

from docagent.extraction.base import ExtractionResult, FieldValue

LABELS = {
    "Invoice Number": "invoice_number",
    "Invoice Date": "invoice_date",
    "Due Date": "due_date",
    "Vendor": "vendor_name",
    "Vendor Tax ID": "vendor_tax_id",
    "PO Number": "po_number",
    "Currency": "currency",
    "Subtotal": "subtotal",
    "Tax": "tax_amount",
    "Total Due": "total_amount",
}
STRICT_FORMATS = {
    "invoice_number": re.compile(r"INV-\d{4}-\d{4}"),
    "invoice_date": re.compile(r"\d{4}-\d{2}-\d{2}"),
    "due_date": re.compile(r"\d{4}-\d{2}-\d{2}"),
    "vendor_name": re.compile(r"[A-Za-z][A-Za-z .,&'-]{4,60}"),
    "vendor_tax_id": re.compile(r"[A-Z0-9][A-Z0-9-]{7,16}"),
    "po_number": re.compile(r"PO-\d{5}"),
    "currency": re.compile(r"(INR|EUR|USD|SGD|GBP)"),
    "subtotal": re.compile(r"[\d,]+\.\d{2}"),
    "tax_amount": re.compile(r"[\d,]+\.\d{2}"),
    "total_amount": re.compile(r"[\d,]+\.\d{2}"),
}
# Fields whose characters are digits once separators are removed, so a letter
# sitting in a digit slot is a scan artefact with exactly one sensible reading.
NUMERIC_SLOT_FIELDS = {"invoice_number", "invoice_date", "due_date", "po_number",
                       "subtotal", "tax_amount", "total_amount"}
MONEY_FIELDS = {"subtotal", "tax_amount", "total_amount"}
GLYPH_REPAIRS = {"O": "0", "o": "0", "l": "1", "I": "1", "S": "5", "B": "8", "Z": "2", "G": "6"}

STRICT_CONFIDENCE = 0.985
REPAIR_PENALTY = 0.13
LOOSE_CONFIDENCE = 0.55
ILLEGIBLE_PENALTY = 1.6
LINE_ITEM_PATTERN = re.compile(r"^\s{2,}(?P<description>.+?)\s+x(?P<quantity>\d+)\s+@ (?P<unit_price>[\d,]+\.\d{2})\s+= (?P<amount>[\d,]+\.\d{2})\s*$")


def _to_number(raw: str) -> float | None:
    try:
        return float(raw.replace(",", ""))
    except ValueError:
        return None


def _repair(raw: str, name: str) -> tuple[str, int]:
    if name not in NUMERIC_SLOT_FIELDS:
        return raw, 0
    repaired, repairs = [], 0
    for index, character in enumerate(raw):
        # Keep the alphabetic prefix of INV-/PO- intact; only repair the body.
        prefix_length = 4 if name in ("invoice_number", "po_number") else 0
        if index < prefix_length:
            repaired.append(character)
        elif character in GLYPH_REPAIRS:
            repaired.append(GLYPH_REPAIRS[character])
            repairs += 1
        else:
            repaired.append(character)
    return "".join(repaired), repairs


def _assess(name: str, raw: str | None) -> FieldValue:
    if raw is None:
        return FieldValue(name, None, None, 0.0, "no label for this field on the page")

    raw = raw.strip()
    illegible = sum(1 for character in raw if character == "#") / max(1, len(raw))
    repaired, repairs = _repair(raw, name)
    pattern = STRICT_FORMATS[name]
    match = pattern.fullmatch(repaired) or pattern.search(repaired)
    strict = bool(pattern.fullmatch(repaired))

    if illegible > 0:
        confidence = max(0.03, 0.45 - ILLEGIBLE_PENALTY * illegible)
        value = None
        if illegible <= 0.25:
            value = _to_number(repaired) if name in MONEY_FIELDS else repaired
            if value is None:
                confidence = 0.05
        return FieldValue(
            name, value, raw, confidence,
            f"{illegible:.0%} of the characters are illegible", repairs,
        )

    if not match:
        return FieldValue(
            name, None, raw, 0.12,
            f"value '{raw}' does not match the expected format for {name}", repairs,
        )

    text = match.group(0)
    confidence = (STRICT_CONFIDENCE if strict else LOOSE_CONFIDENCE) - REPAIR_PENALTY * repairs
    confidence = max(0.05, min(0.99, confidence))
    value = _to_number(text) if name in MONEY_FIELDS else text
    if value is None:
        return FieldValue(name, None, raw, 0.1, f"could not parse '{text}' as a number", repairs)

    if repairs:
        evidence = f"matched {name} format after repairing {repairs} glyph(s) from '{raw}'"
    elif strict:
        evidence = f"exact {name} format match"
    else:
        evidence = f"{name} format found inside '{raw}'"
    return FieldValue(name, value, raw, confidence, evidence, repairs)


def parse_line_items(text_layer: str) -> list[dict]:
    items = []
    for line in text_layer.splitlines():
        match = LINE_ITEM_PATTERN.match(line)
        if match:
            items.append(
                {
                    "description": match.group("description").strip(),
                    "quantity": int(match.group("quantity")),
                    "unit_price": _to_number(match.group("unit_price")),
                    "amount": _to_number(match.group("amount")),
                }
            )
    return items


def extract(document_id: str, text_layer: str) -> ExtractionResult:
    started = time.perf_counter()
    found: dict[str, str] = {}
    for line in text_layer.splitlines():
        if ":" not in line:
            continue
        label, _, value = line.partition(":")
        name = LABELS.get(label.strip())
        if name and name not in found:
            found[name] = value.strip()

    fields = {name: _assess(name, found.get(name)) for name in LABELS.values()}
    line_items = parse_line_items(text_layer)

    characters = len(text_layer)
    return ExtractionResult(
        document_id=document_id,
        fields=fields,
        line_items=line_items,
        backend="offline",
        model="offline-text-layer",
        latency_ms=(time.perf_counter() - started) * 1000,
        prompt_tokens=max(1, characters // 4),
        completion_tokens=120,
        cost_usd=round(max(1, characters // 4) / 1000 * 0.0025 + 0.12 * 0.01, 6),
        cost_is_estimated=True,
    )

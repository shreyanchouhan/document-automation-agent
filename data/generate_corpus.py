"""Generate the synthetic invoice corpus: page images, their text layer, and ground truth.

Deterministic from a fixed seed. Each document is assigned a degradation mode -
clean scans, faint scans with confusable glyphs, dropped fields, vendor
arithmetic errors, PO mismatches, duplicates, unreadable totals - so the
pipeline downstream has real reasons to drop confidence, fail a rule, or ask a
human. The final batch is deliberately degraded so the drift monitor has
something to catch.
"""

from __future__ import annotations

import argparse
import json
import random
from datetime import date, timedelta
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

SEED = 20260401
DEFAULT_CORPUS = Path(__file__).resolve().parent / "corpus"
BATCH_SIZE = 40
PAGE_SIZE = (900, 1180)

VENDORS = [
    ("Northwind Components Pvt Ltd", "27AABCN1234L1Z5", "IN"),
    ("Helios Packaging LLP", "29AAFCH7788K1Z2", "IN"),
    ("Mercer Logistics GmbH", "DE811907980", "DE"),
    ("Silverline Chemicals Ltd", "24AAACS9012M1Z8", "IN"),
    ("Kestrel Instruments Inc", "US-47-2938471", "US"),
    ("Dunmore Facility Services", "07AADCD4567P1Z9", "IN"),
    ("Orient Steel Traders", "33AAGCO3344R1Z1", "IN"),
    ("Pacific Lab Supplies Pte", "SG200812345K", "SG"),
]
ITEM_CATALOG = [
    ("Stainless fitting 18mm", 420.0), ("Nitrile glove box (100)", 890.0),
    ("Calibration service", 7500.0), ("Corrugated carton 5-ply", 61.0),
    ("Freight - regional", 3200.0), ("Reagent kit A2", 12400.0),
    ("PTFE tubing 10m", 1850.0), ("Facility cleaning - monthly", 28000.0),
    ("Sensor module Rev C", 9600.0), ("Steel plate 6mm", 4300.0),
]
CURRENCY_BY_COUNTRY = {"IN": "INR", "DE": "EUR", "US": "USD", "SG": "SGD"}
TAX_RATE_BY_COUNTRY = {"IN": 0.18, "DE": 0.19, "US": 0.0725, "SG": 0.09}

CONFUSABLE = {"0": "O", "O": "0", "1": "l", "5": "S", "8": "B", "2": "Z", "6": "G"}

MODE_WEIGHTS = {
    "clean": 0.68,
    "faint_scan": 0.11,
    "missing_field": 0.05,
    "arithmetic_error": 0.05,
    "po_mismatch": 0.04,
    "over_authorised": 0.03,
    "duplicate": 0.02,
    "unreadable_amount": 0.02,
}
DRIFTED_MODE_WEIGHTS = {
    "clean": 0.30,
    "faint_scan": 0.42,
    "missing_field": 0.07,
    "arithmetic_error": 0.05,
    "po_mismatch": 0.04,
    "over_authorised": 0.02,
    "duplicate": 0.02,
    "unreadable_amount": 0.08,
}


def _font(size: int):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10
        return ImageFont.load_default()


def _degrade_text(value: str, rng: random.Random, probability: float) -> str:
    return "".join(
        CONFUSABLE[character] if character in CONFUSABLE and rng.random() < probability else character
        for character in value
    )


def _transpose_digits(value: str, rng: random.Random) -> str:
    """Swap one adjacent pair of digits.

    A glyph swap is recoverable - '0' for 'O' in a digit slot has one sensible
    reading. A transposition is not: the result is still a valid number, so the
    extractor cannot know it misread, and only a business rule or a human
    catches it. This is what keeps field accuracy honestly below 100 percent.
    """
    positions = [i for i in range(len(value) - 1) if value[i].isdigit() and value[i + 1].isdigit()]
    if not positions:
        return value
    index = rng.choice(positions)
    characters = list(value)
    characters[index], characters[index + 1] = characters[index + 1], characters[index]
    return "".join(characters)


def _money(value: float) -> str:
    return f"{value:,.2f}"


def build_masters(rng: random.Random, invoice_count: int) -> dict:
    vendors = [
        {"vendor_name": name, "vendor_tax_id": tax_id, "country": country, "status": "active"}
        for name, tax_id, country in VENDORS
    ]
    purchase_orders = []
    for index in range(1, invoice_count + 2):
        vendor = vendors[index % len(vendors)]
        purchase_orders.append(
            {
                "po_number": f"PO-{45000 + index}",
                "vendor_tax_id": vendor["vendor_tax_id"],
                "currency": CURRENCY_BY_COUNTRY[vendor["country"]],
                "authorised_amount": round(rng.uniform(500000, 1500000), 2),
                "status": "open",
            }
        )
    return {"vendors": vendors, "purchase_orders": purchase_orders}


def _pick_mode(rng: random.Random, drifted: bool) -> str:
    weights = DRIFTED_MODE_WEIGHTS if drifted else MODE_WEIGHTS
    return rng.choices(list(weights), weights=list(weights.values()))[0]


def _render_page(lines: list[tuple[int, int, str, int]], blur: float, ink: int) -> Image.Image:
    page = Image.new("L", PAGE_SIZE, color=250)
    draw = ImageDraw.Draw(page)
    for x, y, text, size in lines:
        draw.text((x, y), text, font=_font(size), fill=ink)
    draw.line([(60, 190), (PAGE_SIZE[0] - 60, 190)], fill=190, width=2)
    draw.line([(60, PAGE_SIZE[1] - 150), (PAGE_SIZE[0] - 60, PAGE_SIZE[1] - 150)], fill=190, width=2)
    if blur:
        page = page.filter(ImageFilter.GaussianBlur(blur))
    return page


def generate(corpus_directory: Path, invoice_count: int = 200) -> dict:
    rng = random.Random(SEED)
    images_directory = corpus_directory / "images"
    text_directory = corpus_directory / "text"
    truth_directory = corpus_directory / "truth"
    for directory in (images_directory, text_directory, truth_directory):
        directory.mkdir(parents=True, exist_ok=True)

    masters = build_masters(rng, invoice_count)
    (corpus_directory / "masters.json").write_text(json.dumps(masters, indent=2))

    purchase_orders = masters["purchase_orders"]
    batch_count = max(1, invoice_count // BATCH_SIZE)
    manifest: list[dict] = []
    issued: list[dict] = []

    for index in range(1, invoice_count + 1):
        batch_index = min(batch_count - 1, (index - 1) // BATCH_SIZE)
        drifted = batch_index == batch_count - 1 and batch_count > 1
        mode = _pick_mode(rng, drifted)

        vendor = masters["vendors"][rng.randrange(len(masters["vendors"]))]
        currency = CURRENCY_BY_COUNTRY[vendor["country"]]
        tax_rate = TAX_RATE_BY_COUNTRY[vendor["country"]]
        matching_pos = [po for po in purchase_orders if po["vendor_tax_id"] == vendor["vendor_tax_id"]]
        purchase_order = rng.choice(matching_pos)

        invoice_date = date(2026, 1, 1) + timedelta(days=rng.randint(0, 150))
        due_date = invoice_date + timedelta(days=rng.choice([15, 30, 45]))
        line_items = []
        for _ in range(rng.randint(2, 5)):
            description, unit_price = rng.choice(ITEM_CATALOG)
            quantity = rng.randint(1, 12)
            line_items.append(
                {
                    "description": description,
                    "quantity": quantity,
                    "unit_price": unit_price,
                    "amount": round(quantity * unit_price, 2),
                }
            )
        if mode == "over_authorised":
            # A genuine over-delivery: the invoice bills past what the PO authorised.
            overage = purchase_order["authorised_amount"] * rng.uniform(1.15, 1.6)
            line_items.append(
                {
                    "description": "Additional scope - unplanned",
                    "quantity": 1,
                    "unit_price": round(overage, 2),
                    "amount": round(overage, 2),
                }
            )
        subtotal = round(sum(item["amount"] for item in line_items), 2)
        tax_amount = round(subtotal * tax_rate, 2)
        total_amount = round(subtotal + tax_amount, 2)
        invoice_number = f"INV-2026-{3000 + index}"

        if mode == "duplicate" and issued:
            original = rng.choice(issued)
            invoice_number = original["invoice_number"]
            vendor = next(
                v for v in masters["vendors"] if v["vendor_tax_id"] == original["vendor_tax_id"]
            )
            currency = CURRENCY_BY_COUNTRY[vendor["country"]]
            purchase_order = rng.choice(
                [po for po in purchase_orders if po["vendor_tax_id"] == vendor["vendor_tax_id"]]
            )
            truth_currency_fix = True

        tax_amount = round(subtotal * TAX_RATE_BY_COUNTRY[vendor["country"]], 2)
        total_amount = round(subtotal + tax_amount, 2)

        truth = {
            "document_id": f"DOC-{index:04d}",
            "invoice_number": invoice_number,
            "invoice_date": invoice_date.isoformat(),
            "due_date": due_date.isoformat(),
            "vendor_name": vendor["vendor_name"],
            "vendor_tax_id": vendor["vendor_tax_id"],
            "po_number": purchase_order["po_number"],
            "currency": currency,
            "subtotal": subtotal,
            "tax_amount": tax_amount,
            "total_amount": total_amount,
            "line_items": line_items,
        }

        printed = dict(truth)
        if mode == "arithmetic_error":
            # The vendor's own maths is wrong - the extractor should read it faithfully
            # and the rule engine should be the thing that catches it.
            printed["total_amount"] = round(total_amount + rng.choice([-1, 1]) * rng.uniform(50, 900), 2)
        if mode == "po_mismatch":
            printed["po_number"] = f"PO-{rng.randint(90000, 99000)}"

        # --- text layer -------------------------------------------------
        confusion_probability = 0.0
        blur, ink = 0.0, 40
        if mode == "faint_scan":
            confusion_probability = 0.22
            blur, ink = 1.1, 120
        if mode == "unreadable_amount":
            blur, ink = 0.8, 95

        def field_text(value: str) -> str:
            scanned = _degrade_text(str(value), rng, confusion_probability)
            if mode == "faint_scan" and rng.random() < 0.18:
                scanned = _transpose_digits(scanned, rng)
            return scanned

        scanned_subtotal = field_text(_money(printed["subtotal"]))
        scanned_tax = field_text(_money(printed["tax_amount"]))

        rows = [
            ("Invoice Number", field_text(printed["invoice_number"])),
            ("Invoice Date", field_text(printed["invoice_date"])),
            ("Due Date", field_text(printed["due_date"])),
            ("Vendor", printed["vendor_name"]),
            ("Vendor Tax ID", field_text(printed["vendor_tax_id"])),
            ("PO Number", field_text(printed["po_number"])),
            ("Currency", printed["currency"]),
        ]
        dropped_field = None
        if mode == "missing_field":
            dropped_field = rng.choice(["PO Number", "Due Date", "Vendor Tax ID"])
            rows = [row for row in rows if row[0] != dropped_field]

        total_text = field_text(_money(printed["total_amount"]))
        if mode == "unreadable_amount":
            total_text = "".join("#" if character.isdigit() and rng.random() < 0.6 else character
                                 for character in _money(printed["total_amount"]))

        text_lines = ["TAX INVOICE", ""]
        text_lines += [f"{label}: {value}" for label, value in rows]
        text_lines += ["", "Line Items"]
        for item in line_items:
            text_lines.append(
                f"  {item['description']}  x{item['quantity']}  @ {_money(item['unit_price'])}"
                f"  = {_money(item['amount'])}"
            )
        text_lines += [
            "",
            f"Subtotal: {scanned_subtotal}",
            f"Tax: {scanned_tax}",
            f"Total Due: {total_text}",
        ]
        text_layer = "\n".join(text_lines)

        # --- page image -------------------------------------------------
        drawn: list[tuple[int, int, str, int]] = [(60, 70, "TAX INVOICE", 34)]
        y = 220
        for label, value in rows:
            drawn.append((60, y, f"{label}", 18))
            drawn.append((330, y, str(value), 20))
            y += 38
        y += 20
        drawn.append((60, y, "Line Items", 22))
        y += 36
        for item in line_items:
            drawn.append((70, y, item["description"], 18))
            drawn.append((520, y, f"x{item['quantity']}", 18))
            drawn.append((600, y, _money(item["unit_price"]), 18))
            drawn.append((760, y, _money(item["amount"]), 18))
            y += 30
        y += 30
        drawn.append((600, y, "Subtotal", 18))
        drawn.append((760, y, scanned_subtotal, 18))
        drawn.append((600, y + 30, "Tax", 18))
        drawn.append((760, y + 30, scanned_tax, 18))
        drawn.append((600, y + 64, "Total Due", 22))
        drawn.append((760, y + 64, total_text, 22))
        _render_page(drawn, blur, ink).save(images_directory / f"{truth['document_id']}.png")

        (text_directory / f"{truth['document_id']}.txt").write_text(text_layer, encoding="utf-8")
        (truth_directory / f"{truth['document_id']}.json").write_text(json.dumps(truth, indent=2))

        manifest.append(
            {
                "document_id": truth["document_id"],
                "batch_id": f"batch-{batch_index + 1:02d}",
                "degradation_mode": mode,
                "dropped_field": dropped_field,
                "image_path": str((images_directory / f"{truth['document_id']}.png").relative_to(corpus_directory)),
                "text_path": str((text_directory / f"{truth['document_id']}.txt").relative_to(corpus_directory)),
                "truth_path": str((truth_directory / f"{truth['document_id']}.json").relative_to(corpus_directory)),
            }
        )
        issued.append(truth)

    with (corpus_directory / "manifest.jsonl").open("w", encoding="utf-8") as handle:
        for entry in manifest:
            handle.write(json.dumps(entry) + "\n")

    counts: dict[str, int] = {}
    for entry in manifest:
        counts[entry["degradation_mode"]] = counts.get(entry["degradation_mode"], 0) + 1
    print(f"wrote {len(manifest)} documents to {corpus_directory}")
    for mode, count in sorted(counts.items(), key=lambda pair: -pair[1]):
        print(f"  {mode:<20} {count:>4}")
    return {"documents": len(manifest), "modes": counts}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate the synthetic invoice corpus.")
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--count", type=int, default=200)
    args = parser.parse_args()
    generate(args.corpus, args.count)

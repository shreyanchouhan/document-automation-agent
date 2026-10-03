import json

from docagent.extraction.offline import extract, parse_line_items

CLEAN_PAGE = """TAX INVOICE

Invoice Number: INV-2026-3001
Invoice Date: 2026-01-20
Due Date: 2026-02-19
Vendor: Kestrel Instruments Inc
Vendor Tax ID: US-47-2938471
PO Number: PO-45124
Currency: USD

Line Items
  Reagent kit A2  x2  @ 12,400.00  = 24,800.00

Subtotal: 24,800.00
Tax: 1,798.00
Total Due: 26,598.00
"""


def test_clean_page_reads_every_field_with_high_confidence():
    result = extract("DOC-TEST", CLEAN_PAGE)
    assert result.value("invoice_number") == "INV-2026-3001"
    assert result.value("total_amount") == 26598.00
    assert result.value("currency") == "USD"
    assert result.minimum_confidence(tuple(result.fields)) > 0.95


def test_a_missing_label_is_zero_confidence_not_a_guess():
    page = CLEAN_PAGE.replace("PO Number: PO-45124\n", "")
    result = extract("DOC-TEST", page)
    assert result.value("po_number") is None
    assert result.confidence("po_number") == 0.0
    assert "no label" in result.fields["po_number"].evidence


def test_glyph_repair_lowers_confidence_but_recovers_the_value():
    page = CLEAN_PAGE.replace("PO Number: PO-45124", "PO Number: PO-4S1Z4")
    result = extract("DOC-TEST", page)
    assert result.value("po_number") == "PO-45124"
    assert result.fields["po_number"].repairs == 2
    assert 0.6 < result.confidence("po_number") < 0.95


def test_illegible_total_is_dropped_rather_than_half_read():
    page = CLEAN_PAGE.replace("Total Due: 26,598.00", "Total Due: 2#,##8.00")
    result = extract("DOC-TEST", page)
    assert result.value("total_amount") is None
    assert result.confidence("total_amount") < 0.3


def test_line_items_parse_with_quantities_and_amounts():
    items = parse_line_items(CLEAN_PAGE)
    assert items == [
        {"description": "Reagent kit A2", "quantity": 2, "unit_price": 12400.0, "amount": 24800.0}
    ]


def test_extraction_round_trips_through_json():
    payload = json.loads(json.dumps(extract("DOC-TEST", CLEAN_PAGE).as_dict()))
    assert payload["fields"]["invoice_number"]["value"] == "INV-2026-3001"
    assert payload["backend"] == "offline"

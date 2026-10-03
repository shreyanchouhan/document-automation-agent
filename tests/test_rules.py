import pytest

from docagent.extraction.base import ExtractionResult, FieldValue
from docagent.rules.engine import BLOCKER, FAIL, NOT_EVALUATED, PASS, RuleContext, run_rules
from docagent.rules.invoice_rules import RULES

VENDOR = {"vendor_tax_id": "US-47-2938471", "vendor_name": "Kestrel", "status": "active"}
PURCHASE_ORDER = {
    "po_number": "PO-45124",
    "vendor_tax_id": "US-47-2938471",
    "currency": "USD",
    "authorised_amount": 100000.0,
}
BASE = {
    "invoice_number": "INV-2026-3001",
    "invoice_date": "2026-01-20",
    "due_date": "2026-02-19",
    "vendor_name": "Kestrel",
    "vendor_tax_id": "US-47-2938471",
    "po_number": "PO-45124",
    "currency": "USD",
    "subtotal": 10000.0,
    "tax_amount": 1800.0,
    "total_amount": 11800.0,
}


def build(overrides=None, line_items=None):
    values = {**BASE, **(overrides or {})}
    return ExtractionResult(
        document_id="DOC-TEST",
        fields={
            name: FieldValue(name, value, None, 0.99 if value is not None else 0.0, "test")
            for name, value in values.items()
        },
        line_items=line_items if line_items is not None else [{"description": "x", "quantity": 1, "unit_price": 10000.0, "amount": 10000.0}],
    )


def context_for(extraction, posted=frozenset(), config=None):
    class _Config:
        manual_approval_amount = 500000.0

    return RuleContext(
        extraction=extraction,
        vendors={VENDOR["vendor_tax_id"]: VENDOR},
        purchase_orders={PURCHASE_ORDER["po_number"]: PURCHASE_ORDER},
        posted_invoices=set(posted),
        config=config or _Config(),
        today="2026-06-01",
    )


def outcomes(extraction, **kwargs):
    report = run_rules(RULES, context_for(extraction, **kwargs))
    return {outcome.rule_id: outcome for outcome in report.outcomes}


def test_a_clean_invoice_passes_every_rule():
    report = run_rules(RULES, context_for(build()))
    assert report.failures == []
    assert report.worst_severity is None


def test_totals_that_do_not_reconcile_fail():
    result = outcomes(build({"total_amount": 11900.0}))["R02"]
    assert result.status == FAIL
    assert "off by" in result.detail


def test_line_items_that_do_not_sum_fail():
    result = outcomes(build(line_items=[{"description": "x", "quantity": 1, "unit_price": 9000.0, "amount": 9000.0}]))["R03"]
    assert result.status == FAIL


def test_unknown_vendor_is_a_blocker():
    result = outcomes(build({"vendor_tax_id": "XX-00-0000000"}))["R05"]
    assert result.status == FAIL
    assert result.severity == BLOCKER


def test_unknown_po_fails_and_leaves_dependent_rules_unevaluated():
    found = outcomes(build({"po_number": "PO-99999"}))
    assert found["R06"].status == FAIL
    assert found["R07"].status == NOT_EVALUATED
    assert "not in the purchase order master" in found["R07"].detail


def test_amount_over_the_po_authorisation_fails():
    result = outcomes(build({"subtotal": 200000.0, "tax_amount": 36000.0, "total_amount": 236000.0},
                            line_items=[{"description": "x", "quantity": 1, "unit_price": 200000.0, "amount": 200000.0}]))["R08"]
    assert result.status == FAIL


def test_duplicate_posting_is_a_blocker():
    result = outcomes(build(), posted={("US-47-2938471", "INV-2026-3001")})["R10"]
    assert result.status == FAIL
    assert result.severity == BLOCKER


def test_a_future_invoice_date_is_flagged():
    assert outcomes(build({"invoice_date": "2027-01-01"}))["R11"].status == FAIL


@pytest.mark.parametrize("rule_id", [rule.rule_id for rule in RULES])
def test_every_rule_reports_a_detail_string(rule_id):
    found = outcomes(build())
    assert found[rule_id].detail
    assert found[rule_id].status in (PASS, FAIL, NOT_EVALUATED)

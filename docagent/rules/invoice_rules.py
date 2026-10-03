"""The accounts-payable rule set applied to every extracted invoice."""

from __future__ import annotations

from datetime import date

from docagent.config import CRITICAL_FIELDS
from docagent.rules.engine import (
    BLOCKER,
    FAIL,
    NOT_EVALUATED,
    PASS,
    REVIEW,
    WARNING,
    Rule,
    RuleContext,
)

TOTAL_TOLERANCE = 0.02
PO_OVER_DELIVERY_TOLERANCE = 1.05
PLAUSIBLE_TAX_RATES = (0.0, 0.05, 0.0725, 0.09, 0.12, 0.18, 0.19, 0.20)
TAX_RATE_TOLERANCE = 0.005


def _why_no_po(po_number, purchase_order, other_name: str, other_value) -> str:
    """Say which half of a PO-matching rule was unavailable, not just that one was."""
    if po_number is None:
        return "no PO number was read from the page"
    if purchase_order is None:
        return f"{po_number} is not in the purchase order master, so there is nothing to match against"
    return f"{other_name} was not read" if other_value is None else "prerequisites unavailable"


def _missing(context: RuleContext, *names: str) -> list[str]:
    return [name for name in names if context.extraction.value(name) is None]


def critical_fields_present(context: RuleContext) -> tuple[str, str]:
    missing = _missing(context, *CRITICAL_FIELDS)
    if missing:
        return FAIL, f"missing critical field(s): {', '.join(missing)}"
    return PASS, "all critical fields were read"


def totals_reconcile(context: RuleContext) -> tuple[str, str]:
    missing = _missing(context, "subtotal", "tax_amount", "total_amount")
    if missing:
        return NOT_EVALUATED, f"cannot check the totals without {', '.join(missing)}"
    subtotal = context.extraction.value("subtotal")
    tax_amount = context.extraction.value("tax_amount")
    total = context.extraction.value("total_amount")
    difference = round(subtotal + tax_amount - total, 2)
    if abs(difference) > TOTAL_TOLERANCE:
        return FAIL, f"subtotal + tax = {subtotal + tax_amount:,.2f} but the total reads {total:,.2f} (off by {difference:,.2f})"
    return PASS, f"subtotal + tax matches the total ({total:,.2f})"


def line_items_sum_to_subtotal(context: RuleContext) -> tuple[str, str]:
    items = context.extraction.line_items
    subtotal = context.extraction.value("subtotal")
    if not items or subtotal is None:
        return NOT_EVALUATED, "no line items were read, or the subtotal is missing"
    total = round(sum(item.get("amount") or 0 for item in items), 2)
    if abs(total - subtotal) > TOTAL_TOLERANCE:
        return FAIL, f"line items sum to {total:,.2f} against a subtotal of {subtotal:,.2f}"
    return PASS, f"{len(items)} line items sum to the subtotal"


def tax_rate_is_plausible(context: RuleContext) -> tuple[str, str]:
    subtotal = context.extraction.value("subtotal")
    tax_amount = context.extraction.value("tax_amount")
    if not subtotal or tax_amount is None:
        return NOT_EVALUATED, "cannot check the tax rate without a subtotal and tax amount"
    rate = tax_amount / subtotal
    if any(abs(rate - candidate) <= TAX_RATE_TOLERANCE for candidate in PLAUSIBLE_TAX_RATES):
        return PASS, f"effective tax rate {rate:.2%} is a known rate"
    return FAIL, f"effective tax rate {rate:.2%} does not match any configured rate"


def vendor_is_known(context: RuleContext) -> tuple[str, str]:
    tax_id = context.extraction.value("vendor_tax_id")
    if tax_id is None:
        return NOT_EVALUATED, "the vendor tax id was not read"
    vendor = context.vendors.get(tax_id)
    if vendor is None:
        return FAIL, f"vendor tax id {tax_id} is not in the vendor master"
    if vendor.get("status") != "active":
        return FAIL, f"vendor {vendor['vendor_name']} is {vendor.get('status')}"
    return PASS, f"matched vendor {vendor['vendor_name']}"


def purchase_order_exists(context: RuleContext) -> tuple[str, str]:
    po_number = context.extraction.value("po_number")
    if po_number is None:
        return NOT_EVALUATED, "no PO number was read"
    if po_number not in context.purchase_orders:
        return FAIL, f"{po_number} is not an open purchase order"
    return PASS, f"matched purchase order {po_number}"


def purchase_order_vendor_matches(context: RuleContext) -> tuple[str, str]:
    po_number = context.extraction.value("po_number")
    tax_id = context.extraction.value("vendor_tax_id")
    purchase_order = context.purchase_orders.get(po_number) if po_number else None
    if purchase_order is None or tax_id is None:
        return NOT_EVALUATED, _why_no_po(po_number, purchase_order, "the vendor tax id", tax_id)
    if purchase_order["vendor_tax_id"] != tax_id:
        return FAIL, f"{po_number} belongs to {purchase_order['vendor_tax_id']}, not {tax_id}"
    return PASS, f"{po_number} belongs to this vendor"


def amount_within_purchase_order(context: RuleContext) -> tuple[str, str]:
    po_number = context.extraction.value("po_number")
    total = context.extraction.value("total_amount")
    purchase_order = context.purchase_orders.get(po_number) if po_number else None
    if purchase_order is None or total is None:
        return NOT_EVALUATED, _why_no_po(po_number, purchase_order, "the invoice total", total)
    ceiling = purchase_order["authorised_amount"] * PO_OVER_DELIVERY_TOLERANCE
    if total > ceiling:
        return FAIL, f"total {total:,.2f} exceeds {po_number} authorisation {purchase_order['authorised_amount']:,.2f} plus tolerance"
    return PASS, f"total is within the {po_number} authorisation"


def currency_matches_purchase_order(context: RuleContext) -> tuple[str, str]:
    po_number = context.extraction.value("po_number")
    currency = context.extraction.value("currency")
    purchase_order = context.purchase_orders.get(po_number) if po_number else None
    if purchase_order is None or currency is None:
        return NOT_EVALUATED, _why_no_po(po_number, purchase_order, "the currency", currency)
    if purchase_order["currency"] != currency:
        return FAIL, f"invoice is in {currency} but {po_number} is in {purchase_order['currency']}"
    return PASS, f"currency {currency} matches {po_number}"


def not_a_duplicate(context: RuleContext) -> tuple[str, str]:
    tax_id = context.extraction.value("vendor_tax_id")
    invoice_number = context.extraction.value("invoice_number")
    if tax_id is None or invoice_number is None:
        return NOT_EVALUATED, "cannot check for duplicates without the vendor and invoice number"
    if (tax_id, invoice_number) in context.posted_invoices:
        return FAIL, f"{invoice_number} from this vendor has already been posted"
    return PASS, "no earlier posting of this invoice number"


def dates_are_sane(context: RuleContext) -> tuple[str, str]:
    invoice_date = context.extraction.value("invoice_date")
    due_date = context.extraction.value("due_date")
    if invoice_date is None:
        return NOT_EVALUATED, "the invoice date was not read"
    if invoice_date > context.today:
        return FAIL, f"invoice date {invoice_date} is in the future"
    if due_date is not None and due_date < invoice_date:
        return FAIL, f"due date {due_date} precedes the invoice date {invoice_date}"
    return PASS, "invoice and due dates are consistent"


def within_auto_approval_limit(context: RuleContext) -> tuple[str, str]:
    total = context.extraction.value("total_amount")
    if total is None:
        return NOT_EVALUATED, "the invoice total was not read"
    limit = context.config.manual_approval_amount
    if total > limit:
        return FAIL, f"total {total:,.2f} is above the {limit:,.0f} auto-approval limit"
    return PASS, f"total is below the {limit:,.0f} auto-approval limit"


RULES = [
    Rule("R01", "Critical fields present", REVIEW, critical_fields_present),
    Rule("R02", "Totals reconcile", REVIEW, totals_reconcile),
    Rule("R03", "Line items sum to subtotal", REVIEW, line_items_sum_to_subtotal),
    Rule("R04", "Tax rate is plausible", WARNING, tax_rate_is_plausible),
    Rule("R05", "Vendor is in the master", BLOCKER, vendor_is_known),
    Rule("R06", "Purchase order exists", REVIEW, purchase_order_exists),
    Rule("R07", "Purchase order belongs to the vendor", BLOCKER, purchase_order_vendor_matches),
    Rule("R08", "Total within PO authorisation", REVIEW, amount_within_purchase_order),
    Rule("R09", "Currency matches the PO", REVIEW, currency_matches_purchase_order),
    Rule("R10", "Not a duplicate posting", BLOCKER, not_a_duplicate),
    Rule("R11", "Dates are sane", WARNING, dates_are_sane),
    Rule("R12", "Within the auto-approval limit", REVIEW, within_auto_approval_limit),
]


def today_iso() -> str:
    return date.today().isoformat()

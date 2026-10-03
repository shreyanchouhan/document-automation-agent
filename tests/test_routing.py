from docagent.config import load_config
from docagent.extraction.base import ExtractionResult, FieldValue
from docagent.routing import AUTO_POST, NEEDS_REVIEW, REJECTED, decide
from docagent.rules.engine import BLOCKER, FAIL, PASS, REVIEW, RuleOutcome, ValidationReport

FIELDS = (
    "invoice_number", "invoice_date", "due_date", "vendor_name", "vendor_tax_id",
    "po_number", "currency", "subtotal", "tax_amount", "total_amount",
)


def extraction(confidence: float = 0.99, **overrides) -> ExtractionResult:
    confidences = {name: confidence for name in FIELDS}
    confidences.update(overrides)
    return ExtractionResult(
        document_id="DOC-TEST",
        fields={
            name: FieldValue(name, "value", None, value, "test")
            for name, value in confidences.items()
        },
    )


def report(*outcomes) -> ValidationReport:
    return ValidationReport(list(outcomes))


def failure(severity=REVIEW, rule_id="R02"):
    return RuleOutcome(rule_id, "test rule", severity, FAIL, "it failed")


def passing():
    return RuleOutcome("R01", "test rule", REVIEW, PASS, "fine")


def test_clean_and_confident_is_auto_posted():
    decision = decide(extraction(), report(passing()), load_config())
    assert decision.action == AUTO_POST


def test_a_review_rule_failure_routes_to_a_human():
    decision = decide(extraction(), report(failure()), load_config())
    assert decision.action == NEEDS_REVIEW
    assert "it failed" in decision.reasons[0]


def test_a_confident_blocker_is_rejected():
    decision = decide(extraction(), report(failure(BLOCKER, "R10")), load_config())
    assert decision.action == REJECTED


def test_a_blocker_on_an_unconfident_reading_goes_to_a_human_instead():
    decision = decide(extraction(vendor_tax_id=0.41), report(failure(BLOCKER, "R05")), load_config())
    assert decision.action == NEEDS_REVIEW
    assert any("not confident enough to reject" in reason for reason in decision.reasons)


def test_a_weak_critical_field_routes_to_a_human_even_with_every_rule_passing():
    config = load_config()
    decision = decide(extraction(total_amount=0.85), report(passing()), config)
    assert decision.action == NEEDS_REVIEW
    assert decision.weakest_field == "total_amount"
    assert any("critical field" in reason for reason in decision.reasons)


def test_a_weak_non_critical_field_still_routes_to_a_human():
    decision = decide(extraction(vendor_name=0.62), report(passing()), load_config())
    assert decision.action == NEEDS_REVIEW


def test_a_failed_extraction_never_auto_posts():
    result = extraction()
    result.error = "model returned invalid JSON"
    decision = decide(result, report(passing()), load_config())
    assert decision.action == NEEDS_REVIEW

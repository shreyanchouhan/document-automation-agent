"""Routing: the decision to post, to ask a human, or to reject.

Confidence alone is not enough - a confident reading of a duplicate invoice is
still a duplicate - so the rule outcomes and the confidence floors are both
inputs, and every trigger is recorded so the queue explains itself.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from docagent.config import ALL_FIELDS, CRITICAL_FIELDS, Config
from docagent.rules.engine import BLOCKER, FAIL, NOT_EVALUATED, REVIEW, ValidationReport

AUTO_POST = "auto_post"
NEEDS_REVIEW = "needs_review"
REJECTED = "rejected"


@dataclass
class RoutingDecision:
    action: str
    reasons: list[str] = field(default_factory=list)
    minimum_confidence: float = 0.0
    minimum_critical_confidence: float = 0.0
    weakest_field: str | None = None

    def as_dict(self) -> dict:
        data = asdict(self)
        data["minimum_confidence"] = round(self.minimum_confidence, 4)
        data["minimum_critical_confidence"] = round(self.minimum_critical_confidence, 4)
        return data


def decide(extraction, report: ValidationReport, config: Config) -> RoutingDecision:
    confidences = {name: extraction.confidence(name) for name in ALL_FIELDS}
    weakest_field = min(confidences, key=confidences.get)
    minimum_confidence = confidences[weakest_field]
    minimum_critical = extraction.minimum_confidence(CRITICAL_FIELDS)

    reasons: list[str] = []

    if extraction.error:
        return RoutingDecision(
            NEEDS_REVIEW, [f"extraction failed: {extraction.error}"],
            minimum_confidence, minimum_critical, weakest_field,
        )

    confidently_read = (
        minimum_critical >= config.critical_confidence_floor
        and minimum_confidence >= config.field_confidence_floor
    )
    blockers = [outcome for outcome in report.failures_at(BLOCKER) if outcome.status == FAIL]

    # A blocker only rejects when the reading behind it is solid. If the page was
    # faint, the right answer is a human, not a refusal the vendor has to chase.
    if blockers and confidently_read:
        reasons = [f"{outcome.rule_id} {outcome.title}: {outcome.detail}" for outcome in blockers]
        return RoutingDecision(REJECTED, reasons, minimum_confidence, minimum_critical, weakest_field)

    for outcome in blockers:
        reasons.append(f"{outcome.rule_id} {outcome.title}: {outcome.detail} (reading not confident enough to reject)")
    for outcome in report.failures_at(REVIEW):
        reasons.append(f"{outcome.rule_id} {outcome.title}: {outcome.detail}")
    for outcome in report.failures:
        if outcome.status == NOT_EVALUATED and outcome.severity == BLOCKER:
            reasons.append(f"{outcome.rule_id} {outcome.title} could not be evaluated: {outcome.detail}")

    if minimum_critical < config.critical_confidence_floor:
        critical_confidences = {name: confidences[name] for name in CRITICAL_FIELDS}
        weakest_critical = min(critical_confidences, key=critical_confidences.get)
        reasons.append(
            f"critical field {weakest_critical} read at {minimum_critical:.2f}, "
            f"below the {config.critical_confidence_floor:.2f} floor"
        )
    if minimum_confidence < config.field_confidence_floor:
        reasons.append(
            f"field {weakest_field} read at {minimum_confidence:.2f}, "
            f"below the {config.field_confidence_floor:.2f} floor"
        )

    action = NEEDS_REVIEW if reasons else AUTO_POST
    if not reasons:
        reasons.append("all rules passed and every field cleared its confidence floor")
    return RoutingDecision(action, reasons, minimum_confidence, minimum_critical, weakest_field)

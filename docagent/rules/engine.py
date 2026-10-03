"""The business-rule engine that sits between extraction and posting.

A rule is a pure function of (extraction, master data, ledger). It returns pass,
fail or not_evaluated - the third is its own outcome, not a silent pass, because
a rule that could not run is exactly the case a human needs to see.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Callable

BLOCKER = "blocker"
REVIEW = "review"
WARNING = "warning"
SEVERITY_ORDER = {WARNING: 0, REVIEW: 1, BLOCKER: 2}

PASS = "pass"
FAIL = "fail"
NOT_EVALUATED = "not_evaluated"


@dataclass
class RuleContext:
    extraction: object
    vendors: dict[str, dict]
    purchase_orders: dict[str, dict]
    posted_invoices: set[tuple[str, str]]
    config: object
    today: str


@dataclass
class RuleOutcome:
    rule_id: str
    title: str
    severity: str
    status: str
    detail: str

    @property
    def is_clean(self) -> bool:
        return self.status == PASS

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class Rule:
    rule_id: str
    title: str
    severity: str
    check: Callable[[RuleContext], tuple[str, str]]

    def run(self, context: RuleContext) -> RuleOutcome:
        status, detail = self.check(context)
        return RuleOutcome(self.rule_id, self.title, self.severity, status, detail)


@dataclass
class ValidationReport:
    outcomes: list[RuleOutcome] = field(default_factory=list)

    @property
    def failures(self) -> list[RuleOutcome]:
        return [outcome for outcome in self.outcomes if outcome.status != PASS]

    def failures_at(self, severity: str) -> list[RuleOutcome]:
        return [outcome for outcome in self.failures if outcome.severity == severity]

    @property
    def worst_severity(self) -> str | None:
        failures = self.failures
        if not failures:
            return None
        return max((outcome.severity for outcome in failures), key=lambda s: SEVERITY_ORDER[s])

    def as_dict(self) -> dict:
        return {
            "outcomes": [outcome.as_dict() for outcome in self.outcomes],
            "failed": [outcome.rule_id for outcome in self.failures],
            "worst_severity": self.worst_severity,
        }


def run_rules(rules: list[Rule], context: RuleContext) -> ValidationReport:
    return ValidationReport([rule.run(context) for rule in rules])

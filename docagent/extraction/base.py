"""Shared extraction types: a field is a value, a confidence, and its evidence."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class FieldValue:
    name: str
    value: Any
    raw: str | None
    confidence: float
    evidence: str
    repairs: int = 0

    def as_dict(self) -> dict:
        data = asdict(self)
        data["confidence"] = round(self.confidence, 4)
        return data


@dataclass
class ExtractionResult:
    document_id: str
    fields: dict[str, FieldValue]
    line_items: list[dict] = field(default_factory=list)
    backend: str = "offline"
    model: str = "offline-text-layer"
    latency_ms: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    cost_is_estimated: bool = False
    error: str | None = None

    def value(self, name: str):
        entry = self.fields.get(name)
        return entry.value if entry else None

    def confidence(self, name: str) -> float:
        entry = self.fields.get(name)
        return entry.confidence if entry else 0.0

    def minimum_confidence(self, names: tuple[str, ...] | list[str]) -> float:
        scores = [self.confidence(name) for name in names]
        return min(scores) if scores else 0.0

    def as_dict(self) -> dict:
        return {
            "document_id": self.document_id,
            "fields": {name: value.as_dict() for name, value in self.fields.items()},
            "line_items": self.line_items,
            "backend": self.backend,
            "model": self.model,
            "latency_ms": round(self.latency_ms, 2),
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cost_usd": self.cost_usd,
            "cost_is_estimated": self.cost_is_estimated,
            "error": self.error,
        }

"""Azure OpenAI vision backend: the page image goes to the model, strict JSON comes back."""

from __future__ import annotations

import base64
import json
import re
import time
from pathlib import Path

from docagent.config import ALL_FIELDS, Config
from docagent.extraction.base import ExtractionResult, FieldValue

SYSTEM_PROMPT = """You extract fields from a scanned supplier invoice.

Return JSON only, no prose and no code fence, shaped exactly as:
{
  "fields": {
    "<field>": {"value": <string|number|null>, "confidence": <0..1>, "evidence": "<what you read on the page>"}
  },
  "line_items": [{"description": "...", "quantity": 0, "unit_price": 0.0, "amount": 0.0}]
}

Fields: invoice_number, invoice_date, due_date, vendor_name, vendor_tax_id,
po_number, currency, subtotal, tax_amount, total_amount.

Rules:
- Transcribe what the page says. Never correct the vendor's arithmetic, and never
  infer a field that is not printed - return null with confidence 0 instead.
- Dates are ISO (YYYY-MM-DD). Money is a number with no thousands separators.
- Confidence is your reading confidence for that field alone. Lower it when the
  scan is faint, a glyph is ambiguous, or the value is partly illegible.
"""

MONEY_FIELDS = {"subtotal", "tax_amount", "total_amount"}
# Azure list prices (USD per 1K tokens) for the cost column.
PROMPT_PRICE_PER_1K = 0.0025
COMPLETION_PRICE_PER_1K = 0.01


def _strip_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0]
    return text.strip()


def _coerce(name: str, value):
    if value is None:
        return None
    if name in MONEY_FIELDS:
        if isinstance(value, (int, float)):
            return float(value)
        digits = re.sub(r"[^\d.\-]", "", str(value))
        try:
            return float(digits)
        except ValueError:
            return None
    return str(value).strip()


def extract(document_id: str, image_path: Path, config: Config) -> ExtractionResult:
    from openai import AzureOpenAI

    client = AzureOpenAI(
        api_key=config.azure_api_key,
        azure_endpoint=config.azure_endpoint,
        api_version=config.azure_api_version,
    )
    encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")

    started = time.perf_counter()
    response = client.chat.completions.create(
        model=config.vision_deployment,
        temperature=0,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": f"Extract the fields from this invoice ({document_id})."},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{encoded}", "detail": "high"},
                    },
                ],
            },
        ],
    )
    latency_ms = (time.perf_counter() - started) * 1000
    usage = response.usage
    prompt_tokens = usage.prompt_tokens if usage else 0
    completion_tokens = usage.completion_tokens if usage else 0
    cost = round(
        prompt_tokens / 1000 * PROMPT_PRICE_PER_1K + completion_tokens / 1000 * COMPLETION_PRICE_PER_1K,
        6,
    )

    try:
        payload = json.loads(_strip_fence(response.choices[0].message.content or "{}"))
    except json.JSONDecodeError as error:
        return ExtractionResult(
            document_id=document_id,
            fields={
                name: FieldValue(name, None, None, 0.0, "model response was not valid JSON")
                for name in ALL_FIELDS
            },
            backend="azure",
            model=config.vision_deployment,
            latency_ms=latency_ms,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=cost,
            error=f"invalid JSON from the model: {error}",
        )

    raw_fields = payload.get("fields", {}) or {}
    fields: dict[str, FieldValue] = {}
    for name in ALL_FIELDS:
        entry = raw_fields.get(name) or {}
        value = _coerce(name, entry.get("value"))
        try:
            confidence = float(entry.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        # A value the model could not parse is not trustworthy whatever it claims.
        if value is None:
            confidence = 0.0
        fields[name] = FieldValue(
            name=name,
            value=value,
            raw=None if entry.get("value") is None else str(entry.get("value")),
            confidence=max(0.0, min(1.0, confidence)),
            evidence=str(entry.get("evidence", ""))[:300],
        )

    return ExtractionResult(
        document_id=document_id,
        fields=fields,
        line_items=payload.get("line_items", []) or [],
        backend="azure",
        model=config.vision_deployment,
        latency_ms=latency_ms,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cost_usd=cost,
    )

"""Runtime configuration, resolved once from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Fields an accounts-payable clerk would re-key by hand if the machine got them
# wrong, so they carry a stricter confidence bar than the rest.
CRITICAL_FIELDS = ("invoice_number", "vendor_tax_id", "total_amount", "po_number")

ALL_FIELDS = (
    "invoice_number",
    "invoice_date",
    "due_date",
    "vendor_name",
    "vendor_tax_id",
    "po_number",
    "currency",
    "subtotal",
    "tax_amount",
    "total_amount",
)


@dataclass
class Config:
    backend: str
    azure_endpoint: str
    azure_api_key: str
    azure_api_version: str
    vision_deployment: str
    corpus_directory: Path
    database_path: Path
    baseline_path: Path
    field_confidence_floor: float
    critical_confidence_floor: float
    manual_approval_amount: float
    drift_psi_alert: float
    auto_post_rate_alert_drop: float
    manual_handling_seconds: float
    review_handling_seconds: float

    @property
    def azure_is_configured(self) -> bool:
        return bool(self.azure_api_key and self.azure_endpoint and self.vision_deployment)


def load_config() -> Config:
    requested_backend = os.getenv("DOCAGENT_BACKEND", "auto").lower()
    azure_api_key = os.getenv("AZURE_OPENAI_API_KEY", "")
    azure_endpoint = os.getenv("AZURE_OPENAI_ENDPOINT", "")
    vision_deployment = os.getenv("AZURE_OPENAI_VISION_DEPLOYMENT", "gpt-4o")

    azure_is_configured = bool(azure_api_key and azure_endpoint and vision_deployment)
    if requested_backend == "auto":
        backend = "azure" if azure_is_configured else "offline"
    else:
        backend = requested_backend

    return Config(
        backend=backend,
        azure_endpoint=azure_endpoint,
        azure_api_key=azure_api_key,
        azure_api_version=os.getenv("AZURE_OPENAI_API_VERSION", "2024-10-21"),
        vision_deployment=vision_deployment,
        corpus_directory=Path(os.getenv("DOCAGENT_CORPUS_DIR", PROJECT_ROOT / "data" / "corpus")),
        database_path=Path(os.getenv("DOCAGENT_DB_PATH", PROJECT_ROOT / "data" / "docagent.db")),
        baseline_path=Path(os.getenv("DOCAGENT_BASELINE_PATH", PROJECT_ROOT / "data" / "baseline.json")),
        field_confidence_floor=float(os.getenv("DOCAGENT_FIELD_FLOOR", "0.80")),
        critical_confidence_floor=float(os.getenv("DOCAGENT_CRITICAL_FLOOR", "0.92")),
        manual_approval_amount=float(os.getenv("DOCAGENT_MANUAL_APPROVAL_AMOUNT", "500000")),
        drift_psi_alert=float(os.getenv("DOCAGENT_DRIFT_PSI_ALERT", "0.2")),
        auto_post_rate_alert_drop=float(os.getenv("DOCAGENT_AUTO_POST_DROP_ALERT", "0.10")),
        manual_handling_seconds=float(os.getenv("DOCAGENT_MANUAL_SECONDS", "210")),
        review_handling_seconds=float(os.getenv("DOCAGENT_REVIEW_SECONDS", "90")),
    )

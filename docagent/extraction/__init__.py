"""Extractor selection: the Azure vision model when configured, the text-layer
parser otherwise. Both return the same ``ExtractionResult``."""

from __future__ import annotations

from pathlib import Path

from docagent.config import Config
from docagent.extraction.base import ExtractionResult, FieldValue

__all__ = ["ExtractionResult", "FieldValue", "extract_document"]


def extract_document(
    document_id: str, image_path: Path, text_path: Path, config: Config
) -> ExtractionResult:
    if config.backend == "azure":
        from docagent.extraction import vlm_azure

        return vlm_azure.extract(document_id, image_path, config)

    from docagent.extraction import offline

    return offline.extract(document_id, text_path.read_text(encoding="utf-8"))

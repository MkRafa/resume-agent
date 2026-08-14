"""Normalised document. Typed input and uploaded files converge here so that
nothing downstream needs to know which one the user chose."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

SourceType = Literal["typed", "pdf", "docx", "txt", "md", "image", "unknown"]


class Document(BaseModel):
    source_type: SourceType
    raw_text: str
    filename: str | None = None
    pages: int | None = None
    extraction_method: str | None = Field(
        None, description="e.g. 'pymupdf-textlayer', 'gemini-native', 'typed'"
    )
    confidence: float = Field(
        1.0, description="Low values (scanned/OCR) should trigger a user confirmation step."
    )
    warnings: list[str] = Field(default_factory=list)

    @property
    def looks_empty(self) -> bool:
        return len(self.raw_text.strip()) < 50

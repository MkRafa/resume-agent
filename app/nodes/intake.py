"""Intake: typed text or attached file, for both the profile and the JD.

Everything downstream sees a Document and never needs to know which the user chose.

These two nodes run in parallel, so they return only their NEW notes/errors -
the reducer in state.py concatenates them.
"""

from __future__ import annotations

from app.schemas import Document
from app.state import PipelineState
from app.tools import UnsupportedDocument, load_input


def _load(text: str | None, file: str | None, side: str) -> Document | str:
    """The document, or a user-facing error. An unreadable file is bad input,
    not a crash - it must come back as a run error with a reason, not a stack
    trace from inside the graph."""
    try:
        return load_input(text, file)
    except (UnsupportedDocument, FileNotFoundError, ValueError) as exc:
        return f"{side}: {exc}"


def intake_profile(state: PipelineState) -> dict:
    doc = _load(state.get("profile_text"), state.get("profile_file"), "Profile")
    if isinstance(doc, str):
        return {"errors": [doc]}
    if doc.looks_empty:
        return {"profile_doc": doc, "errors": ["Profile input produced almost no text."]}
    notes = []
    if doc.confidence < 0.5:
        notes.append(f"Low-confidence profile extraction: {'; '.join(doc.warnings)}")
    return {"profile_doc": doc, "notes": notes}


def intake_jd(state: PipelineState) -> dict:
    doc = _load(state.get("jd_text"), state.get("jd_file"), "Job description")
    if isinstance(doc, str):
        return {"errors": [doc]}
    if doc.looks_empty:
        return {"jd_doc": doc, "errors": ["Job description input produced almost no text."]}
    return {"jd_doc": doc}

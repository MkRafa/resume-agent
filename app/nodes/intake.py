"""Intake: typed text or attached file, for both the profile and the JD.

Everything downstream sees a Document and never needs to know which the user chose.

These two nodes run in parallel, so they return only their NEW notes/errors -
the reducer in state.py concatenates them.
"""

from __future__ import annotations

from app.state import PipelineState
from app.tools import load_input


def intake_profile(state: PipelineState) -> dict:
    doc = load_input(state.get("profile_text"), state.get("profile_file"))
    if doc.looks_empty:
        return {"profile_doc": doc, "errors": ["Profile input produced almost no text."]}
    notes = []
    if doc.confidence < 0.5:
        notes.append(f"Low-confidence profile extraction: {'; '.join(doc.warnings)}")
    return {"profile_doc": doc, "notes": notes}


def intake_jd(state: PipelineState) -> dict:
    doc = load_input(state.get("jd_text"), state.get("jd_file"))
    if doc.looks_empty:
        return {"jd_doc": doc, "errors": ["Job description input produced almost no text."]}
    return {"jd_doc": doc}

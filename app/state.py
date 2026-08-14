"""Graph state.

One typed dict threaded through every node. Nodes return partial updates;
LangGraph merges them. Keeping the whole state visible in one file is worth
more than elegance here - when a run goes wrong, this is the first thing you read.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, TypedDict

from app.schemas import (
    CareerGraph,
    Document,
    JobSpec,
    Scorecard,
    TailoredResume,
    VerifyReport,
)


def _last(_old, new):
    """Reducer: last write wins. Explicit so parallel branches are unambiguous."""
    return new


def _append(old: list | None, new: list | None) -> list:
    """Reducer: concatenate.

    Required because the profile and JD branches run in parallel and both write
    `notes` and `errors`. Without a reducer LangGraph raises InvalidUpdateError
    on concurrent writes to the same key. Nodes therefore return only their NEW
    entries, never the accumulated list.
    """
    return [*(old or []), *(new or [])]


class PipelineState(TypedDict, total=False):
    # --- inputs (either typed text or a file path, for each of the two sides)
    profile_text: str | None
    profile_file: str | None
    jd_text: str | None
    jd_file: str | None
    # Supplied at intake when the resume itself has no contact details.
    email_hint: str | None
    phone_hint: str | None

    # --- normalised documents
    profile_doc: Annotated[Document | None, _last]
    jd_doc: Annotated[Document | None, _last]

    # --- extracted artifacts
    graph: Annotated[CareerGraph | None, _last]
    job: Annotated[JobSpec | None, _last]
    years_experience: float

    # --- matching
    scorecard: Scorecard | None

    # --- generation
    selected_fact_ids: list[str]
    resume: TailoredResume | None
    verify_report: VerifyReport | None
    resolved_claims: list[str]  # blockers the user has explicitly accepted

    # --- output
    out_dir: Path | None
    artifacts: dict[str, str]

    # --- diagnostics (append-reduced; nodes emit only their new entries)
    errors: Annotated[list[str], _append]
    notes: Annotated[list[str], _append]


def new_state(**kwargs) -> PipelineState:
    base: PipelineState = {
        "selected_fact_ids": [],
        "resolved_claims": [],
        "artifacts": {},
        "errors": [],
        "notes": [],
        "years_experience": 0.0,
    }
    base.update(kwargs)  # type: ignore[typeddict-item]
    return base

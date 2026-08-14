"""Extraction nodes: document -> CareerGraph, document -> JobSpec.

Identity is resolved in Python after the model returns. The model reports the
contact details it saw; it does not get to decide the profile key.
"""

from __future__ import annotations

from app.models import complete_json
from app.schemas import CareerGraph, ExtractedProfile, JobSpec
from app.skills import load
from app.state import PipelineState
from app.tools import graph_years_of_experience, resolve_identity
from app.tools.identity import NeedsIdentity


def build_career_graph(state: PipelineState) -> dict:
    doc = state["profile_doc"]
    assert doc is not None

    extracted = complete_json(
        ExtractedProfile,
        node="extract",
        system=load("extract_profile"),
        variable_context=f"CANDIDATE SOURCE DOCUMENT:\n\n{doc.raw_text}",
    )

    # Identity: email primary, phone fallback. Hints from the intake form win
    # over whatever the document happened to contain, since the user typed
    # them deliberately.
    try:
        identity = resolve_identity(
            state.get("email_hint") or extracted.email,
            state.get("phone_hint") or extracted.phone,
        )
    except NeedsIdentity as exc:
        return {"errors": [str(exc)]}

    graph = CareerGraph(
        identity=identity,
        full_name=extracted.full_name,
        location=extracted.location,
        links=extracted.links,
        headline=extracted.headline,
        atoms=extracted.atoms,
    )

    # Computed, never generated - see app/tools/dates.py for why.
    years = graph_years_of_experience(graph)

    return {
        "graph": graph,
        "years_experience": years,
        "notes": [
            f"Career graph: {len(graph.atoms)} atoms, {years} yrs experience, "
            f"key={identity.primary_key}"
        ],
    }


def parse_jd(state: PipelineState) -> dict:
    doc = state["jd_doc"]
    assert doc is not None

    job = complete_json(
        JobSpec,
        node="parse",
        system=load("parse_jd"),
        variable_context=f"JOB DESCRIPTION:\n\n{doc.raw_text}",
    )

    gates = sum(1 for r in job.requirements if r.kind == "gate")
    musts = sum(1 for r in job.requirements if r.kind == "must")
    return {
        "job": job,
        "notes": [f"Job spec: {len(job.requirements)} requirements ({gates} gates, {musts} musts)"],
    }

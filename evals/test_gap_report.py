"""Gap report: every requirement that moved the verdict gets explained.

Regression: the report only covered 'none' and 'transferable' rows, but the
verdict rule also counts 'unknown' on a scorable must-have as absent - so a
requirement could sink the verdict and never appear among the gaps.
"""

from __future__ import annotations

from app.nodes import matching
from app.schemas import (
    CareerGraph, FactAtom, Identity, JobSpec, Requirement, Scorecard, ScorecardRow,
)


def test_unknown_on_a_scorable_must_is_reported(monkeypatch):
    seen: dict[str, str] = {}

    def fake(schema, **kwargs):
        seen["context"] = kwargs["variable_context"]
        return schema()

    monkeypatch.setattr(matching, "complete_json", fake)
    job = JobSpec(requirements=[
        Requirement(id="r_01", kind="must", category="skill", text="Production Kubernetes"),
        Requirement(id="r_02", kind="gate", category="work_authorization", text="Authorized in India"),
        Requirement(id="r_03", kind="must", category="skill", text="Python"),
    ])
    scorecard = Scorecard(rows=[
        ScorecardRow(requirement_id="r_01", grade="unknown", rationale="unclear"),
        ScorecardRow(requirement_id="r_02", grade="unknown", rationale="never on a resume"),
        ScorecardRow(requirement_id="r_03", grade="direct", evidence_fact_ids=["f_001"], rationale="ok"),
    ], verdict="partial_match")
    graph = CareerGraph(identity=Identity(primary_key="a@b.co", keys=["a@b.co"]),
                        atoms=[FactAtom(id="f_001", type="achievement", raw_text="Python.")])

    matching.gap_report({"graph": graph, "job": job, "scorecard": scorecard})

    weak_section = seen["context"].split("WEAK OR MISSING REQUIREMENTS:")[1]
    assert "[r_01]" in weak_section           # scorable unknown: explained
    assert "[r_02]" not in weak_section       # work authorization: an open question
    assert "[r_03]" not in weak_section

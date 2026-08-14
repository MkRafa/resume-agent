"""Matching: grade evidence per requirement, then compute the verdict by rule.

The split is the whole point. The model does judgement it is good at (does this
atom support this requirement, and how strongly). Python does the decision it
must be consistent about (what those grades add up to).
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.models import complete_json
from app.schemas import Gap, Scorecard, ScorecardRow, ScorecardRows
from app.skills import load
from app.state import PipelineState
from app.tools import compute_verdict


class GapAnalysis(BaseModel):
    gaps: list[Gap] = Field(default_factory=list)
    adjacent_roles: list[str] = Field(default_factory=list)


def match(state: PipelineState) -> dict:
    # This is the join of the profile and JD branches, so it is where an
    # upstream failure on either side is caught. Returning no scorecard routes
    # the run to END without a misleading verdict.
    graph, job = state.get("graph"), state.get("job")
    if state.get("errors") or graph is None or job is None:
        missing = [n for n, v in (("profile", graph), ("job description", job)) if v is None]
        return {"errors": [f"Cannot match: {' and '.join(missing)} unavailable."] if missing else []}

    # Degenerate extractions must fail loudly rather than flow downstream. An
    # empty career graph or a requirement-less job spec both produce a
    # meaningless - and misleadingly positive - verdict.
    if not graph.atoms:
        return {"errors": ["Career graph is empty; the profile failed to extract."]}
    if not [r for r in job.requirements if r.kind in {"gate", "must"} and not r.boilerplate]:
        return {
            "errors": [
                f"Job spec has no gradable requirements ({len(job.requirements)} parsed). "
                "The job description failed to parse - refusing to emit a verdict."
            ]
        }

    result = complete_json(
        ScorecardRows,
        node="match",
        system=load("match_grader"),
        # Stable prefix first: identical across every application this
        # candidate makes, so it is the part a provider can cache.
        stable_context=f"CANDIDATE CAREER FACTS:\n\n{graph.render_for_prompt()}",
        variable_context=(
            f"COMPUTED TOTAL EXPERIENCE: {state.get('years_experience', 0)} years "
            "(authoritative - do not recount)\n\n"
            f"{job.render_for_prompt()}\n\n"
            "Grade every requirement above. One row each, ids matching exactly."
        ),
        temperature=0.0,
    )

    scorecard = Scorecard(rows=result.rows)
    _drop_bad_citations(scorecard, graph)
    _fill_missing_rows(scorecard, job)
    compute_verdict(scorecard, job)

    return {
        "scorecard": scorecard,
        "notes": [f"Verdict: {scorecard.verdict} (must coverage {scorecard.must_coverage:.0%})"],
    }


def _drop_bad_citations(scorecard: Scorecard, graph) -> None:
    """Strip citations to atoms that do not exist, and demote rows left with none.

    A model that cites a hallucinated atom id has not actually found evidence,
    and letting the citation stand would put an unverifiable claim into the
    scorecard the tailorer then trusts.
    """
    for row in scorecard.rows:
        real = [fid for fid in row.evidence_fact_ids if graph.by_id(fid)]
        if real != row.evidence_fact_ids:
            row.evidence_fact_ids = real
            if not real and row.grade != "none":
                row.grade = "none"
                row.rationale = f"(evidence citation was invalid) {row.rationale}"


def _fill_missing_rows(scorecard: Scorecard, job) -> None:
    """Any requirement the model skipped is graded 'none', not silently ignored.

    Without this, dropping a requirement would quietly improve the verdict.
    """
    graded = {r.requirement_id for r in scorecard.rows}
    for req in job.requirements:
        if req.id not in graded:
            scorecard.rows.append(
                ScorecardRow(
                    requirement_id=req.id,
                    grade="none",
                    evidence_fact_ids=[],
                    rationale="Not graded by the model; treated as unevidenced.",
                )
            )
    # Drop rows for requirements that do not exist.
    valid = {r.id for r in job.requirements}
    scorecard.rows = [r for r in scorecard.rows if r.requirement_id in valid]


def gap_report(state: PipelineState) -> dict:
    """Explain the gaps and, on a hard no, name roles this profile would fit."""
    graph, job, scorecard = state["graph"], state["job"], state["scorecard"]
    assert graph is not None and job is not None and scorecard is not None

    weak = [r for r in scorecard.rows if r.grade in {"none", "transferable"}]
    if not weak:
        return {}

    detail = "\n".join(
        f"[{r.requirement_id}] ({job.by_id(r.requirement_id).kind if job.by_id(r.requirement_id) else '?'}) "
        f"{job.by_id(r.requirement_id).text if job.by_id(r.requirement_id) else '?'}"
        f" -> {r.grade}: {r.rationale}"
        for r in weak
    )

    analysis = complete_json(
        GapAnalysis,
        node="match",
        system=load("gap_report"),
        stable_context=f"CANDIDATE CAREER FACTS:\n\n{graph.render_for_prompt()}",
        variable_context=(
            f"{job.render_for_prompt()}\n\n"
            f"VERDICT: {scorecard.verdict}\n"
            f"REASONS: {'; '.join(scorecard.verdict_reasons)}\n\n"
            f"WEAK OR MISSING REQUIREMENTS:\n{detail}\n\n"
            + (
                "Verdict is not_matching - populate adjacent_roles."
                if scorecard.verdict == "not_matching"
                else "Verdict is a partial match - adjacent_roles may be empty."
            )
        ),
        temperature=0.3,
    )

    scorecard.gaps = analysis.gaps
    scorecard.adjacent_roles = analysis.adjacent_roles
    return {"scorecard": scorecard}

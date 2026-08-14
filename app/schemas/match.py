"""Evidence scorecard and verdict.

Deliberately NOT a 0-100 score from a model. The model grades evidence per
requirement; the verdict is a deterministic rule over those grades, so it is
explainable, stable across runs, and testable against a gold set.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Grade = Literal["direct", "adjacent", "transferable", "none", "unknown"]
Verdict = Literal["strong_match", "partial_match", "not_matching"]

GRADE_WEIGHT: dict[Grade, float] = {
    "direct": 1.0,
    "adjacent": 0.6,
    "transferable": 0.3,
    "none": 0.0,
    # 'unknown' is never scored - it is excluded from the coverage denominator
    # entirely. See UNSCORABLE_CATEGORIES below.
    "unknown": 0.0,
}

# Requirements a resume essentially never speaks to. Work authorization,
# relocation willingness and security clearance are absent from virtually every
# resume, so grading them 'none' punishes the candidate for a convention rather
# than a gap - and since these are usually gates, a single boilerplate line
# ("must be authorized to work in X") would reject nearly everyone.
#
# These grade 'unknown' instead: they do not fail a gate and do not count
# toward coverage. They surface as an open question for the candidate to
# confirm, which is the honest handling - we genuinely do not know.
UNSCORABLE_CATEGORIES = {"work_authorization"}


class ScorecardRow(BaseModel):
    requirement_id: str
    grade: Grade = Field(
        description=(
            "direct = did exactly this. "
            "adjacent = did the transferable neighbour (Rust for a Go role). "
            "transferable = same underlying skill, different domain. "
            "none = no supporting evidence, and a resume would normally show it. "
            "unknown = a resume would not normally state this either way "
            "(work authorization, clearance, relocation), so absence proves nothing."
        )
    )
    evidence_fact_ids: list[str] = Field(
        default_factory=list,
        description="Atom ids supporting the grade. MUST be empty when grade is 'none'.",
    )
    rationale: str = Field(description="One sentence. Cites the evidence, not vibes.")


class Gap(BaseModel):
    requirement_id: str
    severity: Literal["dealbreaker", "significant", "coachable"]
    explanation: str
    how_to_address: str | None = Field(
        None, description="Honest framing advice or a concrete learning step."
    )


class Scorecard(BaseModel):
    rows: list[ScorecardRow] = Field(default_factory=list)
    verdict: Verdict | None = None
    verdict_reasons: list[str] = Field(default_factory=list)
    gates_failed: list[str] = Field(default_factory=list)
    must_coverage: float = 0.0
    gaps: list[Gap] = Field(default_factory=list)
    open_questions: list[str] = Field(
        default_factory=list,
        description=(
            "Requirements that could not be judged from a resume and need the "
            "candidate to confirm. These do not fail a gate."
        ),
    )
    adjacent_roles: list[str] = Field(
        default_factory=list,
        description="Populated on not_matching: role titles this profile would fit.",
    )

    def row_for(self, rid: str) -> ScorecardRow | None:
        return next((r for r in self.rows if r.requirement_id == rid), None)


class ScorecardRows(BaseModel):
    """The LLM only ever produces rows. Verdict is computed downstream."""

    rows: list[ScorecardRow]

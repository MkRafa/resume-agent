"""The verdict rule.

Deliberately deterministic Python over the model-produced scorecard rows. This
is what makes the verdict explainable ("you failed r_02"), stable across runs,
and testable against a gold set. Asking a model for a 0-100 match score gives
you none of those three.
"""

from __future__ import annotations

from app.schemas import GRADE_WEIGHT, JobSpec, Scorecard, Verdict
from app.schemas.match import UNSCORABLE_CATEGORIES

STRONG_MUST_COVERAGE = 0.80
PARTIAL_MUST_COVERAGE = 0.50
MAX_MUST_MISSING_FOR_PARTIAL = 2

# A gate graded 'adjacent' still passes - "5+ years backend" is met by 4.8
# years, and a rigid reading rejects people who would sail through the screen.
#
# 'unknown' does NOT pass on its own. Whether a requirement is excusable is a
# property of the REQUIREMENT (work authorization is never on a resume), not of
# what the model chose to say about it. Letting a bare 'unknown' clear a gate
# hands every model a universal bypass: a local 7B graded "Bachelor's degree"
# as unknown for a resume that plainly listed one, and sailed through the gate.
# Unknown on a scorable requirement is treated as no evidence.
GATE_PASSING_GRADES = {"direct", "adjacent"}


class UngradableJob(ValueError):
    """A job spec with nothing to grade against."""


def compute_verdict(scorecard: Scorecard, job: JobSpec) -> Scorecard:
    """Fill verdict, reasons, gates_failed and must_coverage on the scorecard."""
    # A JD that parsed into no gates and no must-haves cannot produce a
    # meaningful verdict. Without this guard the "no musts -> coverage 1.0"
    # branch below turns a FAILED PARSE into a strong_match - silently telling
    # a candidate they are perfect for a job nobody ever read. Found by feeding
    # the pipeline a weak local model that returned an empty requirements list.
    gradable = [r for r in job.requirements if r.kind in {"gate", "must"} and not r.boilerplate]
    if not gradable:
        raise UngradableJob(
            f"Job spec has no gradable requirements "
            f"({len(job.requirements)} parsed, all nice/implicit/boilerplate). "
            "The job description probably failed to parse - refusing to emit a verdict."
        )

    gates = [r for r in job.requirements if r.kind == "gate"]
    musts = [r for r in job.requirements if r.kind == "must" and not r.boilerplate]

    def _unscorable(rid: str) -> bool:
        """Excusable by category only — never by what the model claimed."""
        req = job.by_id(rid)
        return req is not None and req.category in UNSCORABLE_CATEGORIES

    gates_failed = [
        g.id
        for g in gates
        if not _unscorable(g.id)
        and ((row := scorecard.row_for(g.id)) is None or row.grade not in GATE_PASSING_GRADES)
    ]

    # Unscorable requirements leave the denominator entirely rather than
    # scoring zero - otherwise "must be authorized to work here" quietly drags
    # every candidate's coverage down for something no resume states.
    scorable_musts = [m for m in musts if not _unscorable(m.id)]
    if scorable_musts:
        earned = sum(
            GRADE_WEIGHT[row.grade]
            for m in scorable_musts
            if (row := scorecard.row_for(m.id)) is not None
        )
        must_coverage = round(earned / len(scorable_musts), 3)
    else:
        must_coverage = 1.0

    # 'unknown' on a scorable requirement is absence of evidence, not a pass.
    musts_absent = [
        m.id
        for m in scorable_musts
        if (row := scorecard.row_for(m.id)) is None or row.grade in {"none", "unknown"}
    ]

    scorecard.open_questions = [
        f"{req.text} — we cannot tell from your resume. Please confirm."
        for r in job.requirements
        if r.kind in {"gate", "must"} and _unscorable(r.id) and (req := r)
    ]

    reasons: list[str] = []
    verdict: Verdict

    if gates_failed:
        verdict = "not_matching"
        labels = ", ".join(
            f"{rid} ({job.by_id(rid).text})" if job.by_id(rid) else rid for rid in gates_failed
        )
        reasons.append(f"Hard requirement not met: {labels}.")
    elif len(musts_absent) > MAX_MUST_MISSING_FOR_PARTIAL:
        verdict = "not_matching"
        reasons.append(
            f"{len(musts_absent)} must-have requirements have no supporting evidence "
            f"({', '.join(musts_absent)})."
        )
    elif must_coverage >= STRONG_MUST_COVERAGE and not musts_absent:
        verdict = "strong_match"
        reasons.append(
            f"All hard requirements met and {must_coverage:.0%} of must-haves "
            "backed by direct evidence."
        )
    elif must_coverage >= PARTIAL_MUST_COVERAGE:
        verdict = "partial_match"
        reasons.append(
            f"Hard requirements met; must-have coverage is {must_coverage:.0%}."
        )
        if musts_absent:
            reasons.append(f"No evidence for: {', '.join(musts_absent)}.")
    else:
        verdict = "not_matching"
        reasons.append(
            f"Must-have coverage is only {must_coverage:.0%} "
            f"(needs {PARTIAL_MUST_COVERAGE:.0%} to be a partial match)."
        )

    scorecard.verdict = verdict
    scorecard.verdict_reasons = reasons
    scorecard.gates_failed = gates_failed
    scorecard.must_coverage = must_coverage
    return scorecard


def strongest_hooks(scorecard: Scorecard, job: JobSpec, limit: int = 3) -> list[str]:
    """The requirements to lead with. Used for the summary line."""
    ranked = sorted(
        (
            (r, scorecard.row_for(r.id))
            for r in job.requirements
            if r.kind in {"gate", "must"}
        ),
        key=lambda pair: GRADE_WEIGHT[pair[1].grade] if pair[1] else 0.0,
        reverse=True,
    )
    return [r.text for r, row in ranked[:limit] if row and row.grade == "direct"]

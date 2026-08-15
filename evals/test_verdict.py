"""The verdict rule.

This is the layer that makes the product's core claim testable: the same
scorecard must always produce the same verdict, and every verdict must point at
the requirement it turned on. These tests are the regression suite for that.
"""

import pytest

from app.schemas import JobSpec, Requirement, Scorecard, ScorecardRow
from app.tools.verdict import UngradableJob, compute_verdict, strongest_hooks


def job(*specs) -> JobSpec:
    """specs: (id, kind, text, boilerplate?, category?)"""
    return JobSpec(
        title="Senior Backend Engineer",
        requirements=[
            Requirement(
                id=s[0],
                kind=s[1],
                text=s[2],
                category=s[4] if len(s) > 4 else "skill",
                boilerplate=len(s) > 3 and s[3],
            )
            for s in specs
        ],
    )


def card(**grades) -> Scorecard:
    return Scorecard(
        rows=[
            ScorecardRow(
                requirement_id=rid,
                grade=grade,
                evidence_fact_ids=[] if grade == "none" else ["f_001"],
                rationale="test",
            )
            for rid, grade in grades.items()
        ]
    )


def test_failed_gate_forces_not_matching_regardless_of_everything_else():
    j = job(("r_01", "gate", "5+ years"), ("r_02", "must", "Go"), ("r_03", "must", "K8s"))
    result = compute_verdict(card(r_01="none", r_02="direct", r_03="direct"), j)
    assert result.verdict == "not_matching"
    assert result.gates_failed == ["r_01"]
    assert "5+ years" in result.verdict_reasons[0]


def test_gate_met_adjacently_still_passes():
    """4.8 years against '5+ years' should not auto-reject someone who would
    sail through the actual screen."""
    j = job(("r_01", "gate", "5+ years"), ("r_02", "must", "Go"))
    result = compute_verdict(card(r_01="adjacent", r_02="direct"), j)
    assert result.gates_failed == []
    assert result.verdict in {"strong_match", "partial_match"}


def test_strong_match_needs_full_must_coverage():
    j = job(("r_01", "gate", "5+ years"), ("r_02", "must", "Go"), ("r_03", "must", "K8s"))
    result = compute_verdict(card(r_01="direct", r_02="direct", r_03="direct"), j)
    assert result.verdict == "strong_match"
    assert result.must_coverage == 1.0


def test_one_missing_must_is_a_partial_not_a_strong():
    j = job(
        ("r_01", "gate", "5+ years"),
        ("r_02", "must", "Go"),
        ("r_03", "must", "K8s"),
        ("r_04", "must", "Payments"),
    )
    result = compute_verdict(card(r_01="direct", r_02="direct", r_03="direct", r_04="none"), j)
    assert result.verdict == "partial_match"
    assert "r_04" in result.verdict_reasons[-1]


def test_three_absent_musts_is_not_matching():
    j = job(
        ("r_01", "gate", "5+ years"),
        ("r_02", "must", "Go"),
        ("r_03", "must", "K8s"),
        ("r_04", "must", "Payments"),
        ("r_05", "must", "gRPC"),
    )
    result = compute_verdict(
        card(r_01="direct", r_02="none", r_03="none", r_04="none", r_05="direct"), j
    )
    assert result.verdict == "not_matching"


def test_boilerplate_musts_cannot_sink_a_candidate():
    """'Strong communication skills' appears in every JD and carries no signal.
    Counting it would let filler text drag a strong profile to partial."""
    j = job(
        ("r_01", "gate", "5+ years"),
        ("r_02", "must", "Go"),
        ("r_03", "must", "Strong communication skills", True),
        ("r_04", "must", "Team player", True),
    )
    result = compute_verdict(card(r_01="direct", r_02="direct", r_03="none", r_04="none"), j)
    assert result.verdict == "strong_match"


def test_nice_to_haves_do_not_affect_the_verdict():
    j = job(("r_01", "gate", "5+ years"), ("r_02", "must", "Go"), ("r_03", "nice", "Terraform"))
    with_nice = compute_verdict(card(r_01="direct", r_02="direct", r_03="direct"), j).verdict
    without = compute_verdict(card(r_01="direct", r_02="direct", r_03="none"), j).verdict
    assert with_nice == without == "strong_match"


def test_ungraded_requirement_counts_as_none_not_as_absent():
    """A requirement the model forgot must not quietly improve the verdict."""
    j = job(("r_01", "gate", "5+ years"), ("r_02", "must", "Go"))
    result = compute_verdict(card(r_02="direct"), j)  # r_01 never graded
    assert result.verdict == "not_matching"
    assert result.gates_failed == ["r_01"]


def test_partial_grades_accumulate_toward_coverage():
    j = job(("r_01", "must", "Go"), ("r_02", "must", "K8s"))
    result = compute_verdict(card(r_01="adjacent", r_02="adjacent"), j)
    assert result.must_coverage == pytest.approx(0.6)
    assert result.verdict == "partial_match"


def test_verdict_is_deterministic():
    j = job(("r_01", "gate", "5+ years"), ("r_02", "must", "Go"))
    verdicts = {compute_verdict(card(r_01="direct", r_02="adjacent"), j).verdict for _ in range(10)}
    assert len(verdicts) == 1


def test_every_verdict_carries_a_reason():
    j = job(("r_01", "gate", "5+ years"), ("r_02", "must", "Go"))
    for grades in [
        {"r_01": "direct", "r_02": "direct"},
        {"r_01": "direct", "r_02": "none"},
        {"r_01": "none", "r_02": "direct"},
    ]:
        assert compute_verdict(card(**grades), j).verdict_reasons


def test_empty_job_spec_refuses_to_produce_a_verdict():
    """A failed JD parse must not become a strong match.

    Regression: with zero must-haves the coverage branch returned 1.0 and no
    gates could fail, so an unparsed job description scored a perfect match.
    Caught by feeding the pipeline a weak local model that returned no
    requirements at all.
    """
    with pytest.raises(UngradableJob):
        compute_verdict(Scorecard(rows=[]), JobSpec(title="Anything"))


def test_job_of_only_boilerplate_and_nice_to_haves_is_ungradable():
    j = job(
        ("r_01", "must", "Strong communication skills", True),
        ("r_02", "nice", "Terraform"),
    )
    with pytest.raises(UngradableJob):
        compute_verdict(card(r_01="direct", r_02="direct"), j)


def test_a_single_real_gate_is_enough_to_grade():
    j = job(("r_01", "gate", "5+ years"), ("r_02", "nice", "Terraform"))
    assert compute_verdict(card(r_01="direct", r_02="none"), j).verdict == "strong_match"


def test_work_authorization_gate_does_not_reject_on_resume_silence():
    """The bug the first real run found.

    'Must be authorized to work in India' appears in a large share of postings,
    and virtually no resume states work authorization either way. Grading it
    'none' failed the gate and returned NOT MATCHING for a candidate living and
    working in Bengaluru — and would have done so for nearly every candidate on
    nearly every job.
    """
    j = job(
        ("r_01", "gate", "5+ years", False, "experience"),
        ("r_02", "must", "Go", False, "skill"),
        ("r_19", "gate", "Authorization to work in India", False, "work_authorization"),
    )
    result = compute_verdict(card(r_01="direct", r_02="direct", r_19="none"), j)

    assert result.gates_failed == []
    assert result.verdict == "strong_match"
    assert any("Authorization to work in India" in q for q in result.open_questions)


def test_unknown_on_an_unscorable_category_passes_a_gate():
    j = job(
        ("r_01", "gate", "5+ years", False, "experience"),
        ("r_02", "gate", "Security clearance", False, "work_authorization"),
    )
    result = compute_verdict(card(r_01="direct", r_02="unknown"), j)
    assert result.gates_failed == []
    assert len(result.open_questions) == 1


def test_location_gate_does_not_reject_on_resume_silence():
    """The single biggest calibration bug the first full gold run exposed.

    13 of 14 location gates failed. "Must be located in India (Remote)" grades
    'unknown' because no resume states willingness to relocate — even when the
    resume gives an address in the right city. That alone dragged the gold set
    to 59% agreement with 9 over-strict verdicts; excusing it took it to 82%.

    Same class as the work-authorization bug — that fix was simply too narrow.
    """
    j = job(
        ("r_01", "gate", "Must be located in India (Remote)", False, "location"),
        ("r_02", "gate", "Master's degree", False, "education"),
        ("r_03", "must", "PyTorch", False, "skill"),
    )
    result = compute_verdict(card(r_01="unknown", r_02="direct", r_03="direct"), j)

    assert result.gates_failed == []
    assert result.verdict == "strong_match"
    assert any("located in India" in q for q in result.open_questions)


def test_unscorable_gates_do_not_inflate_a_genuinely_weak_profile():
    """Excusing location and work-auth must not rescue a candidate who fails on
    the requirements a resume *can* speak to."""
    j = job(
        ("r_01", "gate", "Located in India", False, "location"),
        ("r_02", "gate", "Authorized to work in India", False, "work_authorization"),
        ("r_03", "gate", "5+ years backend", False, "experience"),
        ("r_04", "must", "Go", False, "skill"),
    )
    result = compute_verdict(card(r_01="unknown", r_02="unknown", r_03="none", r_04="none"), j)
    assert result.gates_failed == ["r_03"]
    assert result.verdict == "not_matching"


def test_unknown_cannot_be_used_to_bypass_a_scorable_gate():
    """A grader must not be able to clear any gate by declaring it unknown.

    Regression: a local model graded "Bachelor's degree in CS" as unknown for a
    resume that plainly listed one, and the gate passed. Excusability is a
    property of the requirement's category, not of the model's answer.
    """
    j = job(
        ("r_01", "gate", "5+ years", False, "experience"),
        ("r_02", "gate", "Bachelor's degree in CS", False, "education"),
    )
    result = compute_verdict(card(r_01="direct", r_02="unknown"), j)
    assert result.gates_failed == ["r_02"]
    assert result.verdict == "not_matching"


def test_unknown_on_a_scorable_must_counts_as_absent():
    j = job(
        ("r_01", "must", "Go", False, "skill"),
        ("r_02", "must", "Kubernetes", False, "skill"),
        ("r_03", "must", "Kafka", False, "skill"),
    )
    result = compute_verdict(card(r_01="unknown", r_02="direct", r_03="direct"), j)
    assert result.verdict == "partial_match"
    assert result.must_coverage < 1.0


def test_unscorable_musts_leave_the_coverage_denominator():
    """Otherwise an unanswerable requirement silently drags coverage down."""
    j = job(
        ("r_01", "must", "Go", False, "skill"),
        ("r_02", "must", "Kubernetes", False, "skill"),
        ("r_03", "must", "Authorized to work in India", False, "work_authorization"),
    )
    result = compute_verdict(card(r_01="direct", r_02="direct", r_03="none"), j)
    assert result.must_coverage == 1.0
    assert result.verdict == "strong_match"


def test_a_real_gate_still_fails_normally():
    """The unknown escape hatch must not weaken genuine gates."""
    j = job(
        ("r_01", "gate", "5+ years", False, "experience"),
        ("r_02", "must", "Go", False, "skill"),
    )
    result = compute_verdict(card(r_01="none", r_02="direct"), j)
    assert result.gates_failed == ["r_01"]
    assert result.verdict == "not_matching"


def test_unknown_is_not_a_hedge_for_ordinary_skills():
    """An 'unknown' on a plain skill leaves it unproven and must lower coverage.

    This test originally asserted the opposite — that unknown was excluded from
    the denominator, giving 100% coverage. That encoded the bypass bug: a
    grader could inflate any profile to a strong match by answering 'unknown'.
    Only genuinely unscorable CATEGORIES leave the denominator.
    """
    j = job(("r_01", "must", "Go", False, "skill"), ("r_02", "must", "K8s", False, "skill"))
    result = compute_verdict(card(r_01="unknown", r_02="direct"), j)
    assert result.must_coverage == 0.5
    assert "r_01" in " ".join(result.verdict_reasons)


def test_strongest_hooks_returns_only_direct_evidence():
    j = job(("r_01", "gate", "5+ years"), ("r_02", "must", "Go"), ("r_03", "must", "K8s"))
    hooks = strongest_hooks(card(r_01="direct", r_02="none", r_03="adjacent"), j)
    assert hooks == ["5+ years"]

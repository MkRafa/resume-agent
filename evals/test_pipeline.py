"""End-to-end pipeline test with a stubbed model layer.

Runs the whole graph - fan-out, join, routing, guardrail, render - without a
single API call. This is what tells you the plumbing is right before you spend
a quota on prompt quality, and it is the regression net for graph edits.
"""

from __future__ import annotations

import pytest

from app.hooks.guardrail import RenderBlocked
from app.schemas import (
    Bullet,
    ExperienceBlock,
    ExtractedProfile,
    FactAtom,
    JobSpec,
    Requirement,
    ScorecardRow,
    ScorecardRows,
    SelectedFacts,
    TailoredResume,
    VerifyFlag,
    VerifyReport,
)

ATOMS = [
    FactAtom(
        id="f_001",
        type="achievement",
        raw_text="Cut p99 settlement latency from 1.8s to 640ms with a Kafka write-behind queue.",
        company="Northwind",
        role="Senior Backend Engineer",
        start="2022-03",
        end="present",
        skills=["kafka", "python", "performance"],
        evidence_strength="led",
    ),
    FactAtom(
        id="f_002",
        type="achievement",
        raw_text="Helped migrate 14 services to EKS.",
        company="Northwind",
        role="Senior Backend Engineer",
        start="2022-03",
        end="present",
        skills=["kubernetes", "terraform"],
        evidence_strength="contributed",
    ),
]

JOB = JobSpec(
    title="Senior Backend Engineer",
    company="Meridian",
    min_years=5.0,
    vocabulary=["Go", "Kubernetes", "Kafka", "Postgres", "payments"],
    requirements=[
        Requirement(id="r_01", kind="gate", text="5+ years backend", category="experience"),
        Requirement(id="r_02", kind="must", text="Kubernetes in production", category="skill"),
        Requirement(id="r_03", kind="must", text="Event-driven architecture", category="skill"),
        Requirement(id="r_04", kind="must", text="Strong Go", category="skill"),
    ],
)


def _resume() -> TailoredResume:
    return TailoredResume(
        full_name="Priya Raghavan",
        summary="Backend engineer focused on payments infrastructure.",
        summary_fact_ids=["f_001"],
        skills=["Kafka", "Kubernetes", "Python"],
        experience=[
            ExperienceBlock(
                company="Northwind",
                role="Senior Backend Engineer",
                start="2022-03",
                end="present",
                bullets=[
                    Bullet(
                        text="Cut p99 settlement latency 1.8s -> 640ms via a Kafka write-behind queue.",
                        fact_ids=["f_001"],
                        targets=["r_03"],
                    ),
                    Bullet(
                        text="Contributed to migrating 14 services to EKS.",
                        fact_ids=["f_002"],
                        targets=["r_02"],
                    ),
                ],
            )
        ],
        education=["B.E. Computer Science, 2019"],
    )


@pytest.fixture
def stub_models(monkeypatch):
    """Route every complete_json call to a canned response by schema."""
    flags: list[VerifyFlag] = []

    def fake(schema, **kwargs):
        if schema is ExtractedProfile:
            return ExtractedProfile(
                full_name="Priya Raghavan",
                email="priya.raghavan@example.com",
                phone="+91 98765 43210",
                atoms=ATOMS,
            )
        if schema is JobSpec:
            return JOB
        if schema is ScorecardRows:
            return ScorecardRows(
                rows=[
                    ScorecardRow(requirement_id="r_01", grade="direct",
                                 evidence_fact_ids=["f_001"], rationale="6 yrs"),
                    ScorecardRow(requirement_id="r_02", grade="direct",
                                 evidence_fact_ids=["f_002"], rationale="EKS"),
                    ScorecardRow(requirement_id="r_03", grade="direct",
                                 evidence_fact_ids=["f_001"], rationale="Kafka"),
                    ScorecardRow(requirement_id="r_04", grade="direct",
                                 evidence_fact_ids=["f_001"], rationale="Go"),
                ]
            )
        if schema is SelectedFacts:
            return SelectedFacts(fact_ids=["f_001", "f_002"], reasoning="both cited")
        if schema is TailoredResume:
            return _resume()
        if schema is VerifyReport:
            return VerifyReport(flags=list(flags))
        # GapAnalysis and anything else
        return schema()

    for module in ("app.nodes.extract", "app.nodes.matching", "app.nodes.generate"):
        monkeypatch.setattr(f"{module}.complete_json", fake)
    return flags


def _run(tmp_path, **overrides):
    from app.graph import PIPELINE
    from app.state import new_state

    state = new_state(
        profile_text="Priya Raghavan, backend engineer. priya.raghavan@example.com",
        jd_text="Senior Backend Engineer at Meridian. 5+ years required.",
        out_dir=tmp_path,
        **overrides,
    )
    return PIPELINE.invoke(state)


def test_strong_match_runs_through_to_render(stub_models, tmp_path):
    final = _run(tmp_path)

    assert final["scorecard"].verdict == "strong_match"
    assert final["graph"].identity.primary_key == "priya.raghavan@example.com"
    # Phone retained as an alternate lookup key.
    assert "+919876543210" in final["graph"].identity.keys
    assert "resume_html" in final["artifacts"]
    assert (tmp_path / "resume.html").exists()
    assert (tmp_path / "provenance.json").exists()


def test_parallel_branches_join_without_write_conflict(stub_models, tmp_path):
    """The profile and JD branches both append notes. Without reducers on those
    keys LangGraph raises InvalidUpdateError here."""
    final = _run(tmp_path)
    notes = final["notes"]
    assert any("Career graph:" in n for n in notes)
    assert any("Job spec:" in n for n in notes)


def test_blocker_prevents_render(stub_models, tmp_path):
    stub_models.append(
        VerifyFlag(
            claim="Scaled the platform to 50M users",
            location="experience[0].bullets[0]",
            issue="unsupported_claim",
            severity="blocker",
            explanation="No atom mentions 50M users.",
        )
    )
    final = _run(tmp_path)

    assert final["verify_report"].blockers
    assert "resume_html" not in final.get("artifacts", {})
    assert not (tmp_path / "resume.html").exists()


def test_warning_does_not_prevent_render(stub_models, tmp_path):
    stub_models.append(
        VerifyFlag(
            claim="Contributed to migrating 14 services",
            location="experience[0].bullets[1]",
            issue="overstated_ownership",
            severity="warning",
            explanation="Defensible but generous.",
        )
    )
    final = _run(tmp_path)
    assert (tmp_path / "resume.html").exists()


def test_resolved_blocker_allows_render(stub_models, tmp_path):
    """Models the M1 human-in-the-loop step: user confirms the claim, run resumes."""
    claim = "Scaled the platform to 50M users"
    stub_models.append(
        VerifyFlag(
            claim=claim,
            location="experience[0].bullets[0]",
            issue="unsupported_claim",
            severity="blocker",
            explanation="No supporting atom.",
        )
    )
    final = _run(tmp_path, resolved_claims=[claim])
    assert (tmp_path / "resume.html").exists()


def test_unparsed_job_description_does_not_become_a_strong_match(
    stub_models, monkeypatch, tmp_path
):
    """The most dangerous silent failure: a JD that extracts to nothing used to
    score a perfect match, because 'no must-haves' meant 100% coverage."""

    def empty_job(schema, **kwargs):
        if schema is ExtractedProfile:
            return ExtractedProfile(
                full_name="Priya Raghavan", email="priya.raghavan@example.com", atoms=ATOMS
            )
        if schema is JobSpec:
            return JobSpec(title="Senior Backend Engineer")  # zero requirements
        return schema()

    for module in ("app.nodes.extract", "app.nodes.matching", "app.nodes.generate"):
        monkeypatch.setattr(f"{module}.complete_json", empty_job)

    final = _run(tmp_path)
    assert final.get("scorecard") is None
    assert final["errors"]
    assert "no gradable requirements" in final["errors"][0]
    assert not (tmp_path / "resume.html").exists()


def test_empty_career_graph_does_not_produce_a_verdict(stub_models, monkeypatch, tmp_path):
    def empty_profile(schema, **kwargs):
        if schema is ExtractedProfile:
            return ExtractedProfile(
                full_name="Priya Raghavan", email="priya.raghavan@example.com", atoms=[]
            )
        if schema is JobSpec:
            return JOB
        return schema()

    for module in ("app.nodes.extract", "app.nodes.matching", "app.nodes.generate"):
        monkeypatch.setattr(f"{module}.complete_json", empty_profile)

    final = _run(tmp_path)
    assert final.get("scorecard") is None
    assert "Career graph is empty" in final["errors"][0]


def test_identity_failure_aborts_before_matching(stub_models, monkeypatch, tmp_path):
    def no_contact(schema, **kwargs):
        if schema is ExtractedProfile:
            return ExtractedProfile(full_name="Anon", atoms=ATOMS)
        if schema is JobSpec:
            return JOB
        return schema()

    for module in ("app.nodes.extract", "app.nodes.matching", "app.nodes.generate"):
        monkeypatch.setattr(f"{module}.complete_json", no_contact)

    final = _run(tmp_path)
    assert final["errors"]
    assert "email" in final["errors"][0].lower()
    assert final.get("scorecard") is None


def test_hallucinated_fact_citation_is_stripped(stub_models, monkeypatch, tmp_path):
    """A model citing an atom id that does not exist has not found evidence."""
    from app.nodes import matching as match_module

    scorecard_holder = {}
    original = match_module.complete_json

    def citing_ghost(schema, **kwargs):
        if schema is ScorecardRows:
            return ScorecardRows(
                rows=[
                    ScorecardRow(requirement_id="r_01", grade="direct",
                                 evidence_fact_ids=["f_999"], rationale="ghost"),
                    ScorecardRow(requirement_id="r_02", grade="direct",
                                 evidence_fact_ids=["f_002"], rationale="EKS"),
                    ScorecardRow(requirement_id="r_03", grade="direct",
                                 evidence_fact_ids=["f_001"], rationale="Kafka"),
                    ScorecardRow(requirement_id="r_04", grade="direct",
                                 evidence_fact_ids=["f_001"], rationale="Go"),
                ]
            )
        return original(schema, **kwargs)

    monkeypatch.setattr("app.nodes.matching.complete_json", citing_ghost)
    final = _run(tmp_path)

    row = final["scorecard"].row_for("r_01")
    assert row.evidence_fact_ids == []
    assert row.grade == "none"
    assert final["scorecard"].verdict == "not_matching"
    scorecard_holder.clear()

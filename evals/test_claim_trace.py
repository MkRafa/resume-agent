"""Deterministic claim tracing.

The counterpart to the LLM verifier, and deliberately built to fail differently:
these checks are arithmetic and set membership, so a fabrication that both
models find plausible still gets caught here.
"""

from __future__ import annotations

from app.schemas import Bullet, CareerGraph, ExperienceBlock, FactAtom, Identity, Metric, TailoredResume
from app.tools.claim_trace import summarise, trace_claims


def graph(*atoms: FactAtom) -> CareerGraph:
    return CareerGraph(
        identity=Identity(primary_key="a@b.co", keys=["a@b.co"], email="a@b.co"),
        atoms=list(atoms),
    )


def atom(raw: str, **kw) -> FactAtom:
    kw.setdefault("id", "f_001")
    kw.setdefault("type", "achievement")
    return FactAtom(raw_text=raw, **kw)


def resume(text: str, fact_ids: list[str]) -> TailoredResume:
    return TailoredResume(
        experience=[
            ExperienceBlock(
                company="Northwind", role="Senior Backend Engineer",
                bullets=[Bullet(text=text, fact_ids=fact_ids)],
            )
        ]
    )


def kinds(problems) -> set[str]:
    return {p.kind for p in problems}


def test_faithful_bullet_is_clean():
    g = graph(atom("Cut p99 settlement latency from 1.8s to 640ms using a Kafka queue."))
    r = resume("Cut p99 settlement latency from 1.8s to 640ms with a Kafka queue.", ["f_001"])
    assert trace_claims(r, g) == []


def test_orphan_bullet_is_caught():
    g = graph(atom("Did some work."))
    assert kinds(trace_claims(resume("Did some work.", []), g)) == {"orphan_bullet"}


def test_dangling_citation_is_caught():
    g = graph(atom("Did some work."))
    assert "dangling_citation" in kinds(trace_claims(resume("Did work.", ["f_999"]), g))


def test_fabricated_metric_is_caught():
    """The highest-value check — a fabricated number is the most damaging and
    most checkable thing a resume can contain."""
    g = graph(atom("Improved the checkout flow's load time."))
    problems = trace_claims(resume("Improved checkout load time by 45%.", ["f_001"]), g)
    assert "unsourced_number" in kinds(problems)
    assert any(p.token.startswith("45") for p in problems)


def test_inflated_metric_is_caught():
    g = graph(atom("Reduced integration incidents by roughly 60%."))
    assert "unsourced_number" in kinds(
        trace_claims(resume("Reduced integration incidents by 78%.", ["f_001"]), g)
    )


def test_derived_percentage_is_allowed():
    """1.8s -> 640ms genuinely supports 'cut latency 64%'. Arithmetic on stated
    figures is not invention."""
    g = graph(atom("Cut p99 settlement latency from 1.8s to 640ms."))
    assert trace_claims(resume("Cut p99 latency 64% (1.8s to 640ms).", ["f_001"]), g) == []


def test_derived_percentage_from_minutes():
    g = graph(atom("Took a nightly report from 45 min to 4 min."))
    assert trace_claims(resume("Cut nightly report runtime 91%.", ["f_001"]), g) == []


def test_metrics_field_counts_as_a_source():
    g = graph(atom("Cut settlement latency.", metrics=[Metric(name="p99", delta="-40%")]))
    assert trace_claims(resume("Cut settlement latency 40%.", ["f_001"]), g) == []


def test_years_are_not_treated_as_claims():
    g = graph(atom("Joined Northwind.", start="2022-03"))
    assert trace_claims(resume("Joined Northwind in 2022.", ["f_001"]), g) == []


def test_invented_technology_is_caught():
    g = graph(atom("Provisioned infrastructure with CloudFormation."))
    problems = trace_claims(resume("Provisioned infrastructure with Terraform.", ["f_001"]), g)
    assert "unsourced_technology" in kinds(problems)


def test_specific_to_general_technology_is_allowed():
    """EKS -> Kubernetes is accurate writing, and must not register as a
    hallucination here any more than it does in the verifier."""
    g = graph(atom("Migrated 14 services from EC2 to EKS."))
    assert trace_claims(resume("Migrated 14 services to Kubernetes.", ["f_001"]), g) == []


def test_skills_on_the_atom_count_as_a_source():
    g = graph(atom("Built the ingestion platform.", skills=["spark", "airflow"]))
    assert trace_claims(resume("Built the ingestion platform on Spark and Airflow.", ["f_001"]), g) == []


def test_multiple_cited_atoms_pool_their_sources():
    g = graph(
        atom("Built the route API in FastAPI serving 40k requests/day.", id="f_001"),
        atom("Wrote the runbooks for shipment and billing.", id="f_002"),
    )
    r = resume("Built the FastAPI route API (40k req/day) and authored its runbooks.",
               ["f_001", "f_002"])
    assert trace_claims(r, g) == []


def test_summarise_reports_clean_rate():
    g = graph(atom("Did work.", id="f_001"))
    r = TailoredResume(experience=[ExperienceBlock(company="X", role="Y", bullets=[
        Bullet(text="Did work.", fact_ids=["f_001"]),
        Bullet(text="Grew revenue 300%.", fact_ids=["f_001"]),
    ])])
    s = summarise(trace_claims(r, g), r)
    assert s["bullets"] == 2 and s["affected_bullets"] == 1 and s["clean_rate"] == 0.5


# --- Coverage beyond experience bullets -----------------------------------
# Regression: only experience bullets used to be traced. A project bullet with
# an invented language and metrics, and a summary inflating years, team size
# and stack, both came back clean.


def test_fabricated_project_bullet_is_caught():
    g = graph(atom("Side project: a CLI for budgeting.", type="project"))
    r = TailoredResume(projects=[Bullet(
        text="Built a Rust budgeting CLI used by 5,000 people with 40% retention.",
        fact_ids=["f_001"],
    )])
    problems = trace_claims(r, g)
    assert {p.token for p in problems} >= {"rust", "5000", "40%"}
    assert all(p.location == "projects#0" for p in problems)


def test_fabricated_summary_is_caught():
    g = graph(atom("Led the checkout redesign.", company="Acme", role="PM"))
    r = TailoredResume(
        summary="PM with 12 years leading 30-person teams on Kubernetes.",
        summary_fact_ids=["f_001"],
    )
    problems = trace_claims(r, g, years=4.5)
    assert {p.token for p in problems} == {"12", "30", "kubernetes"}
    assert {p.section for p in problems} == {"summary"}


def test_summary_may_state_the_computed_years():
    g = graph(atom("Owned the settlement service.", company="Northwind"))
    r = TailoredResume(summary="Backend engineer with 6+ years in payments.",
                       summary_fact_ids=["f_001"])
    assert trace_claims(r, g, years=6.4) == []


def test_uncited_summary_is_an_orphan():
    g = graph(atom("Did work."))
    r = TailoredResume(summary="Seasoned engineer.")
    assert kinds(trace_claims(r, g)) == {"orphan_bullet"}


def test_skills_list_cannot_add_a_technology():
    g = graph(atom("Built services in Python.", skills=["python", "postgres"]))
    r = TailoredResume(skills=["Python", "PostgreSQL", "Kafka", "SQL"])
    problems = trace_claims(r, g)
    # Kafka is invented; SQL is the general form of Postgres, which is fine.
    assert [p.token for p in problems] == ["kafka"]
    assert problems[0].section == "skills"


def test_education_year_is_checked():
    g = graph(atom("B.E. Computer Science, R.V. College of Engineering, 2019", type="education"))
    clean = TailoredResume(education=["B.E. Computer Science, RVCE, 2019"])
    assert trace_claims(clean, g) == []
    wrong = TailoredResume(education=["B.E. Computer Science, RVCE, 2017"])
    assert [p.token for p in trace_claims(wrong, g)] == ["2017"]


def _block(role: str, company: str = "Northwind Payments", start: str | None = "2022-03",
           end: str | None = "present") -> TailoredResume:
    return TailoredResume(experience=[ExperienceBlock(
        company=company, role=role, start=start, end=end,
        bullets=[Bullet(text="Owned the settlement service.", fact_ids=["f_001"])],
    )])


HEADER_ATOM = atom("Owned the settlement service.", company="Northwind Payments",
                   role="Senior Backend Engineer", start="2022-03", end="present")


def test_faithful_header_is_clean():
    assert trace_claims(_block("Senior Backend Engineer"), graph(HEADER_ATOM)) == []
    # Weaker title, abbreviation, and a shortened company name are all fine.
    assert trace_claims(_block("Backend Engineer"), graph(HEADER_ATOM)) == []
    assert trace_claims(_block("Sr. Backend Engineer", company="Northwind"), graph(HEADER_ATOM)) == []
    # Same month written differently.
    assert trace_claims(_block("Senior Backend Engineer", start="Mar 2022"), graph(HEADER_ATOM)) == []


def test_title_inflation_is_caught():
    problems = trace_claims(_block("Staff Backend Engineer"), graph(HEADER_ATOM))
    assert [(p.kind, p.token) for p in problems] == [("unsourced_header", "role: Staff Backend Engineer")]


def test_shifted_dates_and_wrong_company_are_caught():
    problems = trace_claims(_block("Senior Backend Engineer", company="Stripe", start="2021-01"),
                            graph(HEADER_ATOM))
    assert {p.token for p in problems} == {"company: Stripe", "start: 2021-01"}


# --- Tool names that are also English words --------------------------------


def test_english_words_are_not_technologies():
    """Regression: 'go' in 'go-to-market' registered as an invented language."""
    g = graph(atom("Launched the checkout redesign with the design team."))
    r = resume("Owned the go-to-market plan and made checkout go live; a spark of growth.",
               ["f_001"])
    assert trace_claims(r, g) == []


def test_proper_noun_language_is_still_checked():
    g = graph(atom("Built the ledger service in Python."))
    assert [p.token for p in trace_claims(resume("Built the ledger service in Go.", ["f_001"]), g)] == ["go"]


def test_summarise_counts_other_sections_separately():
    g = graph(atom("Did work.", id="f_001"))
    r = TailoredResume(
        summary="Grew revenue 300%.", summary_fact_ids=["f_001"],
        experience=[ExperienceBlock(company="X", role="Y", bullets=[
            Bullet(text="Did work.", fact_ids=["f_001"])])],
    )
    s = summarise(trace_claims(r, g), r)
    assert s["clean_rate"] == 1.0 and s["other_sections_affected"] == ["summary"]


# --- Qualified skills ------------------------------------------------------
# Regression from a live run: the candidate listed "Go (basic)", nothing on
# their resume demonstrates Go, and the tailored skills line opened with a bare
# "Go" - the exact oversell tailor.md forbids. Go is in the graph, so the
# sourcing check passed it, and the verifier did not flag it either.

SKILLS_ATOM = atom("Python, Go (basic), FastAPI, Postgres, familiar with Terraform",
                   id="f_010", type="skill")
WORK_ATOM = atom("Built the route API in Python/FastAPI.", id="f_001")


def test_unqualified_basic_skill_is_flagged():
    r = TailoredResume(skills=["Go", "Kafka", "Python"])
    problems = trace_claims(r, graph(WORK_ATOM, SKILLS_ATOM))
    assert [(p.kind, p.token) for p in problems if p.kind == "overstated_skill"] == [
        ("overstated_skill", "Go (candidate: basic)")
    ]


def test_qualified_skill_listed_honestly_and_last_is_fine():
    r = TailoredResume(skills=["Python", "FastAPI", "Go (basic)", "Terraform (familiar)"])
    assert trace_claims(r, graph(WORK_ATOM, SKILLS_ATOM)) == []


def test_qualified_skill_may_not_lead_even_with_its_qualifier():
    r = TailoredResume(skills=["Go (basic)", "Python"])
    assert [p.token for p in trace_claims(r, graph(WORK_ATOM, SKILLS_ATOM))] == ["Go leads the list"]


def test_qualifier_before_the_skill_counts_too():
    r = TailoredResume(skills=["Python", "Terraform"])
    assert [p.token for p in trace_claims(r, graph(WORK_ATOM, SKILLS_ATOM))] == [
        "Terraform (candidate: familiar with)"
    ]


def test_demonstrated_skill_is_not_held_to_its_qualifier():
    """If an achievement shows the skill in use, the modest self-rating in the
    skills list does not bind the resume."""
    shipped = atom("Rewrote the ledger service in Go, cutting p99 by half.", id="f_002")
    r = TailoredResume(skills=["Go", "Python"])
    assert trace_claims(r, graph(WORK_ATOM, shipped, SKILLS_ATOM)) == []

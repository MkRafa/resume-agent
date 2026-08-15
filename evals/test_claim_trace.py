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

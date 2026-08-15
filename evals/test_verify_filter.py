"""The deterministic softener for known verifier false positives.

It may only ever downgrade blocker -> warning. Escalating, or suppressing a
genuine fabrication, would defeat the guardrail it exists to protect.
"""

from __future__ import annotations

import pytest

from app.schemas import CareerGraph, FactAtom, Identity, VerifyFlag, VerifyReport
from app.tools.verify_filter import soften_known_false_positives


def graph(*atoms: FactAtom) -> CareerGraph:
    return CareerGraph(
        identity=Identity(primary_key="a@b.co", keys=["a@b.co"], email="a@b.co"),
        atoms=list(atoms),
    )


def flag(claim: str, issue: str, severity: str = "blocker") -> VerifyFlag:
    return VerifyFlag(
        claim=claim, location="experience[0].bullets[0]", issue=issue,
        severity=severity, explanation="model said so",
    )


def atom(raw: str, **kw) -> FactAtom:
    kw.setdefault("id", "f_001")
    kw.setdefault("type", "achievement")
    return FactAtom(raw_text=raw, **kw)


@pytest.mark.parametrize(
    "source,claim",
    [
        ("Migrated 14 services from EC2 to EKS.", "production Kubernetes"),
        ("Ran GKE clusters for 12 teams.", "Kubernetes at scale"),
        ("Built a Kafka-backed write-behind queue.", "event-driven architecture"),
        ("Added Postgres partitioning to the events table.", "relational databases"),
        ("Built the route API in FastAPI.", "Python services"),
    ],
)
def test_accurate_generalisation_is_softened(source, claim):
    report = VerifyReport(flags=[flag(claim, "invented_technology")])
    soften_known_false_positives(report, graph(atom(source)))
    assert report.flags[0].severity == "warning"
    assert report.clean, "a softened flag must no longer block the render"


def test_genuine_invented_technology_still_blocks():
    """The softener must not rescue a tool the candidate never used."""
    report = VerifyReport(flags=[flag("Terraform modules", "invented_technology")])
    soften_known_false_positives(report, graph(atom("Provisioned infra with CloudFormation.")))
    assert report.flags[0].severity == "blocker"
    assert not report.clean


def test_general_to_specific_still_blocks():
    """Atom says CI/CD, resume says GitHub Actions — the disallowed direction."""
    report = VerifyReport(flags=[flag("GitHub Actions", "invented_technology")])
    soften_known_false_positives(report, graph(atom("Built the team's CI/CD pipelines.")))
    assert report.flags[0].severity == "blocker"


def test_own_project_authorship_is_softened():
    report = VerifyReport(flags=[flag("Created ledger-lint", "overstated_ownership")])
    g = graph(atom("ledger-lint - open-source static analyser. 400+ stars.",
                   id="f_013", type="project"))
    soften_known_false_positives(report, g)
    assert report.flags[0].severity == "warning"


def test_contributed_project_authorship_still_blocks():
    """If the atom says the candidate only contributed, 'Created' is still a lie."""
    report = VerifyReport(flags=[flag("Created ledger-lint", "overstated_ownership")])
    g = graph(atom("Contributed to ledger-lint, an open-source analyser.",
                   id="f_013", type="project", evidence_strength="contributed"))
    soften_known_false_positives(report, g)
    assert report.flags[0].severity == "blocker"


def test_employment_ownership_inflation_still_blocks():
    """Softening applies to `project` atoms only — never to job achievements."""
    report = VerifyReport(flags=[flag("Led the migration", "overstated_ownership")])
    g = graph(atom("Helped migrate the shipment module.", evidence_strength="contributed"))
    soften_known_false_positives(report, g)
    assert report.flags[0].severity == "blocker"


def test_fabricated_metrics_are_never_softened():
    report = VerifyReport(flags=[flag("improved latency 45%", "inflated_metric")])
    soften_known_false_positives(report, graph(atom("Improved checkout load time.")))
    assert report.flags[0].severity == "blocker"


def test_unsupported_claims_are_never_softened():
    report = VerifyReport(flags=[flag("PCI-DSS certification", "unsupported_claim")])
    soften_known_false_positives(report, graph(atom("Built a Kafka CDC pipeline.")))
    assert report.flags[0].severity == "blocker"


def test_warnings_are_never_escalated():
    report = VerifyReport(flags=[flag("Kubernetes", "invented_technology", severity="warning")])
    soften_known_false_positives(report, graph(atom("Migrated to EKS.")))
    assert report.flags[0].severity == "warning"


def test_empty_report_is_safe():
    report = VerifyReport(flags=[])
    soften_known_false_positives(report, graph(atom("Anything.")))
    assert report.clean

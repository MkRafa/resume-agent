"""Deterministic softening of known verifier false positives.

Two false-positive classes survived three revisions of verify.md even though
the prompt names them with the exact example. At that point the honest read is
that the model will not reliably follow the rule, and the project's standing
answer applies: anything checkable in Python should be checked in Python.

This does NOT delete flags — it downgrades `blocker` to `warning`. The claim
still reaches the user; it just stops withholding a truthful resume. Suppressing
outright would risk hiding a real fabrication that happens to pattern-match.
Nothing here can ever raise a severity.
"""

from __future__ import annotations

import re

from app.schemas import CareerGraph, VerifyReport

# Managed services that ARE the general technology. Naming the general form is
# accurate writing, not invention (the resume says "Kubernetes"; the atom says
# "EKS"). Keys are what an atom might say, values what a resume may call it.
TECH_GENERALISATIONS: dict[str, set[str]] = {
    "eks": {"kubernetes", "k8s"},
    "aks": {"kubernetes", "k8s"},
    "gke": {"kubernetes", "k8s"},
    "openshift": {"kubernetes", "k8s"},
    "kafka": {"event-driven", "event driven", "event streaming", "message queue", "pub/sub"},
    "kinesis": {"event-driven", "event driven", "event streaming"},
    "rabbitmq": {"event-driven", "event driven", "message queue"},
    "postgres": {"relational database", "relational databases", "sql", "rdbms"},
    "postgresql": {"relational database", "relational databases", "sql", "rdbms"},
    "mysql": {"relational database", "relational databases", "sql", "rdbms"},
    "fastapi": {"python", "rest api", "rest apis"},
    "django": {"python", "web framework"},
    "flask": {"python", "rest api", "rest apis"},
    "react": {"javascript", "frontend", "typescript"},
    "spark": {"distributed computing", "big data"},
    "airflow": {"workflow orchestration", "orchestration"},
    "terraform": {"infrastructure-as-code", "infrastructure as code", "iac"},
    "cloudformation": {"infrastructure-as-code", "infrastructure as code", "iac"},
    "pytorch": {"deep learning", "machine learning"},
}


def _normalise(text: str) -> str:
    return re.sub(r"[^a-z0-9+#./ -]", " ", text.lower())


def _atom_text(graph: CareerGraph) -> str:
    parts = []
    for atom in graph.atoms:
        parts.append(atom.raw_text)
        parts.extend(atom.skills)
    return _normalise(" ".join(parts))


def _is_accurate_generalisation(claim: str, corpus: str) -> bool:
    """True when the flagged term is the general form of something in the atoms."""
    claim_l = _normalise(claim)
    for specific, generals in TECH_GENERALISATIONS.items():
        if not re.search(rf"(?<![a-z0-9]){re.escape(specific)}(?![a-z0-9])", corpus):
            continue
        if any(g in claim_l for g in generals):
            return True
    return False


def _is_own_project_authorship(claim: str, graph: CareerGraph) -> bool:
    """True when the claim asserts authorship of the candidate's own project.

    A `project` atom on someone's own resume means they built it — unless the
    atom itself says otherwise, which `evidence_strength` records.
    """
    if not re.search(r"\b(created|built|wrote|authored|developed)\b", claim, re.IGNORECASE):
        return False
    claim_l = _normalise(claim)
    for atom in graph.atoms:
        if atom.type != "project" or atom.evidence_strength in {"contributed", "assisted"}:
            continue
        # Project names are usually a distinctive token in the atom's text.
        for token in re.findall(r"[a-z0-9][a-z0-9._-]{3,}", _normalise(atom.raw_text)):
            if token in claim_l:
                return True
    return False


def soften_known_false_positives(report: VerifyReport, graph: CareerGraph) -> VerifyReport:
    """Downgrade blockers that match a checkable allowed transformation."""
    corpus = _atom_text(graph)

    for flag in report.flags:
        if flag.severity != "blocker":
            continue

        if flag.issue == "invented_technology" and _is_accurate_generalisation(flag.claim, corpus):
            flag.severity = "warning"
            flag.explanation = (
                f"[softened] The source names a specific instance of this; calling it by the "
                f"general name is accurate. {flag.explanation}"
            )
        elif flag.issue == "overstated_ownership" and _is_own_project_authorship(flag.claim, graph):
            flag.severity = "warning"
            flag.explanation = (
                f"[softened] This is the candidate's own listed project, which implies "
                f"authorship. {flag.explanation}"
            )
    return report

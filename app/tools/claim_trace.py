"""Deterministic claim tracing: can every assertion on a resume be sourced?

This is the counterpart to the LLM verifier, and deliberately shares nothing
with it. Using a model to check a model's output is circular — they share
training, blind spots and failure modes, and a fabrication both find plausible
sails through. These checks are arithmetic and set membership, so they fail
differently.

Four things are checked per bullet:

  orphan_bullet        no fact_ids at all — nothing to trace back to
  dangling_citation    cites an atom id that does not exist
  unsourced_number     a figure that appears in neither the cited atoms nor a
                       derivation from them
  unsourced_technology a named tool absent from the cited atoms (allowing the
                       specific->general forms in verify_filter)

Numbers are the highest-signal check: a fabricated metric is the most damaging
and most checkable thing a resume can contain.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from app.schemas import CareerGraph, TailoredResume
from app.tools.verify_filter import TECH_GENERALISATIONS

UntracedKind = Literal[
    "orphan_bullet", "dangling_citation", "unsourced_number", "unsourced_technology"
]

# Tokens we treat as technology names. Union of the generalisation map plus
# common tools; anything unknown is ignored rather than guessed at, so this
# check under-reports rather than crying wolf.
KNOWN_TECH: set[str] = {
    *TECH_GENERALISATIONS.keys(),
    *(g for gs in TECH_GENERALISATIONS.values() for g in gs),
    "kubernetes", "docker", "redis", "grpc", "graphql", "snowflake", "redshift",
    "bigquery", "dbt", "pact", "prometheus", "grafana", "argocd", "gitlab",
    "github actions", "jenkins", "aws", "gcp", "azure", "go", "rust", "java",
    "typescript", "javascript", "next.js", "tailwind", "playwright", "jest",
    "pytorch", "tensorflow", "huggingface", "langchain", "mlflow", "kubeflow",
    "sagemaker", "burp suite", "sast", "dast", "pci-dss", "terraform", "ansible",
    "chef", "puppet", "kafka", "rabbitmq", "postgres", "postgresql", "mysql",
    "mongodb", "elasticsearch", "spark", "airflow", "dagster", "fastapi",
}

# Figures a resume states that are not claims about achievement.
_YEAR = re.compile(r"^(19|20)\d{2}$")
# The unit must be ADJACENT to the digits. An earlier version allowed \s*, so
# "p99 settlement" tokenised as "99s" while "p99 latency" gave "99" — the same
# figure read differently on each side and registered as unsourced. Longer units
# come first so "640ms" does not match "640m" and strand the "s".
_NUM = re.compile(
    r"\d+(?:\.\d+)?(?:percent|min|bn|ms|hr|tb|gb|mb|%|k|m|b|x|s|h)?(?![a-z0-9])", re.I
)


@dataclass
class Untraced:
    location: str
    kind: UntracedKind
    token: str
    text: str

    def __str__(self) -> str:
        return f"[{self.kind}] {self.token!r} in {self.location}"


def _norm_num(raw: str) -> str:
    return re.sub(r"[\s,]", "", raw.lower())


def _numbers(text: str) -> list[str]:
    return [_norm_num(m.group(0)) for m in _NUM.finditer(text)]


def _bare(value: str) -> float | None:
    m = re.match(r"^(\d+(?:\.\d+)?)", value)
    return float(m.group(1)) if m else None


def _derivable(target: str, sources: list[str]) -> bool:
    """True if `target` is a percentage change between two source figures.

    "1.8s to 640ms" legitimately supports "cut latency 64%" — deriving a figure
    from stated ones is arithmetic, not invention.
    """
    if "%" not in target:
        return False
    want = _bare(target)
    if want is None:
        return False
    vals = [v for v in (_bare(s) for s in sources) if v]
    for a in vals:
        for b in vals:
            if a <= 0 or a == b:
                continue
            for scale in (1.0, 1000.0):  # tolerate unit changes like s -> ms
                for x, y in ((a, b * scale), (a * scale, b)):
                    if x <= 0:
                        continue
                    if abs(abs(1 - y / x) * 100 - want) <= 1.5:
                        return True
    return False


def _sourced_tech(corpus: str) -> set[str]:
    found = {t for t in KNOWN_TECH if re.search(rf"(?<![a-z0-9]){re.escape(t)}(?![a-z0-9])", corpus)}
    for specific, generals in TECH_GENERALISATIONS.items():
        if specific in found:
            found |= generals
    return found


def trace_claims(resume: TailoredResume, graph: CareerGraph) -> list[Untraced]:
    """Every claim that cannot be traced to the atoms a bullet cites."""
    problems: list[Untraced] = []

    for block in resume.experience:
        for i, bullet in enumerate(block.bullets):
            loc = f"{block.company}/{block.role}#{i}"

            if not bullet.fact_ids:
                problems.append(Untraced(loc, "orphan_bullet", bullet.text[:60], bullet.text))
                continue

            atoms = []
            for fid in bullet.fact_ids:
                atom = graph.by_id(fid)
                if atom is None:
                    problems.append(Untraced(loc, "dangling_citation", fid, bullet.text))
                else:
                    atoms.append(atom)
            if not atoms:
                continue

            corpus = " ".join(
                [a.raw_text for a in atoms]
                + [s for a in atoms for s in a.skills]
                + [
                    f"{m.value or ''} {m.delta or ''}"
                    for a in atoms
                    for m in a.metrics
                ]
                + [a.scope.users_served or "" for a in atoms]
                + [str(a.scope.team_size or "") for a in atoms]
            ).lower()

            source_nums = _numbers(corpus)
            for num in _numbers(bullet.text):
                if _YEAR.match(num) or num in source_nums:
                    continue
                if _derivable(num, source_nums):
                    continue
                problems.append(Untraced(loc, "unsourced_number", num, bullet.text))

            sourced = _sourced_tech(corpus)
            for tech in _sourced_tech(bullet.text.lower()):
                if tech not in sourced:
                    problems.append(Untraced(loc, "unsourced_technology", tech, bullet.text))

    return problems


def summarise(problems: list[Untraced], resume: TailoredResume) -> dict:
    total = len(resume.all_bullets())
    by_kind: dict[str, int] = {}
    for p in problems:
        by_kind[p.kind] = by_kind.get(p.kind, 0) + 1
    affected = len({p.location for p in problems})
    return {
        "bullets": total,
        "affected_bullets": affected,
        "clean_rate": round(1 - affected / total, 3) if total else 1.0,
        "by_kind": by_kind,
    }

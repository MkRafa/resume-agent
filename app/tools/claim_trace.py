"""Deterministic claim tracing: can every assertion on a resume be sourced?

This is the counterpart to the LLM verifier, and deliberately shares nothing
with it. Using a model to check a model's output is circular — they share
training, blind spots and failure modes, and a fabrication both find plausible
sails through. These checks are arithmetic and set membership, so they fail
differently.

Every section of the resume is traced, not just experience bullets - a
fabricated metric in the summary or a project is just as damaging:

  orphan_bullet        a bullet (or the summary) cites no facts at all
  dangling_citation    cites an atom id that does not exist
  unsourced_number     a figure that appears in neither the cited atoms nor a
                       derivation from them
  unsourced_technology a named tool absent from the cited atoms (allowing the
                       specific->general forms in verify_filter)
  unsourced_header     a role title, company or date range under Experience
                       that none of the block's cited atoms carry - title
                       inflation ("Senior" -> "Staff") lives here
  overstated_skill     a skill the candidate qualified ("Go (basic)",
                       "familiar with Terraform") and nothing demonstrates,
                       listed without its qualifier or leading the list

Sections: experience and project bullets against their own citations; the
summary against `summary_fact_ids`; the skills list and education lines
against the whole graph, since they carry no citations.

Numbers are the highest-signal check: a fabricated metric is the most damaging
and most checkable thing a resume can contain.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from app.schemas import CareerGraph, FactAtom, TailoredResume
from app.tools.dates import parse_month
from app.tools.verify_filter import TECH_GENERALISATIONS

UntracedKind = Literal[
    "orphan_bullet",
    "dangling_citation",
    "unsourced_number",
    "unsourced_technology",
    "unsourced_header",
    "overstated_skill",
]
Section = Literal["bullet", "summary", "skills", "education", "header"]

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

# Tool names that are also ordinary English. On the claim side they only count
# as a technology when written as a proper noun and not as part of a compound:
# "Services in Go" names the language, "go-to-market" and "go live" do not.
AMBIGUOUS_TECH = {"go", "chef", "spark", "jest", "puppet", "rust", "pact"}

# Figures a resume states that are not claims about achievement.
_YEAR = re.compile(r"^(19|20)\d{2}$")
# The unit must be ADJACENT to the digits. An earlier version allowed \s*, so
# "p99 settlement" tokenised as "99s" while "p99 latency" gave "99" — the same
# figure read differently on each side and registered as unsourced. Longer units
# come first so "640ms" does not match "640m" and strand the "s". Thousands
# separators are part of the number ("5,000" is one figure, normalised to
# "5000"), or a fabricated count reads as two meaningless tokens.
_NUM = re.compile(
    r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
    r"(?:percent|min|bn|ms|hr|tb|gb|mb|%|k|m|b|x|s|h)?(?![a-z0-9])",
    re.I,
)

# A candidate's own statement of limited proficiency. Written after the skill
# ("Go (basic)") or before it ("familiar with Terraform").
_QUALIFIERS = r"basic|beginner|learning|familiar|exposure|limited|novice|elementary"
_QUALIFIED_AFTER = re.compile(
    rf"([A-Za-z][\w.+#-]*)\s*\(\s*((?:{_QUALIFIERS})[^)]*)\)", re.I
)
_QUALIFIED_BEFORE = re.compile(
    r"\b(familiar with|exposure to|basic knowledge of|learning|beginner in)\s+([A-Za-z][\w.+#-]*)",
    re.I,
)

_ROLE_ABBREVIATIONS = {"sr": "senior", "jr": "junior", "eng": "engineer", "mgr": "manager"}


@dataclass
class Untraced:
    location: str
    kind: UntracedKind
    token: str
    text: str
    section: Section = "bullet"

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
    """Technologies a source text supports - read leniently, including every
    general form of a specific tool it names."""
    found = {t for t in KNOWN_TECH if re.search(rf"(?<![a-z0-9]){re.escape(t)}(?![a-z0-9])", corpus)}
    for specific, generals in TECH_GENERALISATIONS.items():
        if specific in found:
            found |= generals
    return found


def _claimed_tech(text: str) -> set[str]:
    """Technologies a resume line asserts - read strictly, so an ordinary word
    that happens to share a tool's name is not reported as an invention."""
    low = text.lower()
    found: set[str] = set()
    for t in KNOWN_TECH:
        if t in AMBIGUOUS_TECH:
            if re.search(rf"(?<![A-Za-z0-9]){t.capitalize()}(?![A-Za-z0-9-])(?! live)", text):
                found.add(t)
        elif re.search(rf"(?<![a-z0-9]){re.escape(t)}(?![a-z0-9])", low):
            found.add(t)
    return found


def _corpus(
    atoms: list[FactAtom], *, headline: str | None = None, dates: bool = False
) -> str:
    """Source text for a set of atoms. Dates are opt-in: "2019-12" in the
    corpus would otherwise source a fabricated "12 services" in a bullet."""
    return " ".join(
        [a.raw_text for a in atoms]
        + [s for a in atoms for s in a.skills]
        + [f"{m.value or ''} {m.delta or ''}" for a in atoms for m in a.metrics]
        + [a.scope.users_served or "" for a in atoms]
        + [str(a.scope.team_size or "") for a in atoms]
        + ([f"{a.start or ''} {a.end or ''}" for a in atoms] if dates else [])
        + [headline or ""]
    ).lower()


def _check_text(
    problems: list[Untraced],
    *,
    loc: str,
    section: Section,
    text: str,
    corpus: str,
    extra_numbers: set[str] = frozenset(),  # type: ignore[assignment]
    years_are_claims: bool = False,
) -> None:
    source_nums = _numbers(corpus)
    for num in _numbers(text):
        if not years_are_claims and _YEAR.match(num):
            continue
        if num in source_nums or num in extra_numbers or _derivable(num, source_nums):
            continue
        problems.append(Untraced(loc, "unsourced_number", num, text, section))

    sourced = _sourced_tech(corpus)
    for tech in sorted(_claimed_tech(text) - sourced):
        problems.append(Untraced(loc, "unsourced_technology", tech, text, section))


def _cited(
    problems: list[Untraced], *, loc: str, section: Section, text: str,
    fact_ids: list[str], graph: CareerGraph,
) -> list[FactAtom]:
    """Resolve citations, recording orphans and dangling ids along the way."""
    if not fact_ids:
        problems.append(Untraced(loc, "orphan_bullet", text[:60], text, section))
        return []
    atoms = []
    for fid in fact_ids:
        atom = graph.by_id(fid)
        if atom is None:
            problems.append(Untraced(loc, "dangling_citation", fid, text, section))
        else:
            atoms.append(atom)
    return atoms


def _mentions(term: str, text: str) -> bool:
    """Whole-word mention; English-word tool names only as a proper noun."""
    if term.lower() in AMBIGUOUS_TECH:
        return bool(re.search(rf"(?<![A-Za-z0-9]){term.capitalize()}(?![A-Za-z0-9-])", text))
    return bool(re.search(rf"(?<![a-z0-9]){re.escape(term.lower())}(?![a-z0-9])", text.lower()))


def _qualified_skills(graph: CareerGraph) -> dict[str, str]:
    """Skills the candidate qualified that no achievement demonstrates.

    Returns {skill: qualifier}. The qualified mentions themselves are cut out
    before looking for a demonstration, so "Go (basic)" cannot vouch for Go.
    """
    qualified: dict[str, str] = {}
    for atom in graph.atoms:
        for text in (atom.raw_text, *atom.skills):
            for m in _QUALIFIED_AFTER.finditer(text):
                qualified.setdefault(m.group(1), m.group(2).strip())
            for m in _QUALIFIED_BEFORE.finditer(text):
                qualified.setdefault(m.group(2), m.group(1).lower())
    if not qualified:
        return {}

    evidence = " ".join(
        text
        for a in graph.atoms
        if a.type in {"achievement", "responsibility", "project"}
        for text in (a.raw_text, *a.skills)
    )
    evidence = _QUALIFIED_BEFORE.sub(" ", _QUALIFIED_AFTER.sub(" ", evidence))
    return {s: q for s, q in qualified.items() if not _mentions(s, evidence)}


def _check_skills(problems: list[Untraced], resume: TailoredResume, graph: CareerGraph) -> None:
    """tailor.md: a qualified skill is dropped or listed last, never presented
    as a strength. Leading the list with one is the most-read oversell on a
    resume - it is where the interviewer starts."""
    for skill, qualifier in _qualified_skills(graph).items():
        for i, entry in enumerate(resume.skills):
            if not _mentions(skill, entry):
                continue
            first = qualifier.split()[0].lower()
            if first not in entry.lower():
                problems.append(Untraced(
                    "skills", "overstated_skill", f"{skill} (candidate: {qualifier})", entry, "skills"
                ))
            elif i == 0:
                problems.append(Untraced(
                    "skills", "overstated_skill", f"{skill} leads the list", entry, "skills"
                ))


def _norm_words(text: str) -> list[str]:
    words = re.findall(r"[a-z0-9+#]+", text.lower())
    return [_ROLE_ABBREVIATIONS.get(w, w) for w in words]


def _check_header(problems: list[Untraced], block, atoms: list[FactAtom]) -> None:
    """The role line must be one the cited atoms actually carry.

    Only atoms that state a company are comparable. Role words must be a subset
    of an atom's role, so a weaker title ("Backend Engineer" for "Senior
    Backend Engineer") passes and an inflated one does not.
    """
    placed = [a for a in atoms if a.company]
    if not placed:
        return
    loc = f"{block.company}/{block.role}"
    header = f"{block.role} @ {block.company}"

    company = " ".join(_norm_words(block.company))
    if not any(
        company in (theirs := " ".join(_norm_words(a.company or ""))) or theirs in company
        for a in placed
    ):
        problems.append(Untraced(loc, "unsourced_header", f"company: {block.company}", header, "header"))

    roles = [a for a in placed if a.role]
    if roles and not any(set(_norm_words(block.role)) <= set(_norm_words(a.role or "")) for a in roles):
        problems.append(Untraced(loc, "unsourced_header", f"role: {block.role}", header, "header"))

    for field in ("start", "end"):
        claimed = parse_month(getattr(block, field))
        stated = {parse_month(getattr(a, field)) for a in placed} - {None}
        if claimed and stated and claimed not in stated:
            problems.append(
                Untraced(loc, "unsourced_header", f"{field}: {getattr(block, field)}", header, "header")
            )


def _years_forms(years: float | None) -> set[str]:
    """Ways the computed total-experience figure may legitimately be written."""
    if not years:
        return set()
    return {f"{years:g}", str(int(years)), str(round(years))}


def trace_claims(
    resume: TailoredResume, graph: CareerGraph, *, years: float | None = None
) -> list[Untraced]:
    """Every claim that cannot be traced to the facts it rests on.

    `years` is the computed total experience; the summary may state it even
    though no single atom does.
    """
    problems: list[Untraced] = []

    for block in resume.experience:
        block_atoms: list[FactAtom] = []
        for i, bullet in enumerate(block.bullets):
            loc = f"{block.company}/{block.role}#{i}"
            atoms = _cited(problems, loc=loc, section="bullet", text=bullet.text,
                           fact_ids=bullet.fact_ids, graph=graph)
            block_atoms.extend(atoms)
            if atoms:
                _check_text(problems, loc=loc, section="bullet", text=bullet.text,
                            corpus=_corpus(atoms))
        _check_header(problems, block, block_atoms)

    for j, bullet in enumerate(resume.projects):
        loc = f"projects#{j}"
        atoms = _cited(problems, loc=loc, section="bullet", text=bullet.text,
                       fact_ids=bullet.fact_ids, graph=graph)
        if atoms:
            _check_text(problems, loc=loc, section="bullet", text=bullet.text,
                        corpus=_corpus(atoms))

    if resume.summary:
        atoms = _cited(problems, loc="summary", section="summary", text=resume.summary,
                       fact_ids=resume.summary_fact_ids, graph=graph)
        if atoms:
            _check_text(problems, loc="summary", section="summary", text=resume.summary,
                        corpus=_corpus(atoms, headline=graph.headline),
                        extra_numbers=_years_forms(years))

    # Uncited sections: sourced from anywhere in the graph.
    everything = _corpus(graph.atoms, headline=graph.headline)
    for skill in resume.skills:
        for tech in sorted(_claimed_tech(skill) - _sourced_tech(everything)):
            problems.append(Untraced("skills", "unsourced_technology", tech, skill, "skills"))

    _check_skills(problems, resume, graph)

    schooling = _corpus(
        [a for a in graph.atoms if a.type in {"education", "credential"}] or graph.atoms,
        dates=True,
    )
    for k, line in enumerate(resume.education):
        # A graduation year is a checkable claim here, unlike a date in a bullet.
        _check_text(problems, loc=f"education#{k}", section="education", text=line,
                    corpus=schooling, years_are_claims=True)

    return problems


def summarise(problems: list[Untraced], resume: TailoredResume) -> dict:
    """Clean rate is over bullets; other sections are counted separately so a
    flagged summary or skills line cannot be averaged away across bullets."""
    total = len(resume.all_bullets())
    by_kind: dict[str, int] = {}
    by_section: dict[str, int] = {}
    for p in problems:
        by_kind[p.kind] = by_kind.get(p.kind, 0) + 1
        by_section[p.section] = by_section.get(p.section, 0) + 1
    affected = len({p.location for p in problems if p.section == "bullet"})
    return {
        "bullets": total,
        "affected_bullets": affected,
        "clean_rate": round(1 - affected / total, 3) if total else 1.0,
        "other_sections_affected": sorted({p.section for p in problems if p.section != "bullet"}),
        "by_kind": by_kind,
        "by_section": by_section,
    }

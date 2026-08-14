"""The Career Graph: atomic, provenance-carrying facts about a candidate.

This is the system's core asset. Everything downstream - matching, tailoring,
cover letters - is a projection of these atoms onto a target. Nothing may
appear on a generated resume that does not trace back to an atom id here.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

EvidenceStrength = Literal["led", "contributed", "assisted", "unknown"]
Confidence = Literal["verified", "inferred_from_resume", "user_claimed"]
AtomType = Literal[
    "achievement",
    "responsibility",
    "skill",
    "credential",
    "project",
    "education",
]


class Metric(BaseModel):
    """A quantified outcome. Kept structured so we never re-derive it with an LLM."""

    name: str = Field(description="What was measured, e.g. 'p99_latency'")
    value: str | None = Field(None, description="Absolute value, e.g. '2M'")
    delta: str | None = Field(None, description="Change, e.g. '-40%'")
    unit: str | None = None


class Scope(BaseModel):
    """Signals of blast radius. Drives seniority inference during matching."""

    team_size: int | None = None
    users_served: str | None = None
    budget: str | None = None
    systems_owned: list[str] = Field(default_factory=list)


class FactAtom(BaseModel):
    id: str = Field(description="Stable id, e.g. 'f_014'. Cited by generated bullets.")
    type: AtomType
    raw_text: str = Field(description="The fact as stated by the candidate. Never invented.")

    company: str | None = None
    role: str | None = None
    start: str | None = Field(None, description="YYYY-MM")
    end: str | None = Field(None, description="YYYY-MM, or 'present'")

    skills: list[str] = Field(default_factory=list)
    metrics: list[Metric] = Field(default_factory=list)
    scope: Scope = Field(default_factory=Scope)

    evidence_strength: EvidenceStrength = "unknown"
    confidence: Confidence = "inferred_from_resume"


class Identity(BaseModel):
    """Profile identity. Email is primary, phone is the fallback.

    Both are retained as alternate lookup keys so a later upload that supplies
    only one of them still reconciles to the same profile instead of silently
    forking a second one (and losing the enriched graph).
    """

    primary_key: str
    keys: list[str] = Field(description="All keys that resolve to this profile.")
    email: str | None = None
    phone: str | None = Field(None, description="E.164")


class CareerGraph(BaseModel):
    identity: Identity
    full_name: str | None = None
    location: str | None = None
    links: list[str] = Field(default_factory=list)
    headline: str | None = None
    atoms: list[FactAtom] = Field(default_factory=list)

    def by_id(self, atom_id: str) -> FactAtom | None:
        return next((a for a in self.atoms if a.id == atom_id), None)

    def render_for_prompt(self) -> str:
        """Stable text block. Kept first in every prompt so it can be cache-hit
        across every application this candidate makes."""
        lines: list[str] = []
        for a in self.atoms:
            bits = [f"[{a.id}] ({a.type})"]
            if a.role or a.company:
                bits.append(f"{a.role or '?'} @ {a.company or '?'}")
            if a.start or a.end:
                bits.append(f"{a.start or '?'}..{a.end or '?'}")
            if a.evidence_strength != "unknown":
                bits.append(f"role={a.evidence_strength}")
            head = " | ".join(bits)
            body = a.raw_text
            extra = []
            if a.skills:
                extra.append("skills=" + ",".join(a.skills))
            if a.metrics:
                extra.append(
                    "metrics="
                    + ";".join(
                        f"{m.name}{'=' + m.value if m.value else ''}"
                        f"{'(' + m.delta + ')' if m.delta else ''}"
                        for m in a.metrics
                    )
                )
            if a.scope.team_size:
                extra.append(f"team={a.scope.team_size}")
            if a.scope.users_served:
                extra.append(f"users={a.scope.users_served}")
            lines.append(f"{head}\n    {body}" + (f"\n    {' | '.join(extra)}" if extra else ""))
        return "\n".join(lines)


class ExtractedProfile(BaseModel):
    """LLM output shape for profile extraction. Identity is resolved separately
    in Python - the model reports what it saw, it does not decide the key."""

    full_name: str | None = None
    email: str | None = None
    phone: str | None = None
    location: str | None = None
    links: list[str] = Field(default_factory=list)
    headline: str | None = None
    atoms: list[FactAtom] = Field(default_factory=list)

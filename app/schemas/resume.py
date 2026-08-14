"""Tailored resume, plus the verifier's output.

Every bullet carries the fact atom id it was derived from. That single field is
what makes provenance UI, the verification pass, and the audit log possible.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class Bullet(BaseModel):
    text: str = Field(description="Rewritten in the JD's vocabulary. No new facts.")
    fact_ids: list[str] = Field(
        description="Source atoms. A bullet with no fact_ids is a hallucination."
    )
    targets: list[str] = Field(
        default_factory=list, description="Requirement ids this bullet answers."
    )


class ExperienceBlock(BaseModel):
    company: str
    role: str
    start: str | None = None
    end: str | None = None
    location: str | None = None
    bullets: list[Bullet] = Field(default_factory=list)


class TailoredResume(BaseModel):
    full_name: str | None = None
    contact: dict[str, str] = Field(default_factory=dict)
    summary: str | None = Field(
        None, description="The one bespoke line. Positioning, not new claims."
    )
    summary_fact_ids: list[str] = Field(default_factory=list)
    skills: list[str] = Field(
        default_factory=list, description="Ordered to lead with the JD's stack."
    )
    experience: list[ExperienceBlock] = Field(default_factory=list)
    education: list[str] = Field(default_factory=list)
    projects: list[Bullet] = Field(default_factory=list)

    def all_bullets(self) -> list[Bullet]:
        out = list(self.projects)
        for block in self.experience:
            out.extend(block.bullets)
        return out


class VerifyFlag(BaseModel):
    claim: str = Field(description="The exact span that could not be traced.")
    location: str = Field(description="Where it appears, e.g. 'experience[0].bullets[2]'.")
    issue: Literal[
        "unsupported_claim",
        "inflated_metric",
        "invented_technology",
        "overstated_ownership",
        "date_inconsistency",
    ]
    severity: Literal["blocker", "warning"]
    explanation: str


class VerifyReport(BaseModel):
    flags: list[VerifyFlag] = Field(default_factory=list)

    @property
    def blockers(self) -> list[VerifyFlag]:
        return [f for f in self.flags if f.severity == "blocker"]

    @property
    def clean(self) -> bool:
        return not self.blockers


class SelectedFacts(BaseModel):
    """Selection is a separate, cheaper step than writing. Ranking first means
    the writer only ever sees facts that earned their place."""

    fact_ids: list[str]
    reasoning: str | None = None

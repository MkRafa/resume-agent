"""Structured decomposition of a job description.

Matching never runs against JD prose. It runs against these requirements, so
that every verdict can point at the specific line it turned on.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

RequirementKind = Literal["gate", "must", "nice", "implicit"]
RequirementCategory = Literal[
    "skill",
    "experience",
    "domain",
    "education",
    "work_authorization",
    "location",
    "seniority",
    "soft",
]


class Requirement(BaseModel):
    id: str = Field(description="Stable id, e.g. 'r_03'.")
    text: str = Field(description="The requirement, normalised to one testable claim.")
    kind: RequirementKind = Field(
        description=(
            "gate = hard disqualifier (years, degree, work auth, location). "
            "must = explicitly required. "
            "nice = 'bonus if'. "
            "implicit = unstated but clearly expected from context."
        )
    )
    category: RequirementCategory
    vocab: list[str] = Field(
        default_factory=list,
        description="Exact terms the JD uses for this. Drives keyword coverage.",
    )
    boilerplate: bool = Field(
        False,
        description=(
            "True for filler that appears in every JD ('strong communication "
            "skills'). Down-weighted in scoring so it cannot sink a verdict."
        ),
    )


class JobSpec(BaseModel):
    title: str | None = None
    company: str | None = None
    seniority: str | None = None
    location: str | None = None
    employment_type: str | None = None
    min_years: float | None = Field(
        None, description="Parsed minimum years. Compared with Python, not an LLM."
    )
    requirements: list[Requirement] = Field(default_factory=list)
    vocabulary: list[str] = Field(
        default_factory=list, description="JD's own terminology, for keyword coverage."
    )

    def by_id(self, rid: str) -> Requirement | None:
        return next((r for r in self.requirements if r.id == rid), None)

    def render_for_prompt(self) -> str:
        lines = [
            f"Role: {self.title or '?'} at {self.company or '?'}",
            f"Seniority: {self.seniority or '?'} | Location: {self.location or '?'}",
            f"Minimum years: {self.min_years if self.min_years is not None else 'unspecified'}",
            "",
            "REQUIREMENTS:",
        ]
        for r in self.requirements:
            tag = r.kind.upper()
            if r.boilerplate:
                tag += "/boilerplate"
            lines.append(f"[{r.id}] ({tag}, {r.category}) {r.text}")
        return "\n".join(lines)

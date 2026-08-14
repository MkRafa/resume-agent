"""ATS keyword coverage. Deterministic - no model needed to count words.

Coverage has a ceiling on purpose. Stuffing is detectable by modern ATS and
reads badly to the human who opens the resume next, so the tailorer is told to
cover terms naturally and we report over-density as a problem, not a win.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

STOPWORDS = {
    "and", "or", "the", "a", "an", "with", "for", "to", "of", "in", "on",
    "experience", "years", "strong", "good", "excellent", "ability", "work",
    "team", "skills", "knowledge", "using", "etc",
}

DENSITY_CEILING = 0.035  # ~3.5% of tokens; above this reads as stuffing
# Density is meaningless on a short document - two mentions in a 40-word draft
# is 5% and perfectly normal. Stuffing requires both a high rate AND enough
# absolute repetitions to look deliberate.
MIN_TOKENS_FOR_DENSITY = 150
MIN_HITS_FOR_STUFFING = 4


@dataclass
class Coverage:
    covered: list[str]
    missing: list[str]
    density: float
    stuffed: list[str]

    @property
    def ratio(self) -> float:
        total = len(self.covered) + len(self.missing)
        return round(len(self.covered) / total, 3) if total else 0.0


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9\+\#\.]+", text.lower())


def _normalise_term(term: str) -> str:
    return re.sub(r"\s+", " ", term.strip().lower())


def keyword_coverage(resume_text: str, vocabulary: list[str]) -> Coverage:
    haystack = " ".join(_tokens(resume_text))
    total_tokens = max(len(haystack.split()), 1)

    covered: list[str] = []
    missing: list[str] = []
    stuffed: list[str] = []

    for raw in vocabulary:
        term = _normalise_term(raw)
        if not term or term in STOPWORDS:
            continue
        pattern = r"(?<![a-z0-9])" + re.escape(term) + r"(?![a-z0-9])"
        hits = len(re.findall(pattern, haystack))
        if hits:
            covered.append(raw)
            if (
                total_tokens >= MIN_TOKENS_FOR_DENSITY
                and hits >= MIN_HITS_FOR_STUFFING
                and hits / total_tokens > DENSITY_CEILING
            ):
                stuffed.append(raw)
        else:
            missing.append(raw)

    matched_tokens = sum(len(_normalise_term(t).split()) for t in covered)
    return Coverage(
        covered=covered,
        missing=missing,
        density=round(matched_tokens / total_tokens, 4),
        stuffed=stuffed,
    )


def resume_to_text(resume) -> str:
    """Flatten a TailoredResume for coverage and lint checks."""
    parts: list[str] = [resume.summary or "", " ".join(resume.skills)]
    for block in resume.experience:
        parts.append(f"{block.role} {block.company}")
        parts.extend(b.text for b in block.bullets)
    parts.extend(b.text for b in resume.projects)
    parts.extend(resume.education)
    return "\n".join(p for p in parts if p)

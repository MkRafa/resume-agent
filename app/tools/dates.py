"""Date arithmetic.

Years of experience is computed here and never asked of a model. LLMs get this
subtly wrong - double-counting overlapping roles, mishandling 'present', losing
a year at a decade boundary - and the errors are quiet and embarrassing because
the number looks plausible.
"""

from __future__ import annotations

import re
from datetime import date

MONTHS = {
    m: i
    for i, m in enumerate(
        [
            "jan", "feb", "mar", "apr", "may", "jun",
            "jul", "aug", "sep", "oct", "nov", "dec",
        ],
        start=1,
    )
}
PRESENT = {"present", "current", "now", "ongoing", "till date", "todate"}


def parse_month(value: str | None, *, today: date | None = None) -> date | None:
    """Accepts 'YYYY-MM', 'YYYY', 'Mar 2021', '03/2021', 'present'."""
    if not value:
        return None
    v = value.strip().lower()
    if v in PRESENT:
        return today or date.today()

    if m := re.fullmatch(r"(\d{4})-(\d{1,2})", v):
        return date(int(m.group(1)), int(m.group(2)), 1)
    if m := re.fullmatch(r"(\d{1,2})[/-](\d{4})", v):
        return date(int(m.group(2)), int(m.group(1)), 1)
    if m := re.fullmatch(r"(\d{4})", v):
        return date(int(m.group(1)), 1, 1)
    if m := re.fullmatch(r"([a-z]{3,9})\.?\s+(\d{4})", v):
        month = MONTHS.get(m.group(1)[:3])
        if month:
            return date(int(m.group(2)), month, 1)
    return None


def _merge(spans: list[tuple[date, date]]) -> list[tuple[date, date]]:
    """Union overlapping spans so concurrent roles are not counted twice."""
    if not spans:
        return []
    spans = sorted(spans)
    merged = [spans[0]]
    for start, end in spans[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def years_of_experience(
    periods: list[tuple[str | None, str | None]],
    *,
    today: date | None = None,
) -> float:
    """Total professional experience in years, overlaps merged, 1 decimal place."""
    today = today or date.today()
    spans: list[tuple[date, date]] = []
    for raw_start, raw_end in periods:
        start = parse_month(raw_start, today=today)
        if not start:
            continue
        end = parse_month(raw_end, today=today) or today
        if end < start:
            start, end = end, start
        spans.append((start, end))

    months = sum(
        (end.year - start.year) * 12 + (end.month - start.month)
        for start, end in _merge(spans)
    )
    return round(max(months, 0) / 12, 1)


def graph_years_of_experience(graph, *, today: date | None = None) -> float:
    """Years across a CareerGraph, counting only employment-bearing atoms."""
    periods = [
        (a.start, a.end)
        for a in graph.atoms
        if a.company and a.type in {"achievement", "responsibility"}
    ]
    return years_of_experience(periods, today=today)

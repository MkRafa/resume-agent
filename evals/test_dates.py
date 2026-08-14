"""Date math. This is the arithmetic we refuse to let a model do."""

from datetime import date

import pytest

from app.tools.dates import parse_month, years_of_experience

TODAY = date(2026, 8, 1)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("2021-03", date(2021, 3, 1)),
        ("2021", date(2021, 1, 1)),
        ("Mar 2021", date(2021, 3, 1)),
        ("March 2021", date(2021, 3, 1)),
        ("03/2021", date(2021, 3, 1)),
        ("present", TODAY),
        ("Current", TODAY),
        ("nonsense", None),
        (None, None),
    ],
)
def test_parse_month(raw, expected):
    assert parse_month(raw, today=TODAY) == expected


def test_simple_span():
    assert years_of_experience([("2019-07", "2022-02")], today=TODAY) == pytest.approx(2.6, abs=0.1)


def test_present_runs_to_today():
    assert years_of_experience([("2022-03", "present")], today=TODAY) == pytest.approx(4.4, abs=0.1)


def test_overlapping_roles_are_not_double_counted():
    """Two concurrent roles are ~5 years of experience, not ~10. LLMs reliably
    get this wrong and the wrong answer looks perfectly plausible."""
    overlapping = years_of_experience(
        [("2021-01", "2026-01"), ("2021-06", "2025-06")], today=TODAY
    )
    assert overlapping == pytest.approx(5.0, abs=0.1)


def test_adjacent_spans_sum():
    total = years_of_experience([("2019-07", "2022-02"), ("2022-03", "present")], today=TODAY)
    assert total == pytest.approx(7.1, abs=0.15)


def test_unparseable_entries_are_skipped_not_guessed():
    assert years_of_experience([("???", "???"), ("2024-08", "2025-08")], today=TODAY) == 1.0


def test_reversed_dates_are_tolerated():
    assert years_of_experience([("2022-01", "2021-01")], today=TODAY) == 1.0


def test_empty_is_zero():
    assert years_of_experience([], today=TODAY) == 0.0

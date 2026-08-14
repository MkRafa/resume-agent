"""Layer 2: verdict agreement against hand-labelled pairs.

This is the eval that tells you whether the product works. Everything else
checks that the machinery runs; this checks that it is *right*.

Costs real API calls, so it is opt-in:

    RUN_GOLD=1 ./.venv/bin/python -m pytest evals/test_gold.py -v

For calibration work prefer the standalone runner, which prints a confusion
matrix and is far easier to read while iterating:

    ./.venv/bin/python evals/run_gold.py

Both share the same on-disk cache of extractions and JD parses, so running one
after the other costs nothing extra.

Error weighting is asymmetric on purpose. Calling a weak profile a strong match
burns one of the candidate's limited applications and their trust; being too
strict is visible, arguable and cheap. Over-generous fails the build;
over-strict xfails so it stays visible without blocking.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from evals.run_gold import GOLD, RANK, run_case

ROOT = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.skipif(
    not os.getenv("RUN_GOLD"),
    reason="Set RUN_GOLD=1 to run the gold set (makes real API calls).",
)


def _cases() -> list[dict]:
    if not GOLD.exists():
        return []
    return [json.loads(line) for line in GOLD.read_text().splitlines() if line.strip()]


@pytest.fixture(scope="module")
def results() -> dict[str, dict]:
    """Run every case once and share the outcomes across the tests below."""
    out: dict[str, dict] = {}
    for case in _cases():
        try:
            out[case["id"]] = run_case(case)
        except Exception as exc:  # noqa: BLE001
            out[case["id"]] = {"id": case["id"], "error": f"{type(exc).__name__}: {exc}"}
    return out


@pytest.mark.parametrize("case", _cases(), ids=lambda c: c["id"])
def test_verdict_matches_gold(case, results):
    row = results[case["id"]]
    if "error" in row:
        pytest.fail(f"{case['id']} did not complete: {row['error']}")

    actual, expected = row["actual"], row["expected"]
    if actual == expected:
        return

    drift = RANK[actual] - RANK[expected]
    detail = (
        f"{case['id']}: expected {expected}, got {actual}\n"
        f"  coverage: {row['coverage']:.0%}  gates_failed: {row['gates_failed']}\n"
        f"  reasons: {row['reasons']}\n"
        f"  this case tests: {case.get('tests', '')}"
    )
    assert drift <= 0, f"OVER-GENEROUS by {drift} rank(s).\n{detail}"
    pytest.xfail(f"Too strict by {abs(drift)} rank(s) — review calibration.\n{detail}")


@pytest.mark.parametrize(
    "case", [c for c in _cases() if c.get("expected_gaps")], ids=lambda c: c["id"]
)
def test_expected_gaps_are_surfaced(case, results):
    row = results[case["id"]]
    if "error" in row:
        pytest.skip("case did not complete")
    assert not row["missed_gaps"], (
        f"{case['id']}: expected these to grade weak but they did not: "
        f"{row['missed_gaps']}. Tests: {case.get('tests', '')}"
    )


@pytest.mark.parametrize(
    "case", [c for c in _cases() if c.get("expected_identity_key")], ids=lambda c: c["id"]
)
def test_identity_key_resolves(case, results):
    """Covers the email-primary / phone-fallback rule on a real extraction."""
    row = results[case["id"]]
    if "error" in row:
        pytest.skip("case did not complete")
    assert row["identity"] == case["expected_identity_key"], (
        f"{case['id']}: expected profile key {case['expected_identity_key']}, "
        f"got {row['identity']}"
    )

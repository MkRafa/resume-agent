"""Verifier eval: the deterministic filter is applied at scoring, not cached.

Regression: the eval cached verify()'s output *after* verify_filter had run,
so a change to the filter never reached cached or --offline results - the eval
kept reporting whatever the filter did on the day the case was first run.
"""

from __future__ import annotations

import json

from evals import run_verifier as rv
from app.schemas import VerifyFlag, VerifyReport


def _case(case_id: str) -> dict:
    for line in rv.CASES.read_text().splitlines():
        if line.strip() and (case := json.loads(line))["id"] == case_id:
            return case
    raise KeyError(case_id)


def test_filter_is_applied_to_cached_raw_output(tmp_path, monkeypatch):
    monkeypatch.setattr(rv, "CACHE", tmp_path)
    case = _case("clean_eks_to_kubernetes")
    # What the model said: EKS -> Kubernetes flagged as invented. Raw, unfiltered.
    raw = VerifyReport(flags=[VerifyFlag(
        claim="Kubernetes", location="experience[0].bullets[0]",
        issue="invented_technology", severity="blocker", explanation="Not in the atom.",
    )])
    rv._cache_path(case).write_text(raw.model_dump_json())

    row = rv.run_case(case, offline=True)
    # The filter softens the accurate generalisation at scoring time, so the
    # truthful resume is not reported as blocked.
    assert row["outcome"] == "clean_with_warning", row

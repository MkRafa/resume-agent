"""Gold-runner cache keys: a prompt or model change must never replay stale output.

Regression: the keys hashed only the fixture files, so after editing
match_grader.md the runner served the previous prompt's grades and reported
them as the new prompt's result. No API calls; nothing is written to the cache.
"""

from __future__ import annotations

from evals import run_gold as rg

PROFILE = rg.ROOT / "evals" / "gold" / "profiles" / "priya_backend.md"
JD = rg.ROOT / "evals" / "gold" / "jds" / "meridian_go_payments.md"


def test_graph_and_job_keys_follow_the_model(monkeypatch):
    from app.config import settings

    before = rg._key(PROFILE, "graph"), rg._key(JD, "job")
    monkeypatch.setattr(settings, "model_extract", "stub/other-extractor")
    monkeypatch.setattr(settings, "model_parse", "stub/other-parser")
    after = rg._key(PROFILE, "graph"), rg._key(JD, "job")
    assert before[0] != after[0] and before[1] != after[1]


def test_keys_follow_the_prompt(monkeypatch):
    before = rg._key(PROFILE, "graph")
    real = rg._producer
    monkeypatch.setattr(rg, "_producer", lambda skill, model: real(skill, model) + b"edited")
    assert rg._key(PROFILE, "graph") != before


def test_rows_key_follows_upstream_output_and_grader(monkeypatch):
    from app.config import settings
    from app.schemas import CareerGraph, Identity, JobSpec

    graph = CareerGraph(identity=Identity(primary_key="a@example.com", keys=["a@example.com"]))
    job = JobSpec(title="Backend")
    base = rg._rows_cache_path(PROFILE, JD, graph, job)

    # A different extraction (e.g. after an extract_profile.md edit) re-grades.
    assert rg._rows_cache_path(PROFILE, JD, graph.model_copy(update={"headline": "x"}), job) != base
    # So does a different grader model.
    monkeypatch.setattr(settings, "model_match", "stub/other-grader")
    assert rg._rows_cache_path(PROFILE, JD, graph, job) != base

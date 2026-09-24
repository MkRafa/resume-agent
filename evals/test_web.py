"""Web layer, against a throwaway store. No model calls."""

from __future__ import annotations

import json
import re

import pytest
from fastapi.testclient import TestClient

from app import store, web
from app.config import settings
from app.schemas import Bullet, ExperienceBlock, TailoredResume, VerifyFlag, VerifyReport


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(settings, "uploads_dir", tmp_path / "uploads")
    monkeypatch.setattr(web, "UPLOADS", tmp_path / "uploads")
    with TestClient(web.app) as c:
        yield c


RESUME = TailoredResume(
    full_name="Priya Raghavan",
    experience=[ExperienceBlock(company="Northwind", role="Engineer", bullets=[
        Bullet(text="Cut latency 45%.", fact_ids=["f_001"])])],
)


def _flag(claim: str) -> VerifyFlag:
    return VerifyFlag(claim=claim, location="experience[0].bullets[0]",
                      issue="unsupported_claim", severity="blocker", explanation="stub")


def test_partial_review_explains_itself_and_keeps_ticks(client):
    """Regression: submitting with some blockers unticked redirected with
    ?unresolved=N, which nothing displayed, and the page came back with every
    box cleared - so the review looked like it had silently done nothing."""
    run_id = store.create_run()
    store.update_run(run_id, status="needs_review", resume_json=RESUME,
                     verify_json=VerifyReport(flags=[_flag("claim one"), _flag("claim two")]))

    res = client.post(f"/runs/{run_id}/review", data={"accept": "claim one"})
    assert res.url.params["unresolved"] == "1"
    page = res.text
    assert "Still blocked" in page and "1 blocker still unticked" in page
    boxes = dict(re.findall(r'value="(claim \w+)"[^>]*?(checked)?>', page))
    assert boxes == {"claim one": "checked", "claim two": ""}
    assert store.get_run(run_id)["status"] == "needs_review"


def test_untraced_claims_are_shown_on_the_run(client, tmp_path):
    """Regression: the page's "untraceable" count only counted bullets with
    no fact_ids; the claim trace's actual findings were written to disk and
    never shown."""
    trace = tmp_path / "untraced_claims.json"
    trace.write_text(json.dumps({"summary": {}, "items": [{
        "section": "bullet", "location": "Northwind/Engineer#0", "kind": "unsourced_number",
        "token": "45%", "text": "Cut latency 45%.",
    }]}))
    run_id = store.create_run()
    store.update_run(run_id, status="done", resume_json=RESUME,
                     artifacts_json={"resume_html": str(tmp_path / "r.html"),
                                     "untraced_claims": str(trace)})

    page = client.get(f"/runs/{run_id}").text
    assert "1 untraced claim" in page
    assert "Untraced claims" in page and "unsourced number" in page and "45%" in page


def test_polled_fragment_skips_the_sidebar(client, monkeypatch):
    run_id = store.create_run()

    def boom():
        raise AssertionError("_nav must not run on a polled fragment")

    monkeypatch.setattr(web, "_nav", boom)
    assert client.get(f"/runs/{run_id}/body").status_code == 200


def test_eval_score_counts_each_case_once(client, tmp_path, monkeypatch):
    """Regression: results.jsonl is append-only, and the sidebar counted every
    line while /system took the latest per case - so after a re-run they
    disagreed."""
    cache = tmp_path / "evals" / ".cache"
    cache.mkdir(parents=True)
    rows = [
        {"id": "a", "expected": "strong_match", "actual": "partial_match", "drift": -1, "coverage": 0.7},
        {"id": "b", "expected": "strong_match", "actual": "strong_match", "drift": 0, "coverage": 0.9},
        # Re-run of "a": now agrees. Latest wins.
        {"id": "a", "expected": "strong_match", "actual": "strong_match", "drift": 0, "coverage": 0.9},
    ]
    (cache / "results.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    monkeypatch.setattr(settings, "root", tmp_path)

    assert web._nav()["eval_score"] == "100%"
    assert "2/2" in client.get("/system").text

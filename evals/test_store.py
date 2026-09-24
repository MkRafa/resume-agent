"""Store + review-gate re-entry, against a throwaway SQLite file.

Regression for a provenance bug: a run awaiting review was rendered against the
*current* profile graph. Every extraction replaces the profile and reassigns
atom ids from f_001, so if the same candidate was re-run before the review was
approved, the resume's citations resolved to someone else's facts.
"""

from __future__ import annotations

import sqlite3

import pytest

from app import runner, store
from app.schemas import (
    Bullet,
    CareerGraph,
    ExperienceBlock,
    FactAtom,
    Identity,
    TailoredResume,
    VerifyFlag,
    VerifyReport,
)

EMAIL = "priya.raghavan@example.com"


def _graph(raw_text: str) -> CareerGraph:
    return CareerGraph(
        identity=Identity(primary_key=EMAIL, keys=[EMAIL], email=EMAIL),
        full_name="Priya Raghavan",
        atoms=[
            FactAtom(id="f_001", type="achievement", raw_text=raw_text,
                     company="Northwind", role="Senior Backend Engineer",
                     start="2022-03", end="present", evidence_strength="led"),
        ],
    )


RESUME = TailoredResume(
    full_name="Priya Raghavan",
    contact={"email": EMAIL},
    experience=[
        ExperienceBlock(
            company="Northwind",
            role="Senior Backend Engineer",
            bullets=[Bullet(text="Cut p99 latency from 1.8s to 640ms.", fact_ids=["f_001"])],
        )
    ],
)
FLAG = VerifyFlag(claim="Cut p99 latency", location="experience[0].bullets[0]",
                  issue="unsupported_claim", severity="blocker", explanation="stub")


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(runner, "_out_dir", lambda run_id: tmp_path / "out" / run_id)
    store.init_db()
    return tmp_path


def test_review_renders_against_the_runs_own_graph(db):
    original = _graph("Cut p99 settlement latency from 1.8s to 640ms with Kafka.")
    key = store.save_profile(original, 4.5)
    run_id = store.create_run()
    store.update_run(
        run_id, status="needs_review", profile_key=key, graph_json=original,
        resume_json=RESUME, verify_json=VerifyReport(flags=[FLAG]),
    )

    # Same candidate re-run before the review is approved: the profile is
    # replaced and f_001 now means something else entirely.
    store.save_profile(_graph("Mentored 3 junior engineers."), 4.5)

    runner.resolve_and_render(run_id, [FLAG.claim])
    run = store.get_run(run_id)

    assert run["status"] == "done", run["error"]
    assert run["graph"].atoms[0].raw_text.startswith("Cut p99")
    # Traced against the right atom, every figure on the bullet is sourced.
    assert "untraced_claims" not in run["artifacts"], run["notes"]


def test_init_db_migrates_a_pre_snapshot_database(db):
    """Existing installs have a runs table without graph_json. CREATE TABLE IF
    NOT EXISTS leaves it alone, so the column has to be added explicitly."""
    store.DB_PATH.unlink()
    conn = sqlite3.connect(store.DB_PATH)
    conn.execute(
        "CREATE TABLE runs (id TEXT PRIMARY KEY, profile_key TEXT, status TEXT NOT NULL, "
        "stage TEXT, jd_title TEXT, jd_company TEXT, verdict TEXT, job_json TEXT, "
        "scorecard_json TEXT, resume_json TEXT, verify_json TEXT, resolved_json TEXT, "
        "artifacts_json TEXT, notes_json TEXT, error TEXT, created_at TEXT NOT NULL, "
        "updated_at TEXT NOT NULL)"
    )
    conn.commit()
    conn.close()

    store.init_db()
    run_id = store.create_run()
    assert store.get_run(run_id)["graph"] is None  # legacy runs fall back to the profile

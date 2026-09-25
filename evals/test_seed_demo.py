"""Demo seeding mechanics, with models stubbed. No API calls."""

from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import pytest

from app import runner, store
from app.schemas import VerifyFlag
from evals.test_pipeline import stub_models  # noqa: F401 - pytest fixture

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def seed(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("seed_demo", ROOT / "scripts" / "seed_demo.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "demo.db")
    monkeypatch.setattr(runner, "_out_dir", lambda run_id: tmp_path / "out" / run_id)
    store.init_db()
    return module


def test_planted_claim_reaches_the_verifier_and_is_labelled(seed, stub_models):  # noqa: F811
    # The stub verifier flags whatever carries the planted figure.
    stub_models.append(VerifyFlag(claim="cutting cloud infrastructure spend by 38%",
                                  location="experience[0].bullets[0]", issue="inflated_metric",
                                  severity="blocker", explanation="no source"))
    run_id, claim = seed._run_with_plant("profiles/meera_data.md", "jds/data_platform.md")
    run = store.get_run(run_id)

    assert claim and claim.endswith("cutting cloud infrastructure spend by 38%.")
    assert run["status"] == "needs_review"
    assert run["jd_title"].endswith("(demo: planted fabrication)")
    assert any("planted" in n for n in run["notes"])
    # The plant is scoped to this one run: later generations are untouched.
    from app.nodes import generate
    assert generate.complete_json.__name__ != "plant"


def test_copy_run_survives_a_different_column_order(seed, tmp_path):
    """A migrated working store has graph_json at the END of `runs`; a fresh
    demo store has it mid-table. A positional copy would scramble columns."""
    source = tmp_path / "working.db"
    conn = sqlite3.connect(source)
    fresh_cols = [r[1] for r in sqlite3.connect(store.DB_PATH).execute("PRAGMA table_info(runs)")]
    legacy = [c for c in fresh_cols if c != "graph_json"] + ["graph_json"]
    conn.execute(f"CREATE TABLE runs ({', '.join(legacy)})")
    conn.execute("CREATE TABLE profiles (key, full_name, graph_json, years, created_at, updated_at)")
    conn.execute("CREATE TABLE profile_keys (key, profile_key)")
    row = {c: None for c in legacy} | {
        "id": "abc123", "status": "done", "verdict": "partial_match", "jd_title": "Backend",
        "created_at": "2026-09-24T00:00:00+00:00", "updated_at": "2026-09-24T00:00:00+00:00",
    }
    conn.execute(f"INSERT INTO runs ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
                 list(row.values()))
    conn.commit()
    conn.close()

    assert seed._copy_run(source, "abc123")
    run = store.get_run("abc123")
    assert (run["status"], run["verdict"], run["jd_title"]) == ("done", "partial_match", "Backend")

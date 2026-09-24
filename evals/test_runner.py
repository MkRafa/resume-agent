"""Background runner: progress labels and failure handling. Models stubbed.

Two regressions from a live run:
- The label named the step that had just FINISHED as if in progress, so the
  page read "Building the career graph" for the six minutes spent parsing.
- A verifier outage failed the run and discarded the verdict and gap report
  that had already been computed.
"""

from __future__ import annotations

import pytest

from app import runner, store
from app.models import ModelCallError
from app.schemas import VerifyReport
from evals.test_pipeline import stub_models  # noqa: F401 - pytest fixture


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(runner, "_out_dir", lambda run_id: tmp_path / "out" / run_id)
    store.init_db()
    return tmp_path


@pytest.fixture
def stages(monkeypatch):
    """Every stage label the runner writes, in order."""
    seen: list[str] = []
    real = store.update_run

    def spy(run_id, **fields):
        if "stage" in fields:
            seen.append(fields["stage"])
        return real(run_id, **fields)

    monkeypatch.setattr(store, "update_run", spy)
    return seen


def _run() -> str:
    run_id = store.create_run()
    runner._execute(
        run_id,
        profile_text="Priya Raghavan, backend engineer. priya.raghavan@example.com",
        profile_file=None,
        jd_text="Senior Backend Engineer at Meridian. 5+ years required.",
        jd_file=None, email=None, phone=None,
    )
    return run_id


def test_labels_name_the_step_in_progress(db, stages, stub_models):  # noqa: F811
    run_id = _run()
    assert store.get_run(run_id)["status"] == "done"
    # The parallel branches are named together while both run...
    assert "Building the career graph · Parsing requirements" in stages
    # ...and each later step is labelled while it runs, in pipeline order.
    order = ["Grading evidence", "Selecting the strongest facts", "Writing the resume",
             "Verifying every claim", "Rendering"]
    positions = [stages.index(label) for label in order]
    assert positions == sorted(positions), stages
    assert stages[-1] == "Complete"


def test_verifier_failure_keeps_the_match_but_not_the_resume(
    db, stages, stub_models, monkeypatch  # noqa: F811
):
    from app.nodes import generate

    stubbed = generate.complete_json

    def verifier_down(schema, **kwargs):
        if schema is VerifyReport:
            raise ModelCallError("[verify] all models exhausted. model_not_found")
        return stubbed(schema, **kwargs)

    monkeypatch.setattr(generate, "complete_json", verifier_down)
    run = store.get_run(_run())

    assert run["status"] == "failed" and run["stage"] == "Model unavailable"
    assert "failed while: verifying every claim" in run["error"]
    assert "still valid" in run["error"]
    # The match survives...
    assert run["verdict"] == "strong_match"
    assert run["scorecard"] is not None and run["job"] is not None and run["graph"] is not None
    # ...the unverified resume does not.
    assert run["resume"] is None

"""Background execution of the pipeline.

A run takes 30-60s and makes half a dozen model calls, so it cannot happen
inside a request. A worker thread executes it and writes status transitions to
SQLite; the UI polls.

A thread pool is the right size for a prototype. When this needs to survive a
process restart or scale past one machine, swap the executor for a real queue -
the status model (queued -> running -> needs_review -> done/failed) is already
the shape a queue would want.
"""

from __future__ import annotations

import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from app import store
from app.config import settings
from app.graph import PIPELINE
from app.models import ModelCallError
from app.nodes import render as render_node
from app.state import new_state

_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="pipeline")
_LOCK = threading.Lock()

# Node -> what the user sees while that node runs. Ordered as the pipeline
# runs, which is also the order parallel steps are listed in.
STAGE_LABELS: dict[str, str] = {
    "intake_profile": "Reading the profile",
    "intake_jd": "Reading the job description",
    "build_career_graph": "Building the career graph",
    "parse_jd": "Parsing requirements",
    "match": "Grading evidence",
    "gap_report": "Analysing gaps",
    "select_facts": "Selecting the strongest facts",
    "tailor": "Writing the resume",
    "verify": "Verifying every claim",
    "render": "Rendering",
}


def submit(
    run_id: str,
    *,
    profile_text: str | None,
    profile_file: str | None,
    jd_text: str | None,
    jd_file: str | None,
    email: str | None,
    phone: str | None,
) -> None:
    _EXECUTOR.submit(
        _execute,
        run_id,
        profile_text=profile_text,
        profile_file=profile_file,
        jd_text=jd_text,
        jd_file=jd_file,
        email=email,
        phone=phone,
    )


def _out_dir(run_id: str) -> Path:
    return settings.out_dir / run_id


def discard_uploads(*paths: str | None) -> None:
    """Delete uploaded source documents once a run has read them.

    An uploaded resume is raw PII, and nothing after intake reads it again: the
    extracted graph is stored, and the review gate re-renders from that. Only
    files inside the uploads directory are touched, so a CLI user's own files
    are never deleted.
    """
    uploads = settings.uploads_dir.resolve()
    for raw in paths:
        if not raw:
            continue
        path = Path(raw).resolve()
        if path.parent == uploads:
            path.unlink(missing_ok=True)


def _classify(exc: Exception) -> tuple[str, str, str]:
    """(stage, headline, detail) for a failed run.

    Providers return a wall of JSON on failure. Showing that raw to a user is
    not an error message - it is a stack trace with extra steps. The headline
    says what to do; the raw payload stays available but folded away.
    """
    raw = str(exc)
    low = raw.lower()

    if "429" in low or "quota" in low or "rate limit" in low or "resource_exhausted" in low:
        return (
            "Rate limited",
            "The model provider is out of quota. Google's free tier allows only 20 requests "
            "per day per model, and one match uses about seven. Either wait for the daily "
            "reset, add billing to the API key, or point MODEL_* in .env at local Ollama "
            "models to keep working offline.",
            raw,
        )
    if "not_found" in low or "no longer available" in low:
        return (
            "Model unavailable",
            "A configured model no longer exists on this provider. Update MODEL_* in .env "
            "to a current model id.",
            raw,
        )
    if "api key" in low or "unauthenticated" in low or "401" in low or "403" in low:
        return (
            "Authentication failed",
            "The provider rejected the API key. Check GEMINI_API_KEY and GROQ_API_KEY in .env.",
            raw,
        )
    if "schema-invalid" in low or "validation error" in low:
        return (
            "Bad model output",
            "The model returned output that did not match the expected schema, twice. This is "
            "usually a model too small for the task — try a stronger one for that node.",
            raw,
        )
    return ("Failed", f"{type(exc).__name__}: {raw[:200]}", raw)


def _execute(run_id: str, **kwargs) -> None:
    try:
        _run_pipeline(run_id, **kwargs)
    finally:
        discard_uploads(kwargs["profile_file"], kwargs["jd_file"])


def _stage_label(running: set[str]) -> str | None:
    """What the run is doing now: every node currently executing, in pipeline
    order - the profile and JD branches run in parallel, so both are named."""
    return " · ".join(label for node, label in STAGE_LABELS.items() if node in running) or None


def _results(final: dict) -> dict:
    """The run's durable results from whatever state the graph reached.

    Deliberately excludes the resume: on a failed run it has not been verified,
    and an unverified resume must never be stored where it could be shown.
    """
    graph, job, scorecard = final.get("graph"), final.get("job"), final.get("scorecard")
    profile_key = (
        store.save_profile(graph, final.get("years_experience", 0.0)) if graph is not None else None
    )
    return {
        "profile_key": profile_key,
        "jd_title": job.title if job else None,
        "jd_company": job.company if job else None,
        "verdict": scorecard.verdict if scorecard else None,
        # Snapshot, not a reference to the profile: the profile is replaced by
        # the next extraction, and atom ids (f_001...) are reassigned each time.
        "graph_json": graph,
        "job_json": job,
        "scorecard_json": scorecard,
    }


def _run_pipeline(run_id: str, **kwargs) -> None:
    store.update_run(run_id, status="running", stage="Reading documents")
    final: dict = {}
    running: set[str] = set()
    try:
        state = new_state(
            profile_text=kwargs["profile_text"],
            profile_file=kwargs["profile_file"],
            jd_text=kwargs["jd_text"],
            jd_file=kwargs["jd_file"],
            email_hint=kwargs["email"],
            phone_hint=kwargs["phone"],
            out_dir=_out_dir(run_id),
        )
        # "tasks" reports each node as it STARTS and as it finishes, so the
        # label names the step in progress. ("updates" only fires on finish,
        # which labelled every step with the one before it - the page said
        # "Building the career graph" for the six minutes spent parsing the JD.)
        # "values" carries the accumulated state; its last emission is final.
        for mode, chunk in PIPELINE.stream(state, stream_mode=["tasks", "values"]):
            if mode == "tasks":
                if "input" in chunk:
                    running.add(chunk["name"])
                else:
                    running.discard(chunk["name"])
                if label := _stage_label(running):
                    store.update_run(run_id, stage=label)
            else:
                final = chunk
    except Exception as exc:  # noqa: BLE001 - a failed run must still report
        failed_during = _stage_label(running)
        if isinstance(exc, ModelCallError):  # includes QuotaExhausted
            stage, headline, detail = _classify(exc)
        else:
            stage, headline = "Crashed", f"{type(exc).__name__}: {exc}"
            detail = traceback.format_exc()[-2000:]
        if failed_during:
            headline = f"{headline} (failed while: {failed_during.lower()})"
        # Keep what finished before the failure. A verifier outage used to
        # discard a verdict and gap report that had already been computed.
        results = _results(final)
        if results["scorecard_json"] is not None:
            headline += " The match below completed before the failure and is still valid."
        store.update_run(
            run_id, status="failed", stage=stage, error=headline, notes_json=[detail], **results
        )
        return

    if final.get("errors"):
        store.update_run(
            run_id, status="failed", stage="Rejected", error="\n".join(final["errors"])
        )
        return

    common = {
        **_results(final),
        "resume_json": final.get("resume"),
        "verify_json": final.get("verify_report"),
        "artifacts_json": final.get("artifacts", {}),
        "notes_json": final.get("notes", []),
    }

    report = final.get("verify_report")
    if final.get("artifacts"):
        store.update_run(run_id, status="done", stage="Complete", **common)
    elif report and report.blockers:
        # The human-review step: the resume exists but is not released until a
        # person has confirmed, edited or dropped every unsupported claim.
        store.update_run(run_id, status="needs_review", stage="Awaiting your review", **common)
    else:
        # No resume: verdict was not_matching, so the gap report is the output.
        store.update_run(run_id, status="done", stage="Complete", **common)


def resolve_and_render(run_id: str, accepted_claims: list[str]) -> None:
    """Second half of the human-in-the-loop step.

    Renders the resume the user already reviewed, rather than re-running the
    pipeline - a fresh run would generate a *different* resume whose claims no
    longer correspond to the ones just accepted.
    """
    run = store.get_run(run_id)
    if not run or not run["resume"]:
        return

    # The resume's fact_ids refer to the graph this run extracted. The stored
    # profile may since have been replaced by a later run for the same person,
    # with different atoms under the same ids - rendering against that would
    # check provenance and claims against the wrong facts.
    graph = run["graph"]
    if graph is None:
        # Runs recorded before graph snapshots existed: the profile is the only
        # copy left, and is correct unless the person has been re-run since.
        profile = store.get_profile(run["profile_key"]) if run["profile_key"] else None
        if not profile:
            store.update_run(run_id, status="failed", error="Profile missing for this run.")
            return
        graph, _ = profile

    state = {
        "resume": run["resume"],
        "graph": graph,
        "job": run["job"],
        "scorecard": run["scorecard"],
        "verify_report": run["verify"],
        "resolved_claims": accepted_claims,
        "selected_fact_ids": [
            fid for bullet in run["resume"].all_bullets() for fid in bullet.fact_ids
        ],
        "out_dir": _out_dir(run_id),
        "artifacts": {},
        "notes": [],
    }
    try:
        result = render_node(state)  # type: ignore[arg-type]
    except Exception as exc:  # noqa: BLE001
        store.update_run(run_id, status="failed", stage="Render failed", error=str(exc))
        return

    store.update_run(
        run_id,
        status="done",
        stage="Complete",
        resolved_json=accepted_claims,
        artifacts_json=result.get("artifacts", {}),
        notes_json=[*run["notes"], *result.get("notes", [])],
    )

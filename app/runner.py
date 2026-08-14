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
from app.models import ModelCallError, QuotaExhausted
from app.nodes import render as render_node
from app.state import new_state

_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="pipeline")
_LOCK = threading.Lock()


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
    store.update_run(run_id, status="running", stage="Reading documents")
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
        final = PIPELINE.invoke(state)
    except (QuotaExhausted, ModelCallError) as exc:
        stage, headline, detail = _classify(exc)
        store.update_run(
            run_id, status="failed", stage=stage, error=headline, notes_json=[detail]
        )
        return
    except Exception as exc:  # noqa: BLE001 - a crashed run must still report
        store.update_run(
            run_id,
            status="failed",
            stage="Crashed",
            error=f"{type(exc).__name__}: {exc}",
            notes_json=[traceback.format_exc()[-2000:]],
        )
        return

    if final.get("errors"):
        store.update_run(
            run_id, status="failed", stage="Rejected", error="\n".join(final["errors"])
        )
        return

    graph = final.get("graph")
    job = final.get("job")
    scorecard = final.get("scorecard")
    profile_key = None

    if graph is not None:
        profile_key = store.save_profile(graph, final.get("years_experience", 0.0))

    common = {
        "profile_key": profile_key,
        "jd_title": job.title if job else None,
        "jd_company": job.company if job else None,
        "verdict": scorecard.verdict if scorecard else None,
        "job_json": job,
        "scorecard_json": scorecard,
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

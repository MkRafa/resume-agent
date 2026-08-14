"""FastAPI app.

One Python service: the pipeline, the store and the UI. Server-rendered Jinja
with a small amount of vanilla JS for polling - no build step, no CDN, works
offline.

    ./.venv/bin/uvicorn app.web:app --reload --port 8000
"""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path

from fastapi import FastAPI, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from app import store
from app.config import settings
from app.nodes.rendering import ats_lint
from app.preflight import missing_credentials
from app.runner import resolve_and_render, submit
from app.tools.documents import IMAGE_SUFFIXES, TEXT_SUFFIXES

TEMPLATES = Jinja2Templates(directory=str(settings.root / "app" / "templates"))
UPLOADS = settings.data_dir / "uploads"
ALLOWED_SUFFIXES = {".pdf", ".docx", *TEXT_SUFFIXES, *IMAGE_SUFFIXES}

app = FastAPI(title="resume-agent")


@app.on_event("startup")
def _startup() -> None:
    store.init_db()
    UPLOADS.mkdir(parents=True, exist_ok=True)


def render(request: Request, name: str, context: dict, status_code: int = 200) -> HTMLResponse:
    """Current Starlette wants (request, name, context) — the older
    (name, context-with-request) form fails with an unhashable-dict TypeError."""
    return TEMPLATES.TemplateResponse(request, name, context, status_code=status_code)


def _save_upload(upload: UploadFile | None) -> str | None:
    """Persist an upload and return its path, or None if nothing was sent."""
    if upload is None or not upload.filename:
        return None
    suffix = Path(upload.filename).suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise ValueError(
            f"Unsupported file type '{suffix or '(none)'}'. "
            f"Accepted: {', '.join(sorted(ALLOWED_SUFFIXES))}"
        )
    dest = UPLOADS / f"{uuid.uuid4().hex[:12]}{suffix}"
    with dest.open("wb") as fh:
        shutil.copyfileobj(upload.file, fh)
    return str(dest)


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return render(
        request,
        "web/index.html",
        {
            "runs": store.list_runs(limit=12),
            "profiles": store.list_profiles(),
            "credential_problems": missing_credentials(),
        },
    )


@app.post("/runs")
async def start_run(
    request: Request,
    profile_text: str = Form(""),
    jd_text: str = Form(""),
    email: str = Form(""),
    phone: str = Form(""),
    profile_file: UploadFile | None = None,
    jd_file: UploadFile | None = None,
):
    try:
        profile_path = _save_upload(profile_file)
        jd_path = _save_upload(jd_file)
    except ValueError as exc:
        return render(
            request,
            "web/index.html",
            {
                "runs": store.list_runs(limit=12),
                "profiles": store.list_profiles(),
                "credential_problems": missing_credentials(),
                "error": str(exc),
            },
            status_code=400,
        )

    if not (profile_text.strip() or profile_path) or not (jd_text.strip() or jd_path):
        return render(
            request,
            "web/index.html",
            {
                "runs": store.list_runs(limit=12),
                "profiles": store.list_profiles(),
                "credential_problems": missing_credentials(),
                "error": "Provide a profile and a job description — typed, uploaded, or both.",
            },
            status_code=400,
        )

    run_id = store.create_run()
    submit(
        run_id,
        profile_text=profile_text or None,
        profile_file=profile_path,
        jd_text=jd_text or None,
        jd_file=jd_path,
        email=email or None,
        phone=phone or None,
    )
    return RedirectResponse(f"/runs/{run_id}", status_code=303)


def _run_context(run: dict) -> dict:
    scorecard, job = run["scorecard"], run["job"]
    rows = []
    if scorecard and job:
        order = {"gate": 0, "must": 1, "implicit": 2, "nice": 3}
        rows = sorted(
            (
                {"row": r, "req": job.by_id(r.requirement_id)}
                for r in scorecard.rows
                if job.by_id(r.requirement_id)
            ),
            key=lambda item: (order.get(item["req"].kind, 9), item["row"].requirement_id),
        )
    lint = ats_lint(run["resume"], job) if run["resume"] and run["status"] == "done" else []
    return {"run": run, "rows": rows, "lint": lint}


@app.get("/runs/{run_id}", response_class=HTMLResponse)
def run_page(request: Request, run_id: str):
    run = store.get_run(run_id)
    if not run:
        return HTMLResponse("Run not found", status_code=404)
    return render(request, "web/run.html", _run_context(run))


@app.get("/runs/{run_id}/body", response_class=HTMLResponse)
def run_body(request: Request, run_id: str):
    """Polled fragment. The page stops polling once status is terminal."""
    run = store.get_run(run_id)
    if not run:
        return HTMLResponse("Run not found", status_code=404)
    return render(request, "web/_run_body.html", _run_context(run))


@app.post("/runs/{run_id}/review")
async def submit_review(request: Request, run_id: str):
    """Accept the claims the user ticked; anything unticked stays blocking.

    Unchecked blockers mean the resume does not render - which is the point.
    """
    form = await request.form()
    accepted = [v for k, v in form.multi_items() if k == "accept"]
    run = store.get_run(run_id)
    if not run or not run["verify"]:
        return RedirectResponse(f"/runs/{run_id}", status_code=303)

    outstanding = [f for f in run["verify"].blockers if f.claim not in accepted]
    if outstanding:
        store.update_run(run_id, resolved_json=accepted)
        return RedirectResponse(f"/runs/{run_id}?unresolved={len(outstanding)}", status_code=303)

    store.update_run(run_id, status="running", stage="Rendering")
    resolve_and_render(run_id, accepted)
    return RedirectResponse(f"/runs/{run_id}", status_code=303)


@app.get("/runs/{run_id}/resume", response_class=HTMLResponse)
def run_resume(run_id: str):
    run = store.get_run(run_id)
    if not run or not run["artifacts"].get("resume_html"):
        return HTMLResponse("No resume for this run", status_code=404)
    return HTMLResponse(Path(run["artifacts"]["resume_html"]).read_text(encoding="utf-8"))


@app.get("/runs/{run_id}/download")
def run_download(run_id: str):
    run = store.get_run(run_id)
    if not run:
        return HTMLResponse("Run not found", status_code=404)
    pdf = run["artifacts"].get("resume_pdf")
    if pdf and Path(pdf).exists():
        return Response(
            Path(pdf).read_bytes(),
            media_type="application/pdf",
            headers={"Content-Disposition": 'attachment; filename="resume.pdf"'},
        )
    html = run["artifacts"].get("resume_html")
    if html and Path(html).exists():
        return Response(
            Path(html).read_bytes(),
            media_type="text/html",
            headers={"Content-Disposition": 'attachment; filename="resume.html"'},
        )
    return HTMLResponse("No resume artifact", status_code=404)


@app.get("/profiles/{key:path}", response_class=HTMLResponse)
def profile_page(request: Request, key: str):
    profile = store.get_profile(key)
    if not profile:
        return HTMLResponse("Profile not found", status_code=404)
    graph, years = profile
    by_company: dict[str, list] = {}
    for atom in graph.atoms:
        by_company.setdefault(atom.company or "Other", []).append(atom)
    return render(
        request,
        "web/profile.html",
        {
            "graph": graph,
            "years": years,
            "by_company": by_company,
            "runs": store.list_runs(profile_key=key),
        },
    )

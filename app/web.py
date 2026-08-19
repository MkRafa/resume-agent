"""FastAPI app.

One Python service: the pipeline, the store and the UI. Server-rendered Jinja
with a small amount of vanilla JS for polling - no build step, no CDN, works
offline.

    ./.venv/bin/uvicorn app.web:app --reload --port 8000
"""

from __future__ import annotations

import json
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
from app.schemas.match import UNSCORABLE_CATEGORIES
from app.tools.documents import IMAGE_SUFFIXES, TEXT_SUFFIXES

TEMPLATES = Jinja2Templates(directory=str(settings.root / "app" / "templates"))
UPLOADS = settings.data_dir / "uploads"
ALLOWED_SUFFIXES = {".pdf", ".docx", *TEXT_SUFFIXES, *IMAGE_SUFFIXES}

app = FastAPI(title="resume-agent")


@app.on_event("startup")
def _startup() -> None:
    store.init_db()
    UPLOADS.mkdir(parents=True, exist_ok=True)


def _nav() -> dict:
    """Sidebar context, on every page.

    The counts are real rather than decorative — the design shows numbers next
    to each section, and showing invented ones would make the chrome lie about
    the system it is reporting on.
    """
    runs = store.list_runs(limit=200)
    eval_score = None
    results = settings.root / "evals" / ".cache" / "results.jsonl"
    if results.exists():
        rows = [json.loads(l) for l in results.read_text().splitlines() if l.strip()]
        graded = [r for r in rows if "error" not in r]
        if graded:
            agree = sum(1 for r in graded if r.get("drift") == 0)
            eval_score = f"{round(100 * agree / len(graded))}%"

    return {
        "runs": len(runs),
        "profiles": len(store.list_profiles()),
        "inflight": [r for r in runs if r["status"] in ("queued", "running", "needs_review")][:4],
        "eval_score": eval_score,
        "match_model": settings.model_match.split("/", 1)[-1],
        "provider": settings.model_match.split("/", 1)[0],
    }


def render(
    request: Request, name: str, context: dict, status_code: int = 200, page: str = ""
) -> HTMLResponse:
    """Current Starlette wants (request, name, context) — the older
    (name, context-with-request) form fails with an unhashable-dict TypeError."""
    context = {**context, "nav": _nav(), "page": page}
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
        page="new",
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
    """Group the scorecard the way the design reads it: gates first (a single
    failure ends the run), then scorable must-haves, then nice-to-haves that
    never move the verdict. The excluded rows are shown but visibly set apart,
    so it is obvious *why* they left the denominator."""
    scorecard, job = run["scorecard"], run["job"]
    groups: list[dict] = []
    breakdown = {"direct": 0, "adjacent": 0, "absent": 0, "excluded": 0}

    if scorecard and job:
        buckets: dict[str, list] = {"gate": [], "must": [], "nice": []}
        for row in scorecard.rows:
            req = job.by_id(row.requirement_id)
            if req is None:
                continue
            excluded = req.boilerplate or req.category in UNSCORABLE_CATEGORIES
            item = {"row": row, "req": req, "excluded": excluded}
            buckets.setdefault("nice" if req.kind == "nice" else req.kind, []).append(item)

            if req.kind == "nice":
                continue
            if excluded:
                breakdown["excluded"] += 1
            elif row.grade == "direct":
                breakdown["direct"] += 1
            elif row.grade in ("adjacent", "transferable"):
                breakdown["adjacent"] += 1
            else:
                breakdown["absent"] += 1

        labels = {
            "gate": ("Gates", "a single failure ends the run"),
            "must": ("Must-haves", "weighted into coverage"),
            "nice": ("Nice-to-haves", "never affect the verdict"),
        }
        for kind in ("gate", "must", "nice"):
            # NB: the key must not be called "items" — Jinja resolves g.items to
            # dict.items (the bound method) before it ever looks for the key.
            entries = sorted(buckets.get(kind, []), key=lambda i: i["row"].requirement_id)
            if entries:
                title, note = labels[kind]
                groups.append({"title": title, "note": note, "rows": entries, "kind": kind})

    lint = ats_lint(run["resume"], job) if run["resume"] and run["status"] == "done" else []
    report = run.get("verify")
    resolved = set(run.get("resolved") or [])
    blockers = report.blockers if report else []
    return {
        "run": run,
        "groups": groups,
        "breakdown": breakdown,
        "lint": lint,
        "blockers_total": len(blockers),
        "blockers_resolved": sum(1 for f in blockers if f.claim in resolved),
    }


@app.get("/runs/{run_id}", response_class=HTMLResponse)
def run_page(request: Request, run_id: str):
    run = store.get_run(run_id)
    if not run:
        return HTMLResponse("Run not found", status_code=404)
    return render(request, "web/run.html", _run_context(run), page="runs")


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
        page="profiles",
    )


@app.get("/runs", response_class=HTMLResponse)
def runs_index(request: Request):
    return render(request, "web/runs.html", {"runs": store.list_runs(limit=100)}, page="runs")


@app.get("/profiles", response_class=HTMLResponse)
def profiles_index(request: Request):
    return render(
        request, "web/profiles.html", {"profiles": store.list_profiles()}, page="profiles"
    )


@app.get("/system", response_class=HTMLResponse)
def system_page(request: Request):
    """Model routing and the eval baseline — both read from the live config and
    the recorded gold results rather than being written into the template."""
    nodes = [
        ("extract", "profile -> career graph", settings.model_extract),
        ("parse", "JD -> requirements", settings.model_parse),
        ("match", "grade evidence per requirement", settings.model_match),
        ("tailor", "select facts, write the resume", settings.model_tailor),
        ("verify", "adversarial fact-check", settings.model_verify),
    ]
    results = settings.root / "evals" / ".cache" / "results.jsonl"
    gold: list[dict] = []
    if results.exists():
        seen: dict[str, dict] = {}
        for line in results.read_text().splitlines():
            if line.strip():
                row = json.loads(line)
                seen[row["id"]] = row
        gold = list(seen.values())
    graded = [r for r in gold if "error" not in r]
    summary = {
        "total": len(graded),
        "agree": sum(1 for r in graded if r.get("drift") == 0),
        "generous": sum(1 for r in graded if (r.get("drift") or 0) > 0),
        "strict": sum(1 for r in graded if (r.get("drift") or 0) < 0),
    }
    return render(
        request,
        "web/system.html",
        {
            "nodes": nodes,
            "fallbacks": [m for m in settings.fallbacks.split(",") if m.strip()],
            "redact": settings.redact_pii,
            "gold": sorted(graded, key=lambda r: r["id"]),
            "summary": summary,
            "credential_problems": missing_credentials(),
        },
        page="system",
    )

"""Render + ATS lint. Gated on the verifier.

The guardrail runs first and raises rather than warns. A blocked render is the
system working: it means a claim reached the output that no fact supports.
"""

from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.config import settings
from app.hooks import block_on_unresolved_flags, write_audit_log
from app.schemas import TailoredResume
from app.state import PipelineState
from app.tools import keyword_coverage, resume_to_text
from app.tools.claim_trace import summarise, trace_claims


def _write_trace(out_dir: Path, untraced, stats: dict) -> Path:
    import json

    path = out_dir / "untraced_claims.json"
    path.write_text(
        json.dumps(
            {
                "summary": stats,
                "items": [
                    {"location": u.location, "kind": u.kind, "token": u.token, "text": u.text}
                    for u in untraced
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return path

TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"


def _env() -> Environment:
    return Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        autoescape=select_autoescape(["html", "xml"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )


def render_html(resume: TailoredResume, *, links: list[str] | None = None, pages: int = 1) -> str:
    template = _env().get_template("ats_clean.html.j2")
    return template.render(
        r=resume,
        links=links or [],
        page_size="A4",
        pages=pages,
    )


def ats_lint(resume: TailoredResume, job) -> list[str]:
    """Deterministic checks. No model involved in counting words or bullets."""
    issues: list[str] = []
    text = resume_to_text(resume)

    if not resume.full_name:
        issues.append("No candidate name - many parsers key the record on it.")
    if not resume.contact.get("email") and not resume.contact.get("phone"):
        issues.append("No contact method on the resume.")

    bullets = resume.all_bullets()
    orphans = [b.text[:60] for b in bullets if not b.fact_ids]
    if orphans:
        issues.append(f"{len(orphans)} bullet(s) cite no source fact: {orphans[:3]}")

    long_bullets = [b.text[:50] for b in bullets if len(b.text) > 240]
    if long_bullets:
        issues.append(f"{len(long_bullets)} bullet(s) exceed ~2 lines: {long_bullets[:2]}")

    if job is not None:
        coverage = keyword_coverage(text, job.vocabulary)
        if coverage.ratio < 0.4:
            issues.append(
                f"Low keyword coverage ({coverage.ratio:.0%}). Missing: "
                f"{', '.join(coverage.missing[:8])}"
            )
        if coverage.stuffed:
            issues.append(
                f"Possible keyword stuffing on: {', '.join(coverage.stuffed)} - "
                "reads badly to humans and is detectable."
            )

    words = len(text.split())
    if words > 900:
        issues.append(f"~{words} words: likely over two pages. Cut the weakest bullets.")
    return issues


def render(state: PipelineState) -> dict:
    resume, graph, job = state["resume"], state["graph"], state["job"]
    assert resume is not None and graph is not None

    # Hard gate. Nothing renders while a blocker is unresolved.
    block_on_unresolved_flags(
        state.get("verify_report"),
        resolved=set(state.get("resolved_claims", [])),
    )

    out_dir = state.get("out_dir") or settings.out_dir / graph.identity.primary_key.replace("/", "_")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    html = render_html(resume, links=graph.links)
    html_path = out_dir / "resume.html"
    html_path.write_text(html, encoding="utf-8")

    artifacts = {**state.get("artifacts", {}), "resume_html": str(html_path)}
    notes: list[str] = []

    try:
        from weasyprint import HTML  # optional; needs pango/cairo

        pdf_path = out_dir / "resume.pdf"
        HTML(string=html, base_url=str(TEMPLATE_DIR)).write_pdf(str(pdf_path))
        artifacts["resume_pdf"] = str(pdf_path)
    except Exception as exc:  # noqa: BLE001 - optional dependency, degrade cleanly
        notes.append(
            f"PDF skipped ({type(exc).__name__}). HTML written instead; "
            "`brew install pango cairo gdk-pixbuf libffi && pip install weasyprint` to enable."
        )

    audit_path = write_audit_log(
        out_dir,
        resume=resume,
        graph=graph,
        extra={
            "verdict": state["scorecard"].verdict if state.get("scorecard") else None,
            "selected_fact_ids": state.get("selected_fact_ids", []),
        },
    )
    artifacts["provenance"] = str(audit_path)

    lint = ats_lint(resume, job)
    if lint:
        notes.extend(f"ATS lint: {issue}" for issue in lint)

    # Deterministic second opinion on the LLM verifier. It shares nothing with
    # that pass - arithmetic and set membership rather than judgement - so a
    # fabrication both models find plausible still surfaces here.
    untraced = trace_claims(resume, graph)
    if untraced:
        stats = summarise(untraced, resume)
        notes.append(
            f"Claim trace: {stats['affected_bullets']}/{stats['bullets']} bullets have "
            f"untraceable content ({stats['by_kind']})"
        )
        notes.extend(f"Claim trace: {u}" for u in untraced[:8])
        artifacts["untraced_claims"] = str(_write_trace(out_dir, untraced, stats))

    return {"artifacts": artifacts, "notes": notes, "out_dir": out_dir}

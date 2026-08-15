"""Generation: select -> tailor -> verify.

Three separate calls rather than one. Selection keeps the writer from padding,
and verification only works because it runs in a different context - and, by
config default, on a different model family.
"""

from __future__ import annotations

from app.config import settings
from app.models import complete_json
from app.schemas import SelectedFacts, TailoredResume, VerifyReport
from app.skills import load
from app.state import PipelineState
from app.tools import strongest_hooks
from app.tools.verify_filter import soften_known_false_positives


def select_facts(state: PipelineState) -> dict:
    graph, job, scorecard = state["graph"], state["job"], state["scorecard"]
    assert graph is not None and job is not None and scorecard is not None

    cited = sorted({fid for row in scorecard.rows for fid in row.evidence_fact_ids})
    evidence_summary = "\n".join(
        f"[{row.requirement_id}] {row.grade} <- {', '.join(row.evidence_fact_ids) or 'none'}"
        for row in scorecard.rows
    )

    selection = complete_json(
        SelectedFacts,
        node="tailor",
        system=load("select_facts"),
        stable_context=f"CANDIDATE CAREER FACTS:\n\n{graph.render_for_prompt()}",
        variable_context=(
            f"{job.render_for_prompt()}\n\n"
            f"SCORECARD (which atoms already proved which requirement):\n{evidence_summary}\n\n"
            f"ATOMS ALREADY CITED AS EVIDENCE (near-mandatory): {', '.join(cited) or '(none)'}\n\n"
            f"TOTAL EXPERIENCE: {state.get('years_experience', 0)} years - "
            "use it to pick the page budget."
        ),
        temperature=0.1,
    )

    valid = [fid for fid in selection.fact_ids if graph.by_id(fid)]
    # Evidence the scorecard relied on must survive selection, or the resume
    # fails to demonstrate the very requirements it was judged to meet.
    for fid in cited:
        if fid not in valid:
            valid.append(fid)

    return {
        "selected_fact_ids": valid,
        "notes": [f"Selected {len(valid)} atoms for the resume"],
    }


def tailor(state: PipelineState) -> dict:
    graph, job, scorecard = state["graph"], state["job"], state["scorecard"]
    assert graph is not None and job is not None and scorecard is not None

    selected = [graph.by_id(fid) for fid in state.get("selected_fact_ids", [])]
    selected = [a for a in selected if a is not None]

    atoms_block = "\n".join(
        f"[{a.id}] {a.role or '?'} @ {a.company or '?'} ({a.start or '?'}..{a.end or '?'}) "
        f"ownership={a.evidence_strength}\n    {a.raw_text}"
        + (f"\n    metrics: {[m.model_dump(exclude_none=True) for m in a.metrics]}" if a.metrics else "")
        + (f"\n    skills: {', '.join(a.skills)}" if a.skills else "")
        for a in selected
    )

    hooks = strongest_hooks(scorecard, job)

    resume = complete_json(
        TailoredResume,
        node="tailor",
        system=load("tailor"),
        variable_context=(
            f"CANDIDATE: {graph.full_name or '(name withheld)'}\n"
            f"LOCATION: {graph.location or '?'}\n"
            f"LINKS: {', '.join(graph.links) or '(none)'}\n"
            # The JD block below states the employer's *minimum* years. Without
            # the candidate's own computed figure alongside it, the model
            # copied that minimum onto the resume - writing "5 years of
            # experience" for a candidate with 7.0, which is both false and
            # undersells them.
            f"CANDIDATE TOTAL EXPERIENCE: {state.get('years_experience', 0)} years "
            "(computed from their dates - use THIS number if you mention years; "
            "the JD's minimum below is the employer's requirement, not the "
            "candidate's experience)\n\n"
            f"{job.render_for_prompt()}\n\n"
            f"EMPLOYER VOCABULARY (use naturally, never stuff): "
            f"{', '.join(job.vocabulary) or '(none)'}\n\n"
            f"STRONGEST HOOKS FOR THE SUMMARY: {'; '.join(hooks) or '(none)'}\n\n"
            f"FACTS YOU MAY USE - these and only these:\n{atoms_block}"
        ),
        temperature=0.4,
    )

    if graph.full_name and not resume.full_name:
        resume.full_name = graph.full_name
    contact = dict(resume.contact)
    if graph.identity.email:
        contact.setdefault("email", graph.identity.email)
    if graph.identity.phone:
        contact.setdefault("phone", graph.identity.phone)
    if graph.location:
        contact.setdefault("location", graph.location)
    resume.contact = contact

    return {"resume": resume}


def verify(state: PipelineState) -> dict:
    """Adversarial pass. Deliberately does NOT receive the job description:
    a verifier that can see what the text was optimised for rationalises its
    stretches instead of catching them."""
    graph, resume = state["graph"], state["resume"]
    assert graph is not None and resume is not None

    resume_block = _render_for_verification(resume)
    allowed = [a for a in graph.atoms if a.id in set(state.get("selected_fact_ids", []))]
    facts_block = "\n".join(
        f"[{a.id}] {a.role or '?'} @ {a.company or '?'} ({a.start or '?'}..{a.end or '?'}) "
        f"ownership={a.evidence_strength}\n    {a.raw_text}"
        + (f"\n    metrics: {[m.model_dump(exclude_none=True) for m in a.metrics]}" if a.metrics else "")
        for a in allowed
    )

    report = complete_json(
        VerifyReport,
        node="verify",
        system=load("verify"),
        variable_context=(
            f"SOURCE FACTS (the only truth available):\n{facts_block}\n\n"
            f"COMPUTED TOTAL EXPERIENCE: {state.get('years_experience', 0)} years. "
            "Any other figure for total years on the resume is wrong - flag it.\n\n"
            f"GENERATED RESUME TO CHECK:\n{resume_block}"
        ),
        temperature=0.0,
    )

    # The model will not reliably honour two of the allowed transformations even
    # when the prompt spells them out, so they are enforced in Python. This only
    # ever downgrades blocker -> warning; nothing is deleted or escalated.
    soften_known_false_positives(report, graph)

    note = (
        f"Verifier ({settings.model_verify}): {len(report.blockers)} blocker(s), "
        f"{len(report.flags) - len(report.blockers)} warning(s)"
    )
    return {"verify_report": report, "notes": [note]}


def _render_for_verification(resume) -> str:
    lines: list[str] = []
    if resume.summary:
        lines.append(f"summary: {resume.summary}")
    lines.append(f"skills: {', '.join(resume.skills)}")
    for i, block in enumerate(resume.experience):
        lines.append(f"experience[{i}]: {block.role} @ {block.company} ({block.start}..{block.end})")
        for j, bullet in enumerate(block.bullets):
            lines.append(
                f"  experience[{i}].bullets[{j}] (cites {', '.join(bullet.fact_ids) or 'NOTHING'}): "
                f"{bullet.text}"
            )
    for j, bullet in enumerate(resume.projects):
        lines.append(f"  projects[{j}] (cites {', '.join(bullet.fact_ids) or 'NOTHING'}): {bullet.text}")
    lines.extend(f"education: {e}" for e in resume.education)
    return "\n".join(lines)

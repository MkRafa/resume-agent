#!/usr/bin/env python
"""M0 CLI.

    python cli.py --profile-file data/profiles/sample_resume.md \
                  --jd-file data/jds/sample_jd.md

Either side accepts --*-text or --*-file (or both). The scorecard is the point
of M0: if it is not sharp, nothing downstream matters.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from app.config import settings
from app.graph import PIPELINE
from app.hooks import cost_summary
from app.hooks.guardrail import RenderBlocked
from app.nodes import render as render_node
from app.preflight import missing_credentials
from app.state import new_state

console = Console()

GRADE_STYLE = {
    "direct": "bold green",
    "adjacent": "yellow",
    "transferable": "dark_orange",
    "none": "bold red",
    "unknown": "dim cyan",
}
GRADE_MARK = {
    "direct": "✔",
    "adjacent": "~",
    "transferable": "≈",
    "none": "✘",
    "unknown": "?",
}
VERDICT_STYLE = {
    "strong_match": ("bold white on green", "STRONG MATCH"),
    "partial_match": ("bold black on yellow", "PARTIAL MATCH"),
    "not_matching": ("bold white on red", "NOT MATCHING"),
}


def print_scorecard(scorecard, job) -> None:
    table = Table(title="Evidence scorecard", header_style="bold", show_lines=False)
    table.add_column("", width=2)
    table.add_column("Req", style="dim", width=5)
    table.add_column("Kind", width=8)
    table.add_column("Requirement", max_width=46)
    table.add_column("Evidence", width=16)
    table.add_column("Why", max_width=44)

    order = {"gate": 0, "must": 1, "implicit": 2, "nice": 3}
    rows = sorted(
        scorecard.rows,
        key=lambda r: (order.get(getattr(job.by_id(r.requirement_id), "kind", "nice"), 9),
                       r.requirement_id),
    )
    for row in rows:
        req = job.by_id(row.requirement_id)
        style = GRADE_STYLE[row.grade]
        table.add_row(
            f"[{style}]{GRADE_MARK[row.grade]}[/]",
            row.requirement_id,
            f"[{style}]{req.kind if req else '?'}[/]",
            (req.text if req else "?") + (" [dim](boilerplate)[/]" if req and req.boilerplate else ""),
            ", ".join(row.evidence_fact_ids) or "[dim]—[/]",
            row.rationale,
        )
    console.print(table)


def print_verdict(scorecard) -> None:
    style, label = VERDICT_STYLE[scorecard.verdict]
    body = "\n".join(f"• {r}" for r in scorecard.verdict_reasons)
    console.print(Panel(body, title=f"[{style}] {label} [/]", border_style=style.split()[-1]))


def print_open_questions(scorecard) -> None:
    if not scorecard.open_questions:
        return
    console.print(
        Panel(
            "\n".join(f"• {q}" for q in scorecard.open_questions),
            title="Needs your confirmation (not counted for or against you)",
            border_style="cyan",
        )
    )


def print_gaps(scorecard) -> None:
    if not scorecard.gaps:
        return
    table = Table(title="Gaps", header_style="bold")
    table.add_column("Req", style="dim", width=5)
    table.add_column("Severity", width=12)
    table.add_column("What's missing", max_width=44)
    table.add_column("What to do", max_width=52)
    sev_style = {"dealbreaker": "bold red", "significant": "yellow", "coachable": "green"}
    for gap in scorecard.gaps:
        table.add_row(
            gap.requirement_id,
            f"[{sev_style.get(gap.severity, '')}]{gap.severity}[/]",
            gap.explanation,
            gap.how_to_address or "[dim]—[/]",
        )
    console.print(table)

    if scorecard.adjacent_roles:
        console.print(
            Panel(
                "\n".join(f"• {r}" for r in scorecard.adjacent_roles),
                title="Roles this profile would match",
                border_style="cyan",
            )
        )


def print_verify(report) -> None:
    if report is None:
        return
    if not report.flags:
        console.print("[green]Verifier: no unsupported claims.[/]")
        return
    table = Table(title="Verifier flags", header_style="bold")
    table.add_column("Sev", width=8)
    table.add_column("Issue", width=22)
    table.add_column("Claim", max_width=48)
    table.add_column("Why", max_width=46)
    for flag in report.flags:
        style = "bold red" if flag.severity == "blocker" else "yellow"
        table.add_row(f"[{style}]{flag.severity}[/]", flag.issue, flag.claim, flag.explanation)
    console.print(table)


def main() -> int:
    ap = argparse.ArgumentParser(description="Match a candidate to a JD and tailor a resume.")
    ap.add_argument("--profile-text")
    ap.add_argument("--profile-file")
    ap.add_argument("--jd-text")
    ap.add_argument("--jd-file")
    ap.add_argument("--email", help="Identity hint; overrides the email in the document.")
    ap.add_argument("--phone", help="Fallback identity when no email is available.")
    ap.add_argument("--out", help="Output directory.")
    ap.add_argument(
        "--accept-flags",
        action="store_true",
        help=(
            "Treat every verifier blocker as reviewed and accepted, so the resume "
            "renders anyway. This is the M0 stand-in for the M1 human-review step "
            "(where the user confirms, edits or drops each flagged claim). Use it "
            "to iterate on output quality - never to ship an unreviewed resume."
        ),
    )
    args = ap.parse_args()

    if not (args.profile_text or args.profile_file):
        ap.error("provide --profile-text or --profile-file")
    if not (args.jd_text or args.jd_file):
        ap.error("provide --jd-text or --jd-file")

    if problems := missing_credentials():
        console.print(
            Panel(
                "\n".join(f"• {p}" for p in problems)
                + "\n\n[dim]cp .env.example .env, then fill in the keys.[/]",
                title="Missing API credentials",
                border_style="red",
            )
        )
        return 3

    console.print(
        Panel(
            f"match: [cyan]{settings.model_match}[/]\n"
            f"tailor: [cyan]{settings.model_tailor}[/]\n"
            f"verify: [cyan]{settings.model_verify}[/] "
            f"[dim](different family on purpose)[/]\n"
            f"PII redaction: [cyan]{settings.redact_pii}[/]",
            title="Model routing",
            border_style="blue",
        )
    )

    state = new_state(
        profile_text=args.profile_text,
        profile_file=args.profile_file,
        jd_text=args.jd_text,
        jd_file=args.jd_file,
        email_hint=args.email,
        phone_hint=args.phone,
        out_dir=Path(args.out) if args.out else None,
    )

    try:
        final = PIPELINE.invoke(state)
        # Second pass: the user has reviewed the flags, so resume the run from
        # the render step with them marked resolved. In M1 this is a LangGraph
        # interrupt() resuming from a checkpoint rather than a re-invoke.
        report = final.get("verify_report")
        if args.accept_flags and report and report.blockers:
            console.print(
                f"[yellow]--accept-flags: accepting {len(report.blockers)} blocker(s) "
                "as reviewed and rendering anyway.[/]"
            )
            # Call the render node directly rather than re-invoking the graph:
            # a re-invoke restarts from START, regenerating a *different*
            # resume whose claims no longer match the ones just accepted.
            # M1 replaces this with interrupt() + a checkpointer, which resumes
            # the original run in place.
            resolved = {**final, "resolved_claims": [f.claim for f in report.flags]}
            final = {**resolved, **render_node(resolved)}
    except RenderBlocked as exc:
        console.print(Panel(str(exc), title="Render blocked", border_style="red"))
        return 2

    for err in final.get("errors", []):
        console.print(f"[bold red]error:[/] {err}")
    if final.get("errors"):
        return 1

    graph, job, scorecard = final.get("graph"), final.get("job"), final.get("scorecard")
    if graph and job:
        console.print(
            f"\n[bold]{graph.full_name or '(unnamed)'}[/] · key=[cyan]{graph.identity.primary_key}[/]"
            f" · {len(graph.atoms)} facts · {final.get('years_experience', 0)} yrs"
        )
        console.print(f"[bold]Target:[/] {job.title or '?'} @ {job.company or '?'}\n")

    if scorecard and job:
        print_scorecard(scorecard, job)
        print_verdict(scorecard)
        print_open_questions(scorecard)
        print_gaps(scorecard)

    print_verify(final.get("verify_report"))

    for note in final.get("notes", []):
        if note.startswith("ATS lint:"):
            console.print(f"[yellow]{note}[/]")

    artifacts = final.get("artifacts", {})
    if artifacts:
        console.print("\n[bold]Artifacts[/]")
        for name, path in artifacts.items():
            console.print(f"  {name}: [cyan]{path}[/]")

    ledger = cost_summary()
    if ledger:
        table = Table(title="Token usage", header_style="bold")
        table.add_column("Node")
        table.add_column("Calls", justify="right")
        table.add_column("In", justify="right")
        table.add_column("Out", justify="right")
        for node, entry in ledger.items():
            table.add_row(node, str(entry.calls), str(entry.prompt_tokens), str(entry.completion_tokens))
        console.print(table)

    return 0


if __name__ == "__main__":
    sys.exit(main())

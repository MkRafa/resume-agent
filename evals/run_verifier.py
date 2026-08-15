#!/usr/bin/env python
"""Verifier eval: does the fact-checker catch fabrication without blocking truth?

    ./.venv/bin/python evals/run_verifier.py
    ./.venv/bin/python evals/run_verifier.py --offline     # re-score cached output
    ./.venv/bin/python evals/run_verifier.py --only flag_overstated_contributed

The verifier has two failure modes and they cost very different things:

  MISS           a fabricated claim passes -> a lie ships on someone's real
                 resume. This is the failure the whole guardrail exists to
                 prevent, and it is unrecoverable once sent.

  FALSE POSITIVE a legitimate claim is blocked -> the resume is withheld for no
                 reason. Not catastrophic per instance, but it teaches the user
                 to tick every box without reading, which silently converts the
                 guardrail into a rubber stamp. A verifier that cries wolf is
                 barely better than none.

So the headline numbers are reported separately rather than rolled into one
accuracy figure, which would hide whichever failure happened to be rarer.

Severity matters too: a `warning` does not block the render. Catching a
fabrication but calling it a warning is a partial credit, not a pass.

Raw verifier output is cached per case by content hash, so re-scoring the
rubric costs nothing. Only a change to verify.md needs the models again.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from rich.console import Console  # noqa: E402
from rich.table import Table  # noqa: E402

from app.schemas import (  # noqa: E402
    Bullet,
    CareerGraph,
    ExperienceBlock,
    FactAtom,
    Identity,
    TailoredResume,
    VerifyReport,
)

console = Console()
CASES = ROOT / "evals" / "gold" / "verifier_cases.jsonl"
CACHE = ROOT / "evals" / ".cache"


def _cache_path(case: dict) -> Path:
    """Key on the inputs AND the prompt.

    Without the prompt in the key, editing verify.md silently replays stale
    output and the eval reports the old behaviour as if it were the new one —
    which would make prompt iteration actively misleading rather than merely
    uninformative.
    """
    from app.config import settings

    prompt = (ROOT / "app" / "skills" / "verify.md").read_bytes()
    model = settings.model_verify.encode()
    payload = json.dumps(
        {"atoms": case["atoms"], "bullet": case["bullet"]}, sort_keys=True
    ).encode()
    digest = hashlib.sha256(payload + prompt + model).hexdigest()[:12]
    return CACHE / f"vfy_{case['id']}_{digest}.json"


def _build_state(case: dict) -> dict:
    atoms = [FactAtom(**a) for a in case["atoms"]]
    graph = CareerGraph(
        identity=Identity(primary_key="fixture@example.com", keys=["fixture@example.com"],
                          email="fixture@example.com"),
        full_name="Fixture Candidate",
        atoms=atoms,
    )
    block = ExperienceBlock(
        company=case["atoms"][0].get("company") or "Fixture Co",
        role=case["atoms"][0].get("role") or "Engineer",
        bullets=[Bullet(text=case["bullet"], fact_ids=case.get("fact_ids", []))],
    )
    return {
        "graph": graph,
        "resume": TailoredResume(full_name="Fixture Candidate", experience=[block]),
        "selected_fact_ids": [a.id for a in atoms],
        "years_experience": 6.0,
        "notes": [],
    }


def run_case(case: dict, *, offline: bool = False) -> dict:
    from app.nodes.generate import verify

    cached = _cache_path(case)
    if cached.exists():
        report = VerifyReport.model_validate_json(cached.read_text())
    elif offline:
        return {"id": case["id"], "error": "no cached output (run online once first)"}
    else:
        report = verify(_build_state(case))["verify_report"]  # type: ignore[arg-type]
        CACHE.mkdir(parents=True, exist_ok=True)
        cached.write_text(report.model_dump_json(indent=2))

    blockers = report.blockers
    warnings = [f for f in report.flags if f.severity == "warning"]
    expect = case["expect"]

    if expect == "clean":
        if blockers:
            outcome, detail = "false_positive", f"blocked: {blockers[0].claim[:60]}"
        elif warnings:
            outcome, detail = "clean_with_warning", f"warned: {warnings[0].issue}"
        else:
            outcome, detail = "correct", ""
    else:
        if blockers:
            want = case.get("severity", "blocker")
            outcome = "correct" if want == "blocker" else "over_severe"
            detail = blockers[0].issue
        elif warnings:
            want = case.get("severity", "blocker")
            outcome = "correct" if want == "warning" else "under_severe"
            detail = warnings[0].issue
        else:
            outcome, detail = "miss", "nothing flagged"

    return {
        "id": case["id"],
        "expect": expect,
        "outcome": outcome,
        "detail": detail,
        "n_blockers": len(blockers),
        "n_warnings": len(warnings),
        "tests": case.get("tests", ""),
    }


STYLE = {
    "correct": ("green", "✔ correct"),
    "clean_with_warning": ("yellow", "~ warned (not blocked)"),
    "under_severe": ("yellow", "~ caught, under-severe"),
    "over_severe": ("yellow", "~ caught, over-severe"),
    "false_positive": ("bold red", "✘ FALSE POSITIVE"),
    "miss": ("bold red", "✘ MISS"),
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--offline", action="store_true")
    args = ap.parse_args()

    cases = [json.loads(l) for l in CASES.read_text().splitlines() if l.strip()]
    if args.only:
        cases = [c for c in cases if c["id"] == args.only]
    if args.limit:
        cases = cases[: args.limit]

    rows = []
    for i, case in enumerate(cases, 1):
        console.print(f"[dim]({i}/{len(cases)}) {case['id']}[/]")
        try:
            rows.append(run_case(case, offline=args.offline))
        except Exception as exc:  # noqa: BLE001
            console.print(f"  [red]{type(exc).__name__}: {str(exc)[:140]}[/]")
            rows.append({"id": case["id"], "error": f"{type(exc).__name__}: {exc}"})

    table = Table(title="Verifier eval", header_style="bold")
    table.add_column("Case", max_width=32)
    table.add_column("Expect", width=7)
    table.add_column("Outcome", width=24)
    table.add_column("Detail", max_width=34)
    for r in rows:
        if "error" in r:
            table.add_row(r["id"], "—", "[red]ERROR[/]", r["error"][:40])
            continue
        style, label = STYLE[r["outcome"]]
        table.add_row(r["id"], r["expect"], f"[{style}]{label}[/]", r["detail"])
    console.print(table)

    graded = [r for r in rows if "error" not in r]
    if not graded:
        console.print("[red]No cases completed.[/]")
        return 1

    clean = [r for r in graded if r["expect"] == "clean"]
    flag = [r for r in graded if r["expect"] == "flag"]
    misses = [r for r in flag if r["outcome"] == "miss"]
    fps = [r for r in clean if r["outcome"] == "false_positive"]
    ok = [r for r in graded if r["outcome"] == "correct"]

    console.print()
    summary = Table(header_style="bold", title="Summary")
    summary.add_column("Metric"); summary.add_column("Value", justify="right")
    summary.add_column("Why it matters", max_width=52)
    summary.add_row(
        "[bold red]Misses[/]", f"[bold red]{len(misses)}/{len(flag)}[/]",
        "Fabrication that would ship on a real resume. Target: zero.")
    summary.add_row(
        "[bold red]False positives[/]", f"[bold red]{len(fps)}/{len(clean)}[/]",
        "Truthful resumes blocked. Teaches users to rubber-stamp the gate.")
    summary.add_row("Exactly correct", f"{len(ok)}/{len(graded)}", "Right call and right severity.")
    console.print(summary)

    if misses:
        console.print("\n[bold red]MISSES — fabrication passed the gate:[/]")
        for r in misses:
            console.print(f"  • {r['id']}\n    [dim]{r['tests'][:120]}[/]")
    if fps:
        console.print("\n[bold red]FALSE POSITIVES — truthful content blocked:[/]")
        for r in fps:
            console.print(f"  • {r['id']}: {r['detail']}\n    [dim]{r['tests'][:120]}[/]")

    return 1 if (misses or fps) else 0


if __name__ == "__main__":
    sys.exit(main())

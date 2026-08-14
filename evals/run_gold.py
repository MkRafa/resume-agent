#!/usr/bin/env python
"""Gold-set runner: verdict agreement, with a confusion matrix.

    ./.venv/bin/python evals/run_gold.py            # all cases
    ./.venv/bin/python evals/run_gold.py --only priya_x_meridian_go
    ./.venv/bin/python evals/run_gold.py --limit 5

Two things make this affordable on a free tier:

1. **It stops at the verdict.** Tailoring, verification and rendering are not
   exercised, because verdict agreement is what this eval measures. That is 3
   model calls per case instead of 7.

2. **Extractions and JD parses are cached on disk**, keyed by file content.
   Eight profiles across 22 cases means eight extractions, not 22 - and on a
   re-run after a prompt change to the *grader*, zero. Cache invalidates
   automatically when a fixture's content changes.

Error weighting is asymmetric: calling a weak candidate strong burns one of
their limited applications and their trust in the tool; being too strict is
visible and arguable. Over-generous errors are reported as failures, over-strict
ones as warnings.
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

from app.schemas import CareerGraph, JobSpec, Scorecard  # noqa: E402

console = Console()
GOLD = ROOT / "evals" / "gold" / "verdicts.jsonl"
CACHE = ROOT / "evals" / ".cache"
# Verdicts are written here as each case lands. A run killed at case 15 used to
# lose all 15 results, because the report was only assembled at the end - and on
# a rate-limited free tier, killed runs are the normal case, not the edge case.
RESULTS = ROOT / "evals" / ".cache" / "results.jsonl"

RANK = {"not_matching": 0, "partial_match": 1, "strong_match": 2}
VERDICTS = ["not_matching", "partial_match", "strong_match"]
SHORT = {"not_matching": "no", "partial_match": "partial", "strong_match": "strong"}


def _key(path: Path, kind: str) -> Path:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    return CACHE / f"{kind}_{path.stem}_{digest}.json"


def load_graph(path: Path) -> CareerGraph:
    from app.nodes.extract import build_career_graph
    from app.tools import load_input

    cached = _key(path, "graph")
    if cached.exists():
        return CareerGraph.model_validate_json(cached.read_text())

    state = {"profile_doc": load_input(file=path), "errors": [], "notes": []}
    result = build_career_graph(state)  # type: ignore[arg-type]
    if result.get("errors"):
        raise RuntimeError(f"{path.name}: {result['errors']}")

    graph: CareerGraph = result["graph"]
    CACHE.mkdir(parents=True, exist_ok=True)
    cached.write_text(graph.model_dump_json(indent=2))
    # Years is computed, not stored on the graph - stash it alongside.
    (cached.with_suffix(".years")).write_text(str(result["years_experience"]))
    return graph


def load_years(path: Path) -> float:
    years_file = _key(path, "graph").with_suffix(".years")
    return float(years_file.read_text()) if years_file.exists() else 0.0


def load_job(path: Path) -> JobSpec:
    from app.nodes.extract import parse_jd
    from app.tools import load_input

    cached = _key(path, "job")
    if cached.exists():
        return JobSpec.model_validate_json(cached.read_text())

    state = {"jd_doc": load_input(file=path), "errors": [], "notes": []}
    job: JobSpec = parse_jd(state)["job"]  # type: ignore[arg-type]
    CACHE.mkdir(parents=True, exist_ok=True)
    cached.write_text(job.model_dump_json(indent=2))
    return job


def _rows_cache_path(profile: Path, jd: Path) -> Path:
    digest = hashlib.sha256(profile.read_bytes() + jd.read_bytes()).hexdigest()[:16]
    return CACHE / f"rows_{profile.stem}__{jd.stem}_{digest}.json"


def run_case(case: dict, *, offline: bool = False) -> dict:
    """Grade one pair.

    The model-produced scorecard ROWS are cached separately from the verdict.
    That split matters: the verdict is deterministic Python over those rows, so
    every change to a threshold, to gate handling, or to the unknown/unscorable
    logic can be re-scored across the whole gold set instantly and for free.
    Only a change to the GRADER PROMPT genuinely needs the models again.

    offline=True refuses to make any call and scores whatever rows are cached.
    """
    from app.nodes.matching import match
    from app.tools import compute_verdict

    profile_path = ROOT / case["profile"]
    jd_path = ROOT / case["jd"]

    graph = load_graph(profile_path)
    job = load_job(jd_path)
    years = load_years(profile_path)

    rows_path = _rows_cache_path(profile_path, jd_path)

    if rows_path.exists():
        # Re-score cached grades through the current rule.
        scorecard = Scorecard.model_validate_json(rows_path.read_text())
        scorecard.verdict = None
        compute_verdict(scorecard, job)
    elif offline:
        return {"id": case["id"], "error": "no cached grades (run online once first)"}
    else:
        result = match(
            {  # type: ignore[arg-type]
                "graph": graph,
                "job": job,
                "years_experience": years,
                "errors": [],
                "notes": [],
            }
        )
        if result.get("errors"):
            return {"id": case["id"], "error": result["errors"][0]}
        scorecard = result["scorecard"]
        CACHE.mkdir(parents=True, exist_ok=True)
        rows_path.write_text(scorecard.model_dump_json(indent=2))
    actual = scorecard.verdict
    expected = case["expected_verdict"]
    drift = RANK[actual] - RANK[expected]

    row = {
        "id": case["id"],
        "expected": expected,
        "actual": actual,
        "drift": drift,
        "coverage": scorecard.must_coverage,
        "gates_failed": scorecard.gates_failed,
        "identity": graph.identity.primary_key,
        "reasons": scorecard.verdict_reasons,
    }

    # Did the requirements we said were gaps actually grade weak?
    missed_gaps = []
    for gap in case.get("expected_gaps", []):
        weak = " ".join(
            (job.by_id(r.requirement_id).text if job.by_id(r.requirement_id) else "").lower()
            for r in scorecard.rows
            if r.grade in {"none", "transferable"}
        )
        if gap.lower() not in weak:
            missed_gaps.append(gap)
    row["missed_gaps"] = missed_gaps

    if expected_key := case.get("expected_identity_key"):
        row["identity_ok"] = graph.identity.primary_key == expected_key
    return row


def _load_results() -> dict[str, dict]:
    if not RESULTS.exists():
        return {}
    out: dict[str, dict] = {}
    for line in RESULTS.read_text().splitlines():
        if line.strip():
            row = json.loads(line)
            out[row["id"]] = row  # later entries win, so a re-run overwrites
    return out


def _append_result(row: dict) -> None:
    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    with RESULTS.open("a") as fh:
        fh.write(json.dumps(row) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="Run a single case id.")
    ap.add_argument("--limit", type=int, help="Run at most N cases.")
    ap.add_argument(
        "--resume",
        action="store_true",
        help="Reuse verdicts already recorded in .cache/results.jsonl. Use after a "
        "quota stop so completed cases are not paid for twice.",
    )
    ap.add_argument(
        "--offline",
        action="store_true",
        help="Make no API calls. Re-scores cached grades through the current verdict "
        "rule — use this to iterate thresholds, gate handling and the unknown/"
        "unscorable logic for free. Cases with no cached grades are reported as such.",
    )
    ap.add_argument("--clear-cache", action="store_true")
    ap.add_argument(
        "--clear-results",
        action="store_true",
        help="Drop recorded verdicts. Required after a prompt change, or --resume "
        "will report stale results as if they were fresh.",
    )
    args = ap.parse_args()

    if args.clear_results and RESULTS.exists():
        RESULTS.unlink()
        console.print("[dim]recorded verdicts cleared[/]")

    if args.clear_cache and CACHE.exists():
        for f in CACHE.iterdir():
            f.unlink()
        console.print("[dim]cache cleared[/]")

    cases = [json.loads(line) for line in GOLD.read_text().splitlines() if line.strip()]
    if args.only:
        cases = [c for c in cases if c["id"] == args.only]
    if args.limit:
        cases = cases[: args.limit]

    from app.models import QuotaExhausted

    done = _load_results() if args.resume else {}
    if done:
        console.print(f"[dim]resuming: {len(done)} case(s) already recorded[/]")

    rows: list[dict] = []
    for i, case in enumerate(cases, 1):
        if case["id"] in done:
            rows.append(done[case["id"]])
            console.print(f"[dim]({i}/{len(cases)}) {case['id']} — cached[/]")
            continue

        console.print(f"[dim]({i}/{len(cases)}) {case['id']}[/]")
        try:
            row = run_case(case, offline=args.offline)
        except QuotaExhausted as exc:
            # Stopping here is the point: the remaining cases would each burn
            # minutes rediscovering the same 429. Everything so far is on disk.
            console.print(f"\n[bold red]Quota exhausted — stopping at case {i}.[/]")
            console.print(f"[dim]{exc}[/]")
            console.print(
                f"[yellow]{len(rows)} result(s) saved. Re-run with --resume when the "
                "quota window reopens; completed cases will not be re-charged.[/]\n"
            )
            break
        except Exception as exc:  # noqa: BLE001 - a dead case must not kill the run
            console.print(f"  [red]failed: {type(exc).__name__}: {str(exc)[:160]}[/]")
            row = {"id": case["id"], "error": f"{type(exc).__name__}: {exc}"}

        rows.append(row)
        _append_result(row)

    _report(rows)
    graded = [r for r in rows if "error" not in r]
    over_generous = [r for r in graded if r["drift"] > 0]
    return 1 if over_generous or len(graded) < len(rows) else 0


def _report(rows: list[dict]) -> None:
    table = Table(title="Gold-set results", header_style="bold")
    table.add_column("Case", max_width=28)
    table.add_column("Expected", width=8)
    table.add_column("Actual", width=8)
    table.add_column("", width=14)
    table.add_column("Cov", width=5, justify="right")
    table.add_column("Notes", max_width=40)

    for r in rows:
        if "error" in r:
            table.add_row(r["id"], "—", "[red]ERROR[/]", "[red]run failed[/]", "—", r["error"][:60])
            continue
        drift = r["drift"]
        if drift == 0:
            mark, style = "✔ match", "green"
        elif drift > 0:
            mark, style = f"✘ generous +{drift}", "bold red"
        else:
            mark, style = f"~ strict {drift}", "yellow"

        notes = []
        if r.get("missed_gaps"):
            notes.append(f"gap not flagged: {', '.join(r['missed_gaps'])}")
        if r.get("identity_ok") is False:
            notes.append(f"identity key wrong: {r['identity']}")
        if r["gates_failed"]:
            notes.append(f"gates failed: {','.join(r['gates_failed'])}")

        table.add_row(
            r["id"],
            SHORT[r["expected"]],
            SHORT[r["actual"]],
            f"[{style}]{mark}[/]",
            f"{r['coverage']:.0%}",
            "; ".join(notes) or "",
        )
    console.print(table)

    graded = [r for r in rows if "error" not in r]
    if not graded:
        console.print("[red]No cases completed.[/]")
        return

    matrix = Table(title="Confusion matrix (rows = expected, cols = actual)", header_style="bold")
    matrix.add_column("expected \\ actual")
    for v in VERDICTS:
        matrix.add_column(SHORT[v], justify="right", width=8)
    for exp in VERDICTS:
        cells = []
        for act in VERDICTS:
            n = sum(1 for r in graded if r["expected"] == exp and r["actual"] == act)
            if n == 0:
                cells.append("[dim]·[/]")
            elif exp == act:
                cells.append(f"[green]{n}[/]")
            elif RANK[act] > RANK[exp]:
                cells.append(f"[bold red]{n}[/]")
            else:
                cells.append(f"[yellow]{n}[/]")
        matrix.add_row(SHORT[exp], *cells)
    console.print(matrix)

    exact = sum(1 for r in graded if r["drift"] == 0)
    generous = [r for r in graded if r["drift"] > 0]
    strict = [r for r in graded if r["drift"] < 0]
    console.print(
        f"\n[bold]Agreement: {exact}/{len(graded)} ({exact / len(graded):.0%})[/]  "
        f"[red]over-generous: {len(generous)}[/]  "
        f"[yellow]over-strict: {len(strict)}[/]  "
        f"[dim]errored: {len(rows) - len(graded)}[/]"
    )
    if generous:
        console.print(
            "[bold red]Over-generous cases are the expensive ones — they send the "
            "candidate into an application they cannot win:[/]"
        )
        for r in generous:
            console.print(f"  • {r['id']}: expected {r['expected']}, got {r['actual']}")
            for reason in r["reasons"]:
                console.print(f"      [dim]{reason}[/]")


if __name__ == "__main__":
    sys.exit(main())

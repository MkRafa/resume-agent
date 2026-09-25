#!/usr/bin/env python
"""Seed a demo database with finished runs, so a live demo never waits on a model.

    ./.venv/bin/python scripts/seed_demo.py --fresh
    RESUME_AGENT_DB=data/demo.db ./.venv/bin/uvicorn app.web:app --port 8000

Free-tier models are slow and often overloaded; a demo that runs the pipeline
live can stall for minutes. Seeding does the model work ahead of time, into a
separate database (data/demo.db by default), so the demo clicks through real,
finished results. Your working store is never touched.

Every run is a real pipeline run over synthetic gold-set fixtures, with one
declared exception: the review-gate demo plants ONE fabricated claim into a
real tailored resume - clearly labelled in the run's title - and the real
verifier has to catch it. Nothing else is staged. If the verifier misses the
plant, the script says so rather than faking a flag.

Costs roughly 23 model calls in all; --only reruns a single demo.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("RESUME_AGENT_DB", "data/demo.db")

from app import runner, store  # noqa: E402
from app.nodes import generate  # noqa: E402
from app.schemas import TailoredResume  # noqa: E402

GOLD = ROOT / "evals" / "gold"
WORKING_DB = ROOT / "data" / "resume_agent.db"

PLANTED_LABEL = "demo: planted fabrication"
# Appended to one real bullet. A metric is the most damaging fabrication a
# resume can carry, and the most checkable - the verifier should block it, and
# the claim trace would independently report the number as unsourced.
PLANTED_CLAIM = ", cutting cloud infrastructure spend by 38%"

# (label, profile fixture, JD fixture, plant a fabrication?)
DEMOS = [
    ("not matching", "profiles/kavya_pm.md", "jds/meridian_go_payments.md", False),
    ("partial match", "profiles/priya_backend.md", "jds/meridian_go_payments.md", False),
    ("strong match", "profiles/sana_platform.md", "jds/platform_sre.md", False),
    ("review gate", "profiles/meera_data.md", "jds/data_platform.md", True),
]


def _run(profile: str, jd: str) -> str:
    run_id = store.create_run()
    runner._execute(
        run_id,
        profile_text=None,
        profile_file=str(GOLD / profile),
        jd_text=None,
        jd_file=str(GOLD / jd),
        email=None,
        phone=None,
    )
    return run_id


def _run_with_plant(profile: str, jd: str) -> tuple[str, str | None]:
    """A real run, except one bullet of the real tailored resume gets
    PLANTED_CLAIM appended before the verifier sees it."""
    real = generate.complete_json
    planted: list[str] = []

    def plant(schema, **kwargs):
        result = real(schema, **kwargs)
        if schema is TailoredResume and not planted:
            for block in result.experience:
                if block.bullets:
                    bullet = block.bullets[0]
                    bullet.text = bullet.text.rstrip(".") + PLANTED_CLAIM + "."
                    planted.append(bullet.text)
                    break
        return result

    generate.complete_json = plant
    try:
        run_id = _run(profile, jd)
    finally:
        generate.complete_json = real

    run = store.get_run(run_id)
    title = run["jd_title"] or "Job match"
    store.update_run(
        run_id,
        jd_title=f"{title} ({PLANTED_LABEL})",
        notes_json=[
            *run["notes"],
            f"DEMO: one fabricated claim was planted into this resume: {planted[0] if planted else '(none - no bullets)'}",
        ],
    )
    return run_id, planted[0] if planted else None


def _copy_run(source: Path, run_id: str) -> bool:
    """Copy a finished run - and its profile - from another store."""
    if not source.exists():
        return False
    with store.connect() as conn:
        conn.execute("ATTACH DATABASE ? AS src", (str(source),))
        found = conn.execute("SELECT profile_key FROM src.runs WHERE id = ?", (run_id,)).fetchone()
        if not found:
            return False
        # Columns by name, never SELECT *: a migrated store has graph_json at
        # the end of `runs`, a fresh one has it mid-table.
        def copy(table: str, where: str, value: str, verb: str) -> None:
            cols = ", ".join(r["name"] for r in conn.execute(f"PRAGMA main.table_info({table})"))
            conn.execute(
                f"{verb} INTO main.{table} ({cols}) SELECT {cols} FROM src.{table} WHERE {where} = ?",
                (value,),
            )

        copy("runs", "id", run_id, "INSERT OR REPLACE")
        if found["profile_key"]:
            copy("profiles", "key", found["profile_key"], "INSERT OR IGNORE")
            copy("profile_keys", "profile_key", found["profile_key"], "INSERT OR IGNORE")
    return True


def _report(label: str, run_id: str) -> dict:
    run = store.get_run(run_id)
    print(f"  {label:13} run {run_id}  status={run['status']:12} verdict={run['verdict']}")
    if run["status"] == "failed":
        print(f"                {run['error']}")
    return run


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--fresh", action="store_true", help="Delete the demo database first.")
    ap.add_argument(
        "--copy-run",
        metavar="RUN_ID",
        help="Also copy this finished run (and its profile) from your working store, "
        "instead of paying for another live run.",
    )
    ap.add_argument(
        "--only",
        choices=[label for label, *_ in DEMOS],
        help="Seed just this demo (adds to the existing demo database).",
    )
    args = ap.parse_args()

    if store.DB_PATH.resolve() == WORKING_DB.resolve():
        sys.exit("Refusing to seed the working store. Set RESUME_AGENT_DB to a demo path.")
    if args.fresh:
        for suffix in ("", "-wal", "-shm"):
            Path(f"{store.DB_PATH}{suffix}").unlink(missing_ok=True)
    store.init_db()
    print(f"Seeding {store.DB_PATH.relative_to(ROOT)}")

    ok = True
    if args.copy_run:
        if _copy_run(WORKING_DB, args.copy_run):
            _report("copied", args.copy_run)
        else:
            print(f"  copied        run {args.copy_run} not found in {WORKING_DB.name}")
            ok = False

    for label, profile, jd, planted in DEMOS:
        if args.only and label != args.only:
            continue
        if planted:
            run_id, claim = _run_with_plant(profile, jd)
            run = _report(label, run_id)
            caught = bool(run["verify"]) and any(
                "38%" in f.claim or "cloud infrastructure spend" in f.claim.lower()
                for f in run["verify"].blockers
            )
            if run["status"] == "needs_review" and caught:
                print(f"                verifier blocked the planted claim: {claim}")
            else:
                print("                WARNING: the verifier did not block the planted claim - "
                      "no review gate to show. Re-run, or inspect the run.")
                ok = False
        else:
            run = _report(label, _run(profile, jd))
            ok = ok and run["status"] != "failed"

    print("\nServe it with:\n  RESUME_AGENT_DB=data/demo.db ./.venv/bin/uvicorn app.web:app --port 8000")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

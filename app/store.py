"""Persistence. SQLite for the prototype; the schema is Postgres-shaped.

The data model is multi-tenant from day one even though there is no login yet:
profiles are keyed by the identity rule (email primary, phone fallback) and
alternate keys resolve to the same profile. Adding auth later is a wrapper over
this, not a migration of it.

The `applications` table is unused today on purpose. Outcome data - did this
application get a reply? - is the thing you will most wish you had been
collecting from the first run, and it costs nothing to write the table now.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from app.config import settings
from app.schemas import CareerGraph, Identity, JobSpec, Scorecard, TailoredResume, VerifyReport
from app.tools.identity import merge_identities

DB_PATH = settings.root / "data" / "resume_agent.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS profiles (
    key         TEXT PRIMARY KEY,          -- the identity primary_key
    full_name   TEXT,
    graph_json  TEXT NOT NULL,
    years       REAL NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

-- Alternate lookup keys (email AND phone both point at one profile) so a later
-- upload supplying only one of them reconciles instead of forking a new profile.
CREATE TABLE IF NOT EXISTS profile_keys (
    key         TEXT PRIMARY KEY,
    profile_key TEXT NOT NULL REFERENCES profiles(key) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS runs (
    id             TEXT PRIMARY KEY,
    profile_key    TEXT,
    status         TEXT NOT NULL,          -- queued|running|needs_review|done|failed
    stage          TEXT,                   -- human-readable current step
    jd_title       TEXT,
    jd_company     TEXT,
    verdict        TEXT,
    job_json       TEXT,
    scorecard_json TEXT,
    resume_json    TEXT,
    verify_json    TEXT,
    resolved_json  TEXT,
    artifacts_json TEXT,
    notes_json     TEXT,
    error          TEXT,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runs_profile ON runs(profile_key, created_at DESC);

-- Deliberately unused in M1. Outcome data is what tells you whether your
-- verdicts are honest, and you cannot backfill it.
CREATE TABLE IF NOT EXISTS applications (
    id          TEXT PRIMARY KEY,
    run_id      TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    applied_at  TEXT,
    outcome     TEXT,                      -- ghosted|screen|onsite|offer|rejected
    noted_at    TEXT
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # The web request thread and the worker thread both write; WAL keeps a
    # reader from blocking the worker mid-run.
    conn.execute("PRAGMA journal_mode = WAL")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)


# --------------------------------------------------------------------------
# profiles
# --------------------------------------------------------------------------


def find_profile(identity: Identity) -> tuple[str, CareerGraph, float] | None:
    """Resolve any of this identity's keys to a stored profile."""
    with connect() as conn:
        for key in identity.keys:
            row = conn.execute(
                "SELECT p.key, p.graph_json, p.years FROM profile_keys pk "
                "JOIN profiles p ON p.key = pk.profile_key WHERE pk.key = ?",
                (key,),
            ).fetchone()
            if row:
                return row["key"], CareerGraph.model_validate_json(row["graph_json"]), row["years"]
    return None


def save_profile(graph: CareerGraph, years: float) -> str:
    """Insert or update, merging identities so alternate keys accumulate."""
    existing = find_profile(graph.identity)
    if existing:
        primary, stored, _ = existing
        graph.identity = merge_identities(stored.identity, graph.identity)
    else:
        primary = graph.identity.primary_key

    now = _now()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO profiles (key, full_name, graph_json, years, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                full_name = excluded.full_name,
                graph_json = excluded.graph_json,
                years = excluded.years,
                updated_at = excluded.updated_at
            """,
            (primary, graph.full_name, graph.model_dump_json(), years, now, now),
        )
        for key in graph.identity.keys:
            conn.execute(
                "INSERT OR REPLACE INTO profile_keys (key, profile_key) VALUES (?, ?)",
                (key, primary),
            )
    return primary


def get_profile(key: str) -> tuple[CareerGraph, float] | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT graph_json, years FROM profiles WHERE key = ?", (key,)
        ).fetchone()
    if not row:
        return None
    return CareerGraph.model_validate_json(row["graph_json"]), row["years"]


def list_profiles() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT key, full_name, years, updated_at, "
            "(SELECT COUNT(*) FROM runs r WHERE r.profile_key = p.key) AS run_count "
            "FROM profiles p ORDER BY updated_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------
# runs
# --------------------------------------------------------------------------


def create_run() -> str:
    run_id = uuid.uuid4().hex[:12]
    now = _now()
    with connect() as conn:
        conn.execute(
            "INSERT INTO runs (id, status, stage, created_at, updated_at) VALUES (?,?,?,?,?)",
            (run_id, "queued", "Queued", now, now),
        )
    return run_id


def update_run(run_id: str, **fields: Any) -> None:
    """Patch a run. Pydantic models and containers are JSON-encoded automatically."""
    if not fields:
        return
    encoded: dict[str, Any] = {}
    for key, value in fields.items():
        if value is None or isinstance(value, (str, int, float)):
            encoded[key] = value
        elif hasattr(value, "model_dump_json"):
            encoded[key] = value.model_dump_json()
        else:
            encoded[key] = json.dumps(value)
    encoded["updated_at"] = _now()

    assignments = ", ".join(f"{k} = ?" for k in encoded)
    with connect() as conn:
        conn.execute(f"UPDATE runs SET {assignments} WHERE id = ?", (*encoded.values(), run_id))


def get_run(run_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    if not row:
        return None

    run = dict(row)
    run["job"] = JobSpec.model_validate_json(run["job_json"]) if run["job_json"] else None
    run["scorecard"] = (
        Scorecard.model_validate_json(run["scorecard_json"]) if run["scorecard_json"] else None
    )
    run["resume"] = (
        TailoredResume.model_validate_json(run["resume_json"]) if run["resume_json"] else None
    )
    run["verify"] = (
        VerifyReport.model_validate_json(run["verify_json"]) if run["verify_json"] else None
    )
    run["resolved"] = json.loads(run["resolved_json"]) if run["resolved_json"] else []
    run["artifacts"] = json.loads(run["artifacts_json"]) if run["artifacts_json"] else {}
    run["notes"] = json.loads(run["notes_json"]) if run["notes_json"] else []
    return run


def list_runs(profile_key: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    query = (
        "SELECT id, profile_key, status, jd_title, jd_company, verdict, created_at "
        "FROM runs {where} ORDER BY created_at DESC LIMIT ?"
    )
    with connect() as conn:
        if profile_key:
            rows = conn.execute(
                query.format(where="WHERE profile_key = ?"), (profile_key, limit)
            ).fetchall()
        else:
            rows = conn.execute(query.format(where=""), (limit,)).fetchall()
    return [dict(r) for r in rows]

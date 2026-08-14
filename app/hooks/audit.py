"""Provenance audit log: every generated bullet -> the atoms it came from.

Powers the provenance UI in M2, and is the record you would want if a user ever
disputed something the system wrote on their behalf.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from app.schemas import CareerGraph, TailoredResume


def audit_provenance(resume: TailoredResume, graph: CareerGraph) -> list[dict]:
    """Build bullet -> fact edges, marking any bullet with no traceable source."""
    edges: list[dict] = []
    for block in resume.experience:
        for i, bullet in enumerate(block.bullets):
            known = [fid for fid in bullet.fact_ids if graph.by_id(fid)]
            edges.append(
                {
                    "location": f"{block.company}/{block.role}#{i}",
                    "text": bullet.text,
                    "fact_ids": bullet.fact_ids,
                    "resolved_fact_ids": known,
                    "orphan": not known,
                }
            )
    return edges


def write_audit_log(
    out_dir: Path,
    *,
    resume: TailoredResume,
    graph: CareerGraph,
    extra: dict | None = None,
) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "provenance.json"
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "profile_key": graph.identity.primary_key,
        "edges": audit_provenance(resume, graph),
        **(extra or {}),
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path

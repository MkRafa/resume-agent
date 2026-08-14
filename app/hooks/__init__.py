"""Middleware around every model call.

Kept as plain functions rather than a framework abstraction so the call path in
models.py stays readable end to end.
"""

from app.hooks.audit import audit_provenance, write_audit_log
from app.hooks.cost import cost_summary, log_cost
from app.hooks.guardrail import RenderBlocked, block_on_unresolved_flags
from app.hooks.pii import redact, restore

__all__ = [
    "RenderBlocked",
    "audit_provenance",
    "block_on_unresolved_flags",
    "cost_summary",
    "log_cost",
    "redact",
    "restore",
    "write_audit_log",
]

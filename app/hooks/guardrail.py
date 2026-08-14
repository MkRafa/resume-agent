"""The render gate.

No PDF is produced while the verifier has unresolved blockers. This is a hard
structural gate rather than a policy someone remembers to check, because the
failure mode it prevents - shipping a fabricated claim into a real job
application - is the one failure this product cannot afford.
"""

from __future__ import annotations

from app.schemas import VerifyReport


class RenderBlocked(RuntimeError):
    def __init__(self, report: VerifyReport):
        self.report = report
        detail = "\n".join(f"  - [{f.issue}] {f.claim} ({f.location})" for f in report.blockers)
        super().__init__(
            f"Render blocked: {len(report.blockers)} unresolved claim(s) could not be "
            f"traced to the career graph.\n{detail}\n"
            "Resolve each one (confirm it as a fact, edit it, or drop it) and re-run."
        )


def block_on_unresolved_flags(report: VerifyReport | None, *, resolved: set[str] | None = None):
    """Raise unless every blocker has been explicitly resolved by the user."""
    if report is None:
        return
    resolved = resolved or set()
    outstanding = [f for f in report.blockers if f.claim not in resolved]
    if outstanding:
        raise RenderBlocked(VerifyReport(flags=outstanding))

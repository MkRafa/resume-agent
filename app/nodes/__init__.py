"""Pipeline nodes.

Module names deliberately avoid colliding with the functions they export
(`matching.match`, `rendering.render`). A module named `match.py` exporting a
function named `match` gets shadowed in this namespace, which makes the module
unpatchable in tests and unimportable by path.
"""

from app.nodes.extract import build_career_graph, parse_jd
from app.nodes.generate import select_facts, tailor, verify
from app.nodes.intake import intake_jd, intake_profile
from app.nodes.matching import gap_report, match
from app.nodes.rendering import ats_lint, render, render_html

__all__ = [
    "ats_lint",
    "build_career_graph",
    "gap_report",
    "intake_jd",
    "intake_profile",
    "match",
    "parse_jd",
    "render",
    "render_html",
    "select_facts",
    "tailor",
    "verify",
]

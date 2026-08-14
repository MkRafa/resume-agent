"""The pipeline.

Deterministic edges throughout. This is not an agent loop and should not become
one - a resume pipeline that takes a different path on each run is a bug. The
only genuinely agentic component in the design is the enrichment interviewer,
which is an M2 addition and lives behind the `enrich` interrupt point.

    intake_profile ─┐
                    ├─> build_career_graph ─┐
    intake_jd ──────┘                       ├─> match ─> route
                    └─> parse_jd ───────────┘             │
                                                          │
        not_matching ──> gap_report ──> END               │
        partial ───────> gap_report ──> select_facts ─────┤
        strong ────────────────────────> select_facts ────┘
                                              │
                                           tailor
                                              │
                                           verify
                                              │
                                    [blockers?] ──yes──> END (render blocked)
                                              │no
                                           render ──> END
"""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from app.nodes import (
    build_career_graph,
    gap_report,
    intake_jd,
    intake_profile,
    match,
    parse_jd,
    render,
    select_facts,
    tailor,
    verify,
)
from app.state import PipelineState


def _has_errors(state: PipelineState) -> bool:
    return bool(state.get("errors"))


def route_after_match(state: PipelineState) -> str:
    """Every verdict gets a gap report; only matches proceed to generation."""
    if _has_errors(state):
        return "abort"
    scorecard = state.get("scorecard")
    if scorecard is None:
        return "abort"
    return "gap_report" if scorecard.verdict != "strong_match" else "generate"


def route_after_gaps(state: PipelineState) -> str:
    scorecard = state.get("scorecard")
    if scorecard is None or scorecard.verdict == "not_matching":
        return "stop"
    return "generate"


def route_after_verify(state: PipelineState) -> str:
    """In M0 an unresolved blocker stops the run and reports.

    In M1 this becomes a LangGraph `interrupt()`: checkpoint, surface the flags
    to the user for confirm/edit/drop, then resume into render. The routing
    shape is already correct for that swap.
    """
    report = state.get("verify_report")
    if report is None:
        return "render"
    resolved = set(state.get("resolved_claims", []))
    outstanding = [f for f in report.blockers if f.claim not in resolved]
    return "blocked" if outstanding else "render"


def build_graph():
    g = StateGraph(PipelineState)

    g.add_node("intake_profile", intake_profile)
    g.add_node("intake_jd", intake_jd)
    g.add_node("build_career_graph", build_career_graph)
    g.add_node("parse_jd", parse_jd)
    g.add_node("match", match)
    g.add_node("gap_report", gap_report)
    g.add_node("select_facts", select_facts)
    g.add_node("tailor", tailor)
    g.add_node("verify", verify)
    g.add_node("render", render)

    # The two sides are independent - fan out, then join at match.
    #
    # Both branches use plain edges into `match` so LangGraph waits for both
    # before firing it. An earlier version routed each branch conditionally to
    # END on error, which broke the join: the healthy branch still triggered
    # `match`, which then read a key the failed branch never wrote. The abort
    # check therefore lives at the join and immediately after it, not upstream.
    g.add_edge(START, "intake_profile")
    g.add_edge(START, "intake_jd")
    g.add_edge("intake_profile", "build_career_graph")
    g.add_edge("intake_jd", "parse_jd")
    g.add_edge("build_career_graph", "match")
    g.add_edge("parse_jd", "match")

    g.add_conditional_edges(
        "match",
        route_after_match,
        {"gap_report": "gap_report", "generate": "select_facts", "abort": END},
    )
    g.add_conditional_edges(
        "gap_report",
        route_after_gaps,
        {"generate": "select_facts", "stop": END},
    )

    g.add_edge("select_facts", "tailor")
    g.add_edge("tailor", "verify")
    g.add_conditional_edges(
        "verify",
        route_after_verify,
        {"render": "render", "blocked": END},
    )
    g.add_edge("render", END)

    return g.compile()


PIPELINE = build_graph()

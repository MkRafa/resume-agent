"""Per-node token and latency accounting.

You want this from day one. The first time a bill or a rate limit appears, the
question is always "which node?", and retrofitting the answer means re-running
everything.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from app.config import settings


@dataclass
class NodeCost:
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    models: set[str] = field(default_factory=set)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


_LEDGER: dict[str, NodeCost] = defaultdict(NodeCost)


def log_cost(node: str, model: str, response) -> None:
    usage = getattr(response, "usage", None)
    entry = _LEDGER[node]
    entry.calls += 1
    entry.models.add(model)
    if usage:
        entry.prompt_tokens += getattr(usage, "prompt_tokens", 0) or 0
        entry.completion_tokens += getattr(usage, "completion_tokens", 0) or 0

    if settings.log_costs and usage:
        print(
            f"  [cost] {node:<16} {model:<34} "
            f"in={getattr(usage, 'prompt_tokens', 0)} out={getattr(usage, 'completion_tokens', 0)}"
        )


def cost_summary() -> dict[str, NodeCost]:
    return dict(_LEDGER)


def reset_costs() -> None:
    _LEDGER.clear()

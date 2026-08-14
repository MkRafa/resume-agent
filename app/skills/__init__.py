"""Skills: versioned prompt modules.

A skill is (instruction, output schema, model binding, eval set) versioned
together. These are the highest-churn artifacts in the system - when quality
moves, it moves because a prompt here changed - so they live apart from the
node plumbing and each carries its own eval fixtures.

Loaded from sibling .md files so prompts can be edited without touching Python.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

SKILL_DIR = Path(__file__).parent


@lru_cache(maxsize=None)
def load(name: str) -> str:
    path = SKILL_DIR / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(f"No skill prompt at {path}")
    return path.read_text(encoding="utf-8").strip()

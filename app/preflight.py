"""Credential preflight.

Checked before the graph runs so a missing key is a one-line message instead of
a LiteLLM stack trace surfacing from inside a node three steps in.
"""

from __future__ import annotations

import os

from app.config import settings

# provider prefix -> (env vars, where to get one)
PROVIDERS: dict[str, tuple[tuple[str, ...], str]] = {
    "gemini": (("GEMINI_API_KEY", "GOOGLE_API_KEY"), "https://aistudio.google.com/apikey"),
    "groq": (("GROQ_API_KEY",), "https://console.groq.com/keys"),
    "anthropic": (("ANTHROPIC_API_KEY",), "https://console.anthropic.com/settings/keys"),
    "openai": (("OPENAI_API_KEY",), "https://platform.openai.com/api-keys"),
    "openrouter": (("OPENROUTER_API_KEY",), "https://openrouter.ai/keys"),
    "cerebras": (("CEREBRAS_API_KEY",), "https://cloud.cerebras.ai"),
    "mistral": (("MISTRAL_API_KEY",), "https://console.mistral.ai/api-keys"),
    # ollama runs locally and needs no key
}


def missing_credentials() -> list[str]:
    """One human-readable line per node whose provider has no key configured."""
    problems: list[str] = []
    seen: set[str] = set()

    for node in ("extract", "parse", "match", "tailor", "verify"):
        model = settings.model_for(node)
        provider = model.split("/", 1)[0].lower()
        if provider == "ollama" or provider in seen:
            continue

        entry = PROVIDERS.get(provider)
        if entry is None:
            continue  # unknown provider: let LiteLLM decide
        env_vars, url = entry
        if not any(os.getenv(v) for v in env_vars):
            seen.add(provider)
            problems.append(
                f"{provider}: set {' or '.join(env_vars)} in .env "
                f"(free key: {url}) — needed by the '{node}' node ({model})"
            )
    return problems

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    # Per-node model routing. Any LiteLLM-supported id.
    model_extract: str = field(
        default_factory=lambda: os.getenv("MODEL_EXTRACT", "gemini/gemini-3.5-flash")
    )
    model_parse: str = field(
        default_factory=lambda: os.getenv("MODEL_PARSE", "gemini/gemini-3.5-flash")
    )
    model_match: str = field(
        default_factory=lambda: os.getenv("MODEL_MATCH", "gemini/gemini-3.5-flash")
    )
    model_tailor: str = field(
        default_factory=lambda: os.getenv("MODEL_TAILOR", "gemini/gemini-3.5-flash")
    )
    # Deliberately a different family from model_tailor: a model asked to check
    # its own output shares its own blind spots.
    model_verify: str = field(
        default_factory=lambda: os.getenv("MODEL_VERIFY", "groq/openai/gpt-oss-120b")
    )

    phone_default_region: str = field(
        default_factory=lambda: os.getenv("PHONE_DEFAULT_REGION", "IN")
    )
    redact_pii: bool = field(default_factory=lambda: _bool("REDACT_PII", True))
    log_costs: bool = field(default_factory=lambda: _bool("LOG_COSTS", True))

    root: Path = ROOT
    data_dir: Path = ROOT / "data"
    out_dir: Path = ROOT / "data" / "out"
    # Web uploads. Transient: deleted once their run has read them.
    uploads_dir: Path = ROOT / "data" / "uploads"

    # Comma-separated fallback chain per node. Free tiers shed load constantly,
    # so a second option is the difference between a run finishing and a run
    # dying three nodes in. Same provider family is fine here - this is
    # availability failover, not the verifier's cross-family independence.
    fallbacks: str = field(
        default_factory=lambda: os.getenv(
            "MODEL_FALLBACKS",
            "gemini/gemini-3.6-flash,gemini/gemini-flash-latest",
        )
    )

    # The verifier's own failover, off by default. Kept separate from the shared
    # chain because it must stay out of the tailorer's family - a model in the
    # same family checking the resume shares its blind spots. Anything in that
    # family is dropped from this list rather than silently used.
    verify_fallbacks: str = field(
        default_factory=lambda: os.getenv("MODEL_VERIFY_FALLBACKS", "")
    )

    def model_for(self, node: str) -> str:
        return {
            "extract": self.model_extract,
            "parse": self.model_parse,
            "match": self.model_match,
            "tailor": self.model_tailor,
            "verify": self.model_verify,
        }.get(node, self.model_extract)

    def models_for(self, node: str) -> list[str]:
        """Primary model followed by the fallback chain, deduplicated.

        Independence between the tailorer and the verifier is guarded in both
        directions. The verifier fails over only to MODEL_VERIFY_FALLBACKS
        outside the tailorer's family, never to the shared chain; and the
        tailorer never fails over to a model in the verifier's family -
        otherwise a rate-limited Gemini quietly hands the writing to the same
        model that then checks it.

        Only fallbacks are filtered. A primary MODEL_TAILOR in the verifier's
        family is an explicit choice (e.g. all-local Ollama) and is respected.
        """
        primary = self.model_for(node)
        if node == "verify":
            writer = model_family(self.model_tailor)
            own = [m.strip() for m in self.verify_fallbacks.split(",") if m.strip()]
            return list(dict.fromkeys([primary, *(m for m in own if model_family(m) != writer)]))
        fallbacks = [m.strip() for m in self.fallbacks.split(",") if m.strip()]
        if node == "tailor":
            verifier = model_family(self.model_verify)
            fallbacks = [m for m in fallbacks if model_family(m) != verifier]
        return list(dict.fromkeys([primary, *fallbacks]))


# Substrings that identify a model family regardless of which provider serves
# it: groq/llama-3.3-70b and openrouter/meta-llama/llama-3.1-8b are both Llama.
_FAMILIES = ("gemini", "gemma", "llama", "gpt", "claude", "mistral", "mixtral",
             "qwen", "deepseek", "kimi", "command")


def model_family(model: str) -> str:
    """Family of a LiteLLM model id; the full id when the family is unknown,
    so an unrecognised model only ever collides with itself."""
    name = model.split("/", 1)[-1].lower()
    return next((f for f in _FAMILIES if f in name), model.lower())


settings = Settings()

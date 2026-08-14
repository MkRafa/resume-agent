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
        default_factory=lambda: os.getenv("MODEL_EXTRACT", "gemini/gemini-2.0-flash")
    )
    model_parse: str = field(
        default_factory=lambda: os.getenv("MODEL_PARSE", "gemini/gemini-2.0-flash")
    )
    model_match: str = field(
        default_factory=lambda: os.getenv("MODEL_MATCH", "gemini/gemini-2.0-flash")
    )
    model_tailor: str = field(
        default_factory=lambda: os.getenv("MODEL_TAILOR", "gemini/gemini-2.0-flash")
    )
    # Deliberately a different family from model_tailor: a model asked to check
    # its own output shares its own blind spots.
    model_verify: str = field(
        default_factory=lambda: os.getenv("MODEL_VERIFY", "groq/llama-3.3-70b-versatile")
    )

    phone_default_region: str = field(
        default_factory=lambda: os.getenv("PHONE_DEFAULT_REGION", "IN")
    )
    redact_pii: bool = field(default_factory=lambda: _bool("REDACT_PII", True))
    log_costs: bool = field(default_factory=lambda: _bool("LOG_COSTS", True))

    root: Path = ROOT
    data_dir: Path = ROOT / "data"
    out_dir: Path = ROOT / "data" / "out"

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

        The verifier is excluded from the Gemini fallbacks on purpose: failing
        it over to the same family as the tailorer would silently discard the
        independence the whole verification pass depends on.
        """
        primary = self.model_for(node)
        if node == "verify":
            return [primary]
        chain = [primary, *(m.strip() for m in self.fallbacks.split(",") if m.strip())]
        return list(dict.fromkeys(chain))


settings = Settings()

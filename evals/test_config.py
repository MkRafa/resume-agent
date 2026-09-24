"""Model routing: failover must never collapse the tailorer into the verifier.

Regression for a config that did exactly that: MODEL_FALLBACKS listed
groq/llama-3.3-70b-versatile - the verifier's own model - so whenever Gemini
was rate-limited, one model wrote the resume and then fact-checked it.
"""

from __future__ import annotations

from app.config import Settings, model_family

VERIFY = "groq/llama-3.3-70b-versatile"
FALLBACKS = (
    "gemini/gemini-flash-lite-latest,groq/openai/gpt-oss-120b,"
    "groq/llama-3.3-70b-versatile,openrouter/meta-llama/llama-3.1-8b-instruct"
)


def _settings(**overrides) -> Settings:
    s = Settings()
    s.model_tailor = "gemini/gemini-3.5-flash"
    s.model_match = "gemini/gemini-3.5-flash"
    s.model_verify = VERIFY
    s.fallbacks = FALLBACKS
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


def test_tailor_never_fails_over_to_the_verifier_family():
    chain = _settings().models_for("tailor")
    assert all(model_family(m) != "llama" for m in chain), chain
    # Other families on the verifier's provider are still usable.
    assert "groq/openai/gpt-oss-120b" in chain


def test_other_nodes_keep_the_full_chain():
    chain = _settings().models_for("match")
    assert VERIFY in chain


def test_verifier_never_fails_over():
    assert _settings().models_for("verify") == [VERIFY]


def test_explicit_same_family_primary_is_respected():
    """All-local Ollama puts every node on one model. That is a documented,
    deliberate choice - only silent failover is prevented."""
    s = _settings(model_tailor="ollama/qwen2.5:7b", model_verify="ollama/qwen2.5:7b",
                  fallbacks="ollama/qwen2.5:7b")
    assert s.models_for("tailor") == ["ollama/qwen2.5:7b"]


def test_model_family():
    assert model_family("groq/llama-3.3-70b-versatile") == "llama"
    assert model_family("openrouter/meta-llama/llama-3.1-8b") == "llama"
    assert model_family("gemini/gemini-3.5-flash") == "gemini"
    assert model_family("groq/openai/gpt-oss-120b") == "gpt"
    assert model_family("acme/unknown-model") == "acme/unknown-model"

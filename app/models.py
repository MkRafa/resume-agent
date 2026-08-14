"""Model routing and the JSON-with-retry call path.

Every model call in the system goes through `complete_json`. That gives us one
place to hang the hooks: PII redaction before the call, schema validation and
retry after it, cost logging around it.

Prompt layout is deliberate:

    [system] [career graph  <- stable] [job-specific  <- varies]

The career graph is identical across every application a single candidate
makes, so keeping it first and unchanged makes it the cacheable prefix. On
providers with prompt caching that turns applications 2..n into a fraction of
the cost of the first.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from app.config import settings
from app.hooks import log_cost, redact, restore

T = TypeVar("T", bound=BaseModel)

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)

# Free tiers shed load and rate-limit aggressively, so transport failures are
# routine rather than exceptional. They get their own retry policy: exponential
# backoff, then a different model entirely. Schema failures are a separate
# concern - those retry by feeding the validation error back to the model.
TRANSIENT_MARKERS = (
    "429", "500", "502", "503", "504",
    "rate limit", "ratelimit", "overloaded", "high demand",
    "unavailable", "timeout", "timed out", "try again",
)
TRANSPORT_ATTEMPTS = 4
BACKOFF_BASE = 3.0
MAX_BACKOFF = 90.0

# Providers tell you how long to wait; guessing shorter just burns the retry.
# Gemini returns {"retryDelay": "21s"} on a 429, and the default exponential
# schedule (1+2+4s) gave up well before the quota window reopened.
_RETRY_DELAY = re.compile(r'"?retry(?:_|-)?delay"?\s*[:=]\s*"?(\d+(?:\.\d+)?)s', re.IGNORECASE)


def _is_transient(exc: Exception) -> bool:
    text = f"{type(exc).__name__} {exc}".lower()
    return any(marker in text for marker in TRANSIENT_MARKERS)


def _suggested_delay(exc: Exception) -> float | None:
    if m := _RETRY_DELAY.search(str(exc)):
        return min(float(m.group(1)) + 1.0, MAX_BACKOFF)
    return None


class ModelCallError(RuntimeError):
    pass


class QuotaExhausted(ModelCallError):
    """Every model is rate-limited and retrying is not going to help today.

    Distinct from a transient failure. When a whole fallback chain 429s on
    consecutive calls, the retry loop stops being resilience and becomes a very
    slow way to fail: 4 rounds x ~60s per call, repeated per case. A 22-case
    eval spent 274 retries to land 1 successful call before this existed.
    """


class _QuotaTracker:
    """Trips after N consecutive rate-limit failures, PER PROVIDER, with a cooldown.

    Two properties this needs that the obvious implementation lacks:

    1. **Per provider.** A global latch means an exhausted Gemini key also blocks
       Groq and local Ollama calls, which have nothing to do with it. Switching
       MODEL_* to a working provider then appears to change nothing.

    2. **Self-clearing.** A latch that refuses to call cannot observe a success,
       so it can never reset itself - it stays tripped for the life of the
       process even after the quota window reopens. The cooldown lets one probe
       through periodically to find out.
    """

    LIMIT = 2
    COOLDOWN_SECONDS = 120.0

    def __init__(self) -> None:
        self._fails: dict[str, int] = {}
        self._last_fail: dict[str, float] = {}

    @staticmethod
    def provider_of(model: str) -> str:
        return model.split("/", 1)[0].lower()

    def record_failure(self, model: str, exc: Exception) -> None:
        provider = self.provider_of(model)
        text = str(exc).lower()
        if any(m in text for m in ("429", "quota", "rate limit", "ratelimit", "resource_exhausted")):
            self._fails[provider] = self._fails.get(provider, 0) + 1
            self._last_fail[provider] = time.monotonic()
        else:
            self._fails[provider] = 0

    def record_success(self, model: str) -> None:
        self._fails[self.provider_of(model)] = 0

    def _provider_tripped(self, provider: str) -> bool:
        if self._fails.get(provider, 0) < self.LIMIT:
            return False
        # Cooldown elapsed: let one probe through to see if the window reopened.
        if time.monotonic() - self._last_fail.get(provider, 0.0) > self.COOLDOWN_SECONDS:
            self._fails[provider] = self.LIMIT - 1
            return False
        return True

    def all_tripped(self, models: list[str]) -> bool:
        """Only skip when EVERY provider in the chain is known-exhausted."""
        providers = {self.provider_of(m) for m in models}
        return bool(providers) and all(self._provider_tripped(p) for p in providers)


QUOTA = _QuotaTracker()


def _extract_json(text: str) -> str:
    """Pull JSON out of a response that may be fenced or have prose around it."""
    text = text.strip()
    if m := _FENCE.search(text):
        return m.group(1).strip()
    # Fall back to the outermost brace pair.
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        return text[start : end + 1]
    return text


# Output ceilings, per node.
#
# Two opposing constraints, both learned the hard way:
#   - Too low: a full career graph runs to ~10k output tokens and the provider
#     default truncated it mid-array, surfacing as "EOF while parsing a list".
#   - Too high: Groq bills max_tokens against your tokens-per-minute budget, so
#     a blanket 32k made every verify call fail with "Request too large"
#     (limit 12000, requested 34537) before it even ran.
# So size each node to what it actually emits.
MAX_TOKENS_BY_NODE = {
    "extract": 32000,  # whole career graph
    "parse": 16000,    # requirement list
    "match": 16000,    # one row per requirement
    "tailor": 12000,   # the resume
    "verify": 4000,    # a handful of flags; must stay under Groq's TPM cap
}
DEFAULT_MAX_TOKENS = 16000


def _raw_call(
    model: str,
    messages: list[dict[str, Any]],
    *,
    json_mode: bool,
    temperature: float,
    max_tokens: int = DEFAULT_MAX_TOKENS,
):
    import litellm

    litellm.drop_params = True  # providers vary in what they accept
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    return litellm.completion(**kwargs)


def _call_with_failover(
    node: str,
    models: list[str],
    messages: list[dict[str, Any]],
    *,
    json_mode: bool,
    temperature: float,
    max_tokens: int = DEFAULT_MAX_TOKENS,
):
    """Rotate across models first, then back off and go round again.

    Free-tier quotas are per-model ("limit: 20, model: gemini-3.7-flash"), so
    when one is exhausted a sibling is usually available immediately. Sleeping
    on the primary before trying an alternative - the obvious ordering - wastes
    a minute to learn something the next model would have answered at once.

    So: one full pass over every model with no sleeping, and only if the entire
    pass fails do we wait (honouring the provider's own retryDelay) and repeat.
    Models that fail non-transiently (retired, bad request) drop out for good.
    """
    last: Exception | None = None
    live = list(models)

    # Fail fast once the whole chain is demonstrably out of quota. Without
    # this, every subsequent call still pays the full retry schedule to
    # rediscover the same 429, which reads to the user as "slow" rather than
    # "stopped".
    if QUOTA.all_tripped(models):
        raise QuotaExhausted(
            f"[{node}] skipped: every provider in this chain ({', '.join(models)}) "
            "returned rate-limit errors on recent calls. Wait for the quota window "
            "to reset, add a different provider to MODEL_FALLBACKS, or switch to "
            "local Ollama models."
        )

    for round_no in range(TRANSPORT_ATTEMPTS):
        dead: list[str] = []

        for model in live:
            try:
                response = _raw_call(
                    model,
                    messages,
                    json_mode=json_mode,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
                QUOTA.record_success(model)
                return response, model
            except Exception as exc:  # noqa: BLE001 - provider exceptions vary widely
                last = exc
                QUOTA.record_failure(model, exc)
                if not _is_transient(exc):
                    print(f"  [warn] {node}: {model} unusable ({type(exc).__name__}); dropping")
                    dead.append(model)
                else:
                    print(f"  [retry] {node}: {model} {type(exc).__name__}; trying next model")

        live = [m for m in live if m not in dead]
        if not live:
            break
        if round_no < TRANSPORT_ATTEMPTS - 1:
            delay = _suggested_delay(last) if last else None
            delay = delay or min(BACKOFF_BASE**round_no, MAX_BACKOFF)
            print(
                f"  [retry] {node}: all {len(live)} model(s) busy, "
                f"round {round_no + 1}/{TRANSPORT_ATTEMPTS}, sleeping {delay:.0f}s"
            )
            time.sleep(delay)

    raise ModelCallError(
        f"[{node}] all models exhausted ({', '.join(models)}). Last error: {last}"
    )


def complete_json(
    schema: type[T],
    *,
    node: str,
    system: str,
    stable_context: str = "",
    variable_context: str = "",
    temperature: float = 0.2,
    max_retries: int = 2,
) -> T:
    """Call the node's model and parse into `schema`, retrying on invalid output.

    The retry feeds the validation error back to the model, which resolves the
    large majority of parse failures on the second attempt.
    """
    models = settings.models_for(node)
    max_tokens = MAX_TOKENS_BY_NODE.get(node, DEFAULT_MAX_TOKENS)

    system_prompt = (
        f"{system}\n\n"
        "Respond with a single JSON object and nothing else. It must validate "
        f"against this JSON Schema:\n{json.dumps(schema.model_json_schema(), indent=2)}"
    )

    user_parts = []
    if stable_context:
        user_parts.append(stable_context)
    if variable_context:
        user_parts.append(variable_context)
    user_prompt = "\n\n".join(user_parts)

    placeholders: dict[str, str] = {}
    if settings.redact_pii:
        user_prompt, placeholders = redact(user_prompt)

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    last_error: Exception | None = None
    for _ in range(max_retries):
        response, model = _call_with_failover(
            node,
            models,
            messages,
            json_mode=True,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        log_cost(node, model, response)
        raw = response.choices[0].message.content or ""
        if settings.redact_pii:
            raw = restore(raw, placeholders)

        try:
            return schema.model_validate_json(_extract_json(raw))
        except (ValidationError, json.JSONDecodeError) as exc:
            last_error = exc
            messages.extend(
                [
                    {"role": "assistant", "content": raw[:4000]},
                    {
                        "role": "user",
                        "content": (
                            "That output failed schema validation with:\n"
                            f"{exc}\n\nReturn corrected JSON only."
                        ),
                    },
                ]
            )

    raise ModelCallError(
        f"[{node}] produced schema-invalid output after {max_retries} attempts "
        f"(models tried: {', '.join(models)}): {last_error}"
    )

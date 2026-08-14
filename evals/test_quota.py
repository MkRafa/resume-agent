"""Quota tracker: fail fast without latching the process shut.

Regression suite for a bug that made the app permanently unusable: the tracker
was a single global counter, so an exhausted Gemini key blocked Ollama calls
too, and because a tripped tracker refuses to make a call it could never see a
success to reset itself.
"""

from __future__ import annotations

import pytest

from app.models import _QuotaTracker

GEMINI = ["gemini/gemini-3.7-flash", "gemini/gemini-3.6-flash"]
OLLAMA = ["ollama/qwen2.5:7b"]
RATE_LIMIT = RuntimeError('{"code": 429, "message": "You exceeded your current quota"}')
OTHER = RuntimeError("NotFoundError: model no longer available")


def trip(tracker: _QuotaTracker, models: list[str]) -> None:
    for _ in range(tracker.LIMIT):
        for m in models:
            tracker.record_failure(m, RATE_LIMIT)


def test_trips_after_repeated_rate_limits():
    t = _QuotaTracker()
    assert not t.all_tripped(GEMINI)
    trip(t, GEMINI)
    assert t.all_tripped(GEMINI)


def test_one_provider_exhausted_does_not_block_another():
    """The deadlock. A dead Gemini key must not stop local Ollama calls —
    otherwise switching MODEL_* to a working provider appears to do nothing."""
    t = _QuotaTracker()
    trip(t, GEMINI)
    assert t.all_tripped(GEMINI)
    assert not t.all_tripped(OLLAMA)


def test_mixed_chain_runs_while_any_provider_is_live():
    t = _QuotaTracker()
    trip(t, GEMINI)
    assert not t.all_tripped([*GEMINI, *OLLAMA])


def test_cooldown_lets_a_probe_through():
    """A tripped tracker never calls, so it can never observe a success. Without
    a cooldown it stays tripped for the life of the process even after the
    provider's quota window reopens."""
    t = _QuotaTracker()
    t.COOLDOWN_SECONDS = 0.0
    trip(t, GEMINI)
    assert not t.all_tripped(GEMINI), "cooldown should allow a probe"


def test_probe_failure_retrips_immediately():
    t = _QuotaTracker()
    t.COOLDOWN_SECONDS = 0.0
    trip(t, GEMINI)
    t.all_tripped(GEMINI)  # consumes the probe allowance
    for m in GEMINI:
        t.record_failure(m, RATE_LIMIT)
    t.COOLDOWN_SECONDS = 120.0
    assert t.all_tripped(GEMINI)


def test_success_clears_the_provider():
    t = _QuotaTracker()
    trip(t, GEMINI)
    t.record_success("gemini/gemini-3.7-flash")
    t.record_success("gemini/gemini-3.6-flash")
    assert not t.all_tripped(GEMINI)


def test_non_rate_limit_errors_do_not_trip_it():
    """A retired model id is not a quota problem; failing over is the fix."""
    t = _QuotaTracker()
    for _ in range(5):
        for m in GEMINI:
            t.record_failure(m, OTHER)
    assert not t.all_tripped(GEMINI)


def test_empty_chain_is_not_tripped():
    assert not _QuotaTracker().all_tripped([])


@pytest.mark.parametrize(
    "model,provider",
    [
        ("gemini/gemini-3.7-flash", "gemini"),
        ("groq/llama-3.3-70b-versatile", "groq"),
        ("ollama/qwen2.5:7b", "ollama"),
    ],
)
def test_provider_extraction(model, provider):
    assert _QuotaTracker.provider_of(model) == provider

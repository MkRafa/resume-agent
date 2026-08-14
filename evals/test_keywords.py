"""Keyword coverage and the stuffing detector."""

from app.tools.keywords import keyword_coverage

VOCAB = ["Go", "Kubernetes", "Kafka", "Postgres", "payments", "gRPC"]


def test_covered_and_missing_are_partitioned():
    text = "Built payment rails in Go on Kubernetes with Kafka."
    cov = keyword_coverage(text, VOCAB)
    assert set(cov.covered) == {"Go", "Kubernetes", "Kafka"}
    assert set(cov.missing) == {"Postgres", "payments", "gRPC"}
    assert cov.ratio == 0.5


def test_matching_is_word_bounded():
    """'Go' must not match inside 'Django' or 'going'."""
    cov = keyword_coverage("Built a Django service, going fast.", ["Go"])
    assert cov.covered == []
    assert cov.missing == ["Go"]


def test_dotted_and_plus_terms_survive_tokenisation():
    cov = keyword_coverage("Wrote C++ and .NET services", ["c++", ".net"])
    assert set(cov.covered) == {"c++", ".net"}


def test_stopwords_are_ignored():
    cov = keyword_coverage("anything", ["the", "and", "experience", "Kafka"])
    assert cov.missing == ["Kafka"]
    assert "the" not in cov.covered + cov.missing


def test_short_document_does_not_trigger_stuffing():
    """Two mentions in a 40-word draft is 5% density and completely normal.
    Flagging it made the linter cry wolf on every short resume."""
    text = "Used Kafka for the settlement queue. Kafka replaced the sync write."
    assert keyword_coverage(text, ["Kafka"]).stuffed == []


def test_real_stuffing_is_caught():
    filler = "engineer built service platform delivered system improved process team " * 20
    text = filler + " Kafka " * 12
    cov = keyword_coverage(text, ["Kafka"])
    assert cov.stuffed == ["Kafka"]


def test_normal_repetition_in_a_long_resume_is_not_stuffing():
    filler = "engineer built service platform delivered system improved process team " * 30
    text = filler + " Kafka Kafka Kafka "
    assert keyword_coverage(text, ["Kafka"]).stuffed == []


def test_empty_vocabulary_is_safe():
    cov = keyword_coverage("anything at all", [])
    assert cov.ratio == 0.0
    assert cov.covered == [] and cov.missing == []

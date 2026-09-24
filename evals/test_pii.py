"""PII handling around model calls. No API calls."""

from __future__ import annotations

from app.hooks.pii import reapply, redact, restore


def test_contact_details_are_redacted_and_restored():
    text = "Priya · priya@example.com · +91 98765 43210 · github.com/example-priya"
    out, mapping = redact(text)
    assert "priya@example.com" not in out and "98765" not in out and "github.com" not in out
    assert restore(out, mapping) == text


def test_runs_of_years_are_not_mistaken_for_phones():
    """Regression: "2018 2019 2020 2021" matched the phone pattern and was sent
    as <PHONE_1>, hiding dates the model needs."""
    for text in ("Active 2018 2019 2020 2021", "Dates: 2019 - 2021 - 2023"):
        assert redact(text)[0] == text


def test_reapply_is_the_inverse_of_restore():
    out, mapping = redact("mail a@example.com or b@example.com")
    restored = restore(out, mapping)
    assert reapply(restored, mapping) == out


def test_tailor_prompt_does_not_carry_the_name(monkeypatch):
    """Names cannot be pattern-redacted, so the one prompt that sent the name
    after extraction - the tailorer's - no longer does. The rendered resume
    still carries it, set from the graph."""
    from app.nodes import generate
    from app.schemas import (
        CareerGraph, FactAtom, Identity, JobSpec, Requirement, Scorecard, TailoredResume,
    )

    seen: dict[str, str] = {}

    def fake(schema, **kwargs):
        seen["prompt"] = kwargs.get("stable_context", "") + kwargs.get("variable_context", "")
        return TailoredResume(full_name="Someone Else")

    monkeypatch.setattr(generate, "complete_json", fake)
    graph = CareerGraph(identity=Identity(primary_key="a@example.com", keys=["a@example.com"]),
                        full_name="Priya Raghavan",
                        atoms=[FactAtom(id="f_001", type="achievement", raw_text="Built X.")])
    job = JobSpec(requirements=[Requirement(id="r_01", kind="must", category="skill", text="X")])
    out = generate.tailor({"graph": graph, "job": job, "scorecard": Scorecard(),
                           "selected_fact_ids": ["f_001"]})

    assert "Priya" not in seen["prompt"] and "Raghavan" not in seen["prompt"]
    assert out["resume"].full_name == "Priya Raghavan"

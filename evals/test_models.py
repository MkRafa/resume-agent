"""complete_json: the schema-retry path must not undo PII redaction.

Regression for a leak: on a validation failure the model's output was restored
to real values and then appended to the retry conversation, so the second
request carried the candidate's email and phone to the provider in clear.
"""

from __future__ import annotations

from types import SimpleNamespace

from pydantic import BaseModel

import app.models as models

EMAIL = "priya.raghavan@example.com"


class Contact(BaseModel):
    email: str
    count: int


def _response(content: str):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))], usage=None
    )


def test_retry_does_not_resend_restored_pii(monkeypatch):
    monkeypatch.setattr(models.settings, "redact_pii", True)
    sent: list[list[dict]] = []
    replies = iter(
        [
            # Invalid: the placeholder lands in an int field, so the validation
            # error quotes the (restored) value it rejected.
            '{"email": "<EMAIL_1>", "count": "<EMAIL_1>"}',
            '{"email": "<EMAIL_1>", "count": 3}',
        ]
    )

    def fake_call(node, models_, messages, **kwargs):
        sent.append([dict(m) for m in messages])
        return _response(next(replies)), "stub/model"

    monkeypatch.setattr(models, "_call_with_failover", fake_call)

    result = models.complete_json(
        Contact, node="parse", system="Extract.", variable_context=f"Contact: {EMAIL}"
    )

    assert result.email == EMAIL  # restored for the caller
    assert len(sent) == 2
    for call in sent:
        for message in call:
            assert EMAIL not in message["content"], message
    # The retry still explains what went wrong, in placeholder form.
    assert "<EMAIL_1>" in sent[1][-1]["content"]

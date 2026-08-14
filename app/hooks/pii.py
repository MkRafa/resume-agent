"""PII redaction around model calls.

The model does not need a real name, email, phone or street address to grade
evidence or write a bullet. Swapping them for placeholders before the request
and restoring afterwards cuts exposure substantially - which matters a great
deal on free tiers, where inputs are generally used to improve the provider's
models.

This is a mitigation, not a compliance story. Employment history is itself
identifying. Before real users, move to a provider tier with no-training terms.
"""

from __future__ import annotations

import re

EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b")
PHONE_RE = re.compile(r"(?<![\w])(?:\+\d{1,3}[\s-]?)?(?:\(?\d{3,5}\)?[\s.-]?){2,4}\d{2,4}(?![\w])")
URL_RE = re.compile(r"https?://\S+|(?:www\.|linkedin\.com/|github\.com/)\S+", re.IGNORECASE)


def redact(text: str) -> tuple[str, dict[str, str]]:
    """Replace PII with stable placeholders. Returns (redacted, mapping)."""
    mapping: dict[str, str] = {}
    counters = {"EMAIL": 0, "PHONE": 0, "URL": 0}

    def swap(kind: str):
        def _sub(match: re.Match[str]) -> str:
            original = match.group(0)
            for placeholder, value in mapping.items():
                if value == original:
                    return placeholder
            counters[kind] += 1
            placeholder = f"<{kind}_{counters[kind]}>"
            mapping[placeholder] = original
            return placeholder

        return _sub

    # URLs first: they frequently contain the email-looking or numeric spans
    # the later patterns would otherwise chew up.
    out = URL_RE.sub(swap("URL"), text)
    out = EMAIL_RE.sub(swap("EMAIL"), out)
    out = PHONE_RE.sub(swap("PHONE"), out)
    return out, mapping


def restore(text: str, mapping: dict[str, str]) -> str:
    for placeholder, original in mapping.items():
        text = text.replace(placeholder, original)
    return text

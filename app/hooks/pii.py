"""PII redaction around model calls.

The model does not need a real name, email, phone or street address to grade
evidence or write a bullet. Swapping contact details for placeholders before
the request and restoring afterwards cuts exposure substantially - which matters a great
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


# A run of years ("2018 2019 2020 2021") satisfies PHONE_RE's digit-group
# shape. Redacting it would hide dates the model needs and restore nothing.
_ONLY_YEARS = re.compile(r"(?:(?:19|20)\d{2}[\s.\-–]*)+")


def redact(text: str) -> tuple[str, dict[str, str]]:
    """Replace PII with stable placeholders. Returns (redacted, mapping).

    Emails, phone numbers and URLs only. Names are not pattern-matchable, so
    they are kept out of prompts at the source instead: after extraction the
    name lives on the graph and no later prompt includes it.
    """
    mapping: dict[str, str] = {}
    counters = {"EMAIL": 0, "PHONE": 0, "URL": 0}

    def swap(kind: str):
        def _sub(match: re.Match[str]) -> str:
            original = match.group(0)
            if kind == "PHONE" and _ONLY_YEARS.fullmatch(original.strip()):
                return original
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


def reapply(text: str, mapping: dict[str, str]) -> str:
    """Inverse of `restore`: swap known originals back to their placeholders.

    For anything derived from restored output that is about to be sent to a
    provider again - a validation error quotes the input it rejected, so it can
    carry the real values. Longest originals first, so one that contains
    another is not half-replaced.
    """
    for placeholder, original in sorted(mapping.items(), key=lambda kv: -len(kv[1])):
        text = text.replace(original, placeholder)
    return text

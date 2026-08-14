"""Profile identity resolution.

Rule: email is the primary key; phone is the fallback when no email is given.

Amendment to the naive rule: we retain BOTH values as alternate lookup keys and
match on any of them. Without this, a user who uploads with an email in January
and with only a phone in March silently forks into two profiles - and the second
one starts with an empty career graph, discarding the enrichment that makes the
product good. Forked profiles are extremely hard to notice and extremely
annoying to merge after the fact.
"""

from __future__ import annotations

import re

import phonenumbers

from app.config import settings
from app.schemas import Identity

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class NeedsIdentity(ValueError):
    """Raised when a profile supplies neither an email nor a phone number."""


def normalize_email(raw: str | None) -> str | None:
    """Lowercase + trim only.

    Deliberately does NOT canonicalise Gmail dots or strip +tags. Those rules
    are provider-specific and getting them wrong merges two different people,
    which is far worse than failing to merge one person's two addresses.
    """
    if not raw:
        return None
    candidate = raw.strip().lower()
    # Tolerate 'mailto:' and surrounding angle brackets from PDF text layers.
    candidate = candidate.removeprefix("mailto:").strip("<>").strip()
    return candidate if EMAIL_RE.match(candidate) else None


def normalize_phone(raw: str | None, region: str | None = None) -> str | None:
    """Parse to E.164. Returns None if the number is not valid for the region."""
    if not raw:
        return None
    region = region or settings.phone_default_region
    cleaned = re.sub(r"[^\d+]", "", raw.strip())
    if not cleaned:
        return None
    try:
        parsed = phonenumbers.parse(cleaned, None if cleaned.startswith("+") else region)
    except phonenumbers.NumberParseException:
        return None
    if not phonenumbers.is_valid_number(parsed):
        return None
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


def resolve_identity(
    email: str | None,
    phone: str | None,
    region: str | None = None,
) -> Identity:
    """Build the profile identity. Email wins as primary; phone is the fallback."""
    e = normalize_email(email)
    p = normalize_phone(phone, region)

    if not e and not p:
        raise NeedsIdentity(
            "A profile needs a valid email address, or a valid phone number as fallback. "
            f"Got email={email!r}, phone={phone!r}."
        )

    primary = e or p
    assert primary is not None  # guaranteed by the check above
    keys = [k for k in (e, p) if k]
    return Identity(primary_key=primary, keys=keys, email=e, phone=p)


def lookup_keys(identity: Identity) -> list[str]:
    """Every key that should resolve to this profile. Query stores with all of them."""
    return identity.keys


def merge_identities(existing: Identity, incoming: Identity) -> Identity:
    """Union two identities that resolved to the same profile.

    The existing primary_key wins so stored references stay stable; newly seen
    contact details are added as additional lookup keys.
    """
    keys = list(dict.fromkeys([*existing.keys, *incoming.keys]))
    return Identity(
        primary_key=existing.primary_key,
        keys=keys,
        email=existing.email or incoming.email,
        phone=existing.phone or incoming.phone,
    )

"""Identity resolution: email primary, phone fallback, both retained as keys."""

import pytest

from app.schemas import Identity
from app.tools.identity import (
    NeedsIdentity,
    merge_identities,
    normalize_email,
    normalize_phone,
    resolve_identity,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("  Priya.Raghavan@Example.COM ", "priya.raghavan@example.com"),
        ("mailto:a@b.co", "a@b.co"),
        ("<a@b.co>", "a@b.co"),
        ("not-an-email", None),
        ("", None),
        (None, None),
    ],
)
def test_normalize_email(raw, expected):
    assert normalize_email(raw) == expected


def test_gmail_dots_are_not_canonicalised():
    """Deliberate: provider-specific canonicalisation risks merging two people,
    which is far worse than failing to merge one person's two addresses."""
    assert normalize_email("m.k@gmail.com") != normalize_email("mk@gmail.com")


@pytest.mark.parametrize(
    "raw,region,expected",
    [
        ("+91 98765 43210", "IN", "+919876543210"),
        ("98765 43210", "IN", "+919876543210"),
        ("(415) 555-2671", "US", "+14155552671"),
        ("12345", "IN", None),
        ("", "IN", None),
    ],
)
def test_normalize_phone(raw, region, expected):
    assert normalize_phone(raw, region) == expected


def test_email_is_primary_when_both_present():
    ident = resolve_identity("a@b.co", "+91 98765 43210")
    assert ident.primary_key == "a@b.co"
    assert set(ident.keys) == {"a@b.co", "+919876543210"}


def test_phone_is_the_fallback():
    ident = resolve_identity(None, "+91 98765 43210")
    assert ident.primary_key == "+919876543210"
    assert ident.keys == ["+919876543210"]


def test_invalid_email_falls_through_to_phone():
    ident = resolve_identity("garbage", "+91 98765 43210")
    assert ident.primary_key == "+919876543210"


def test_neither_raises():
    with pytest.raises(NeedsIdentity):
        resolve_identity(None, None)
    with pytest.raises(NeedsIdentity):
        resolve_identity("nope", "123")


def test_both_values_are_lookup_keys():
    """The reason profiles don't silently fork: a later upload supplying only
    the phone still resolves to the profile created with the email."""
    january = resolve_identity("a@b.co", "+91 98765 43210")
    march = resolve_identity(None, "+91 98765 43210")
    assert march.primary_key in january.keys


def test_merge_keeps_existing_primary():
    existing = Identity(primary_key="a@b.co", keys=["a@b.co"], email="a@b.co")
    incoming = resolve_identity(None, "+91 98765 43210")
    merged = merge_identities(existing, incoming)
    assert merged.primary_key == "a@b.co"
    assert set(merged.keys) == {"a@b.co", "+919876543210"}
    assert merged.phone == "+919876543210"

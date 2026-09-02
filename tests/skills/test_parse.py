"""Skill parsing and validation — no database (C5.1).

Everything a document must satisfy before it is ever stored: the frontmatter
shape, the name/version rules, the size cap, and a stable content address. These
run everywhere, with or without Postgres.
"""

from __future__ import annotations

import pytest

from purse.db.repo import content_hash
from purse.skills.errors import PayloadTooLargeError, ValidationError
from purse.skills.parse import MAX_CONTENT_BYTES, parse_skill

VALID = """\
---
name: purse-save-policy
description: What is worth saving to memory.
version: 1.2.3
updated_at: 2026-09-01T00:00:00Z
---
# Body

Save durable facts.
"""


def test_parse_valid_document_splits_frontmatter_and_body() -> None:
    parsed = parse_skill(VALID)
    assert parsed.name == "purse-save-policy"
    assert parsed.description == "What is worth saving to memory."
    assert parsed.version == "1.2.3"
    # A YAML timestamp is parsed and re-emitted in normalised ISO form (Z → +00:00).
    assert parsed.updated_at == "2026-09-01T00:00:00+00:00"
    assert parsed.body.startswith("# Body")
    assert "Save durable facts." in parsed.body
    # The frontmatter fence and its fields are not part of the body.
    assert "name:" not in parsed.body
    assert parsed.frontmatter["name"] == "purse-save-policy"


def test_content_hash_is_sha256_of_the_whole_document_and_stable() -> None:
    parsed = parse_skill(VALID)
    assert parsed.content == VALID
    assert parsed.content_hash == content_hash(VALID)
    # Stable across parses.
    assert parse_skill(VALID).content_hash == parsed.content_hash


def test_content_hash_changes_with_any_content_change() -> None:
    other = VALID.replace("Save durable facts.", "Save durable facts!!")
    assert parse_skill(other).content_hash != parse_skill(VALID).content_hash


def test_quoted_updated_at_string_is_preserved_verbatim() -> None:
    # Quoted, so YAML keeps it a string; a plain ISO string passes through as-is.
    doc = (
        "---\nname: dated\ndescription: d\nversion: 1.0.0\n"
        'updated_at: "2026-09-01T00:00:00Z"\n---\nbody\n'
    )
    assert parse_skill(doc).updated_at == "2026-09-01T00:00:00Z"


def test_invalid_updated_at_string_is_validation() -> None:
    doc = '---\nname: dated\ndescription: d\nversion: 1.0.0\nupdated_at: "not-a-date"\n---\nbody\n'
    with pytest.raises(ValidationError):
        parse_skill(doc)


def test_updated_at_is_optional() -> None:
    doc = """\
---
name: no-timestamp
description: A skill without updated_at.
version: 0.1.0
---
body
"""
    parsed = parse_skill(doc)
    assert parsed.updated_at is None
    assert "updated_at" not in parsed.frontmatter


def test_yaml_date_timestamp_is_normalised_to_a_json_safe_string() -> None:
    # An unquoted YAML timestamp parses to a datetime; it must not reach the
    # jsonb column as a datetime object.
    doc = """\
---
name: dated
description: Has a bare timestamp.
version: 1.0.0
updated_at: 2026-01-02T03:04:05
---
body
"""
    parsed = parse_skill(doc)
    assert isinstance(parsed.frontmatter["updated_at"], str)
    assert parsed.updated_at == "2026-01-02T03:04:05"


@pytest.mark.parametrize("missing", ["name", "description", "version"])
def test_missing_required_field_is_validation(missing: str) -> None:
    lines = [
        "---",
        "name: ok-name",
        "description: A description.",
        "version: 1.0.0",
        "---",
        "body",
    ]
    kept = [line for line in lines if not line.startswith(f"{missing}:")]
    with pytest.raises(ValidationError):
        parse_skill("\n".join(kept))


def test_no_frontmatter_is_validation() -> None:
    with pytest.raises(ValidationError):
        parse_skill("# Just markdown, no frontmatter\n")


def test_unclosed_frontmatter_is_validation() -> None:
    with pytest.raises(ValidationError):
        parse_skill("---\nname: x\ndescription: y\nversion: 1.0.0\nbody without a closing fence\n")


def test_malformed_yaml_is_validation() -> None:
    with pytest.raises(ValidationError):
        parse_skill("---\nname: [unterminated\n---\nbody\n")


@pytest.mark.parametrize("version", ["1.0", "1", "1.0.0.0", "v1.0.0", "1.0.x", "1.2.3-rc1", ""])
def test_bad_semver_is_validation(version: str) -> None:
    doc = f"---\nname: skill\ndescription: d\nversion: {version or '""'}\n---\nbody\n"
    with pytest.raises(ValidationError):
        parse_skill(doc)


@pytest.mark.parametrize("name", ["Bad-Name", "-leading", "under_score", "space name", "café", ""])
def test_non_kebab_name_is_validation(name: str) -> None:
    doc = f"---\nname: {name or '""'}\ndescription: d\nversion: 1.0.0\n---\nbody\n"
    with pytest.raises(ValidationError):
        parse_skill(doc)


@pytest.mark.parametrize("name", ["skill", "a", "purse-save-policy", "a1-b2-c3", "web3-tools"])
def test_valid_kebab_names_are_accepted(name: str) -> None:
    doc = f"---\nname: {name}\ndescription: d\nversion: 1.0.0\n---\nbody\n"
    assert parse_skill(doc).name == name


def test_oversized_document_is_payload_too_large() -> None:
    header = "---\nname: big\ndescription: d\nversion: 1.0.0\n---\n"
    body = "a" * (MAX_CONTENT_BYTES + 1)
    with pytest.raises(PayloadTooLargeError):
        parse_skill(header + body)


def test_document_exactly_at_the_cap_is_accepted() -> None:
    header = "---\nname: big\ndescription: d\nversion: 1.0.0\n---\n"
    body = "a" * (MAX_CONTENT_BYTES - len(header.encode("utf-8")))
    parsed = parse_skill(header + body)
    assert len(parsed.content.encode("utf-8")) == MAX_CONTENT_BYTES


# ---------------------------------------------------------------------------
# The cap is bytes of UTF-8, not characters
# ---------------------------------------------------------------------------
#
# The ASCII-filler tests above cannot distinguish "64 KB of bytes" from "64K
# characters" because for ASCII the two counts are identical. A multibyte filler
# makes that distinction observable: one character costs more than one byte, so
# a body sized in *characters* to hit the cap would land on the wrong side of it
# if the check were counting characters instead of encoded bytes.

_BIG_HEADER = "---\nname: big\ndescription: d\nversion: 1.0.0\n---\n"


def _document_of_exact_size(total_bytes: int, filler: str) -> str:
    """A document whose UTF-8-encoded size is exactly *total_bytes*.

    The body is packed with *filler* (a multibyte character) as far as it goes,
    then topped up with single-byte padding so the total lands on the exact byte
    count requested — precise sizing regardless of how *filler*'s width divides
    into the remainder.
    """
    header_bytes = len(_BIG_HEADER.encode("utf-8"))
    target_body_bytes = total_bytes - header_bytes
    filler_bytes = len(filler.encode("utf-8"))
    count, remainder = divmod(target_body_bytes, filler_bytes)
    body = filler * count + "a" * remainder
    document = _BIG_HEADER + body
    assert len(document.encode("utf-8")) == total_bytes
    return document


def test_oversized_multibyte_document_is_payload_too_large() -> None:
    """A body of 3-byte characters ('€'), sized so the whole document is exactly
    one UTF-8 byte over the 64 KiB cap."""
    document = _document_of_exact_size(MAX_CONTENT_BYTES + 1, "€")
    with pytest.raises(PayloadTooLargeError):
        parse_skill(document)


def test_document_exactly_at_the_cap_with_multibyte_filler_is_accepted() -> None:
    """The same construction, sized to land exactly on the cap: accepted.

    Proves the boundary is measured in encoded bytes rather than character
    count — a naive ``len(content)`` check would accept a body several times
    over 64 KiB of actual storage, since a 4-byte emoji is one ``len()`` unit but
    four bytes on the wire.
    """
    document = _document_of_exact_size(MAX_CONTENT_BYTES, "\U0001f600")
    # Far fewer characters than bytes — the point of the test.
    assert len(document) < MAX_CONTENT_BYTES
    parsed = parse_skill(document)
    assert len(parsed.content.encode("utf-8")) == MAX_CONTENT_BYTES

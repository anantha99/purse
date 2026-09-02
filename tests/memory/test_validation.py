"""Validation and error codes (C3.1) — no database, no engine, no I/O.

These are the checks that must fire *before* anything is written, so they are
proved without a database on purpose: if one of them ever needs Postgres to
fail, the validation moved somewhere it does not belong.

The error **codes** are asserted as literal strings rather than through the
exception classes. The class is an implementation detail; the code is the wire
contract PRD §10 fixes and the MCP tools will re-use (C4.2). A rename that
changed the string should break here.
"""

from __future__ import annotations

import uuid

import pytest

from purse.db.models import InitiatedBy, MemoryKind
from purse.memory import errors, service
from purse.memory.service import (
    _validate_content,
    _validate_initiated_by,
    _validate_kind,
    _validate_limit,
    _validate_query,
)

# -- error codes are the contract -------------------------------------------


def test_error_codes_are_the_prd_10_spellings() -> None:
    assert errors.NotFoundError.code == "NOT_FOUND"
    assert errors.PayloadTooLargeError.code == "PAYLOAD_TOO_LARGE"
    assert errors.ValidationError.code == "VALIDATION"


def test_every_memory_error_carries_a_message_and_a_code() -> None:
    for cls in (errors.NotFoundError, errors.PayloadTooLargeError, errors.ValidationError):
        error = cls("something went wrong")
        assert error.message == "something went wrong"
        assert str(error) == "something went wrong"
        assert isinstance(error, errors.MemoryError_)


# -- content ----------------------------------------------------------------


@pytest.mark.parametrize("content", ["", "   ", "\n\t "])
def test_empty_content_is_a_validation_error(content: str) -> None:
    with pytest.raises(errors.ValidationError) as caught:
        _validate_content(content)
    assert caught.value.code == "VALIDATION"


def test_content_is_stored_verbatim_not_stripped() -> None:
    """Padding is the user's business. Rejecting is allowed; editing is not."""
    assert _validate_content("  I prefer TypeScript.  ") == "  I prefer TypeScript.  "


def test_content_at_exactly_the_limit_is_accepted() -> None:
    content = "a" * service.MAX_CONTENT_BYTES
    assert _validate_content(content) == content


def test_one_byte_over_the_limit_is_payload_too_large() -> None:
    with pytest.raises(errors.PayloadTooLargeError) as caught:
        _validate_content("a" * (service.MAX_CONTENT_BYTES + 1))
    assert caught.value.code == "PAYLOAD_TOO_LARGE"


def test_the_limit_is_utf8_bytes_not_characters() -> None:
    """The edge that a naive ``len()`` gets wrong.

    1024 four-byte emoji are exactly 4096 bytes — fine — while 1025 of them are
    4100 bytes and must be rejected even though ``len()`` says 1025 < 4096.
    """
    emoji = "\N{PURSE}"
    assert len(emoji.encode("utf-8")) == 4

    at_limit = emoji * (service.MAX_CONTENT_BYTES // 4)
    assert len(at_limit) == 1024
    assert len(at_limit.encode("utf-8")) == service.MAX_CONTENT_BYTES
    assert _validate_content(at_limit) == at_limit

    over = at_limit + emoji
    assert len(over) < service.MAX_CONTENT_BYTES  # a character count would pass this
    with pytest.raises(errors.PayloadTooLargeError):
        _validate_content(over)


def test_a_multibyte_character_straddling_the_limit_is_rejected() -> None:
    """4095 ASCII bytes + one 2-byte character = 4096 bytes: accepted.
    4096 ASCII bytes + one 2-byte character = 4098 bytes: rejected."""
    two_byte = "\N{LATIN SMALL LETTER E WITH ACUTE}"
    assert len(two_byte.encode("utf-8")) == 2

    exact = "a" * (service.MAX_CONTENT_BYTES - 2) + two_byte
    assert len(exact.encode("utf-8")) == service.MAX_CONTENT_BYTES
    assert _validate_content(exact) == exact

    with pytest.raises(errors.PayloadTooLargeError):
        _validate_content("a" * service.MAX_CONTENT_BYTES + two_byte)


# -- enums ------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["fact", "preference", "decision"])
def test_valid_kind_strings_coerce_to_the_enum(kind: str) -> None:
    assert _validate_kind(kind) is MemoryKind(kind)


def test_enum_members_pass_through_unchanged() -> None:
    assert _validate_kind(MemoryKind.DECISION) is MemoryKind.DECISION
    assert _validate_initiated_by(InitiatedBy.AGENT) is InitiatedBy.AGENT


@pytest.mark.parametrize("kind", ["profile", "FACT", "", "note"])
def test_unknown_kind_is_a_validation_error_listing_the_allowed_values(kind: str) -> None:
    """``profile`` is the interesting one: PRD §11 lists it as explicitly post-MVP,
    so it must fail now rather than be quietly accepted."""
    with pytest.raises(errors.ValidationError) as caught:
        _validate_kind(kind)
    assert caught.value.code == "VALIDATION"
    assert "fact, preference, decision" in caught.value.message


@pytest.mark.parametrize("initiated_by", ["user", "agent"])
def test_valid_initiators_coerce(initiated_by: str) -> None:
    assert _validate_initiated_by(initiated_by) is InitiatedBy(initiated_by)


@pytest.mark.parametrize("initiated_by", ["system", "USER", ""])
def test_unknown_initiator_is_a_validation_error(initiated_by: str) -> None:
    with pytest.raises(errors.ValidationError):
        _validate_initiated_by(initiated_by)


# -- limits and queries -----------------------------------------------------


def test_limit_defaults_when_absent() -> None:
    assert _validate_limit(None, default=8) == 8


def test_over_large_limit_is_clamped_not_rejected() -> None:
    assert _validate_limit(10_000, default=8) == service.MAX_LIMIT


@pytest.mark.parametrize("limit", [0, -1])
def test_non_positive_limit_is_a_validation_error(limit: int) -> None:
    with pytest.raises(errors.ValidationError):
        _validate_limit(limit, default=8)


@pytest.mark.parametrize("query", ["", "   "])
def test_empty_query_is_a_validation_error(query: str) -> None:
    with pytest.raises(errors.ValidationError):
        _validate_query(query)


# -- cursors ----------------------------------------------------------------


def test_cursor_round_trips() -> None:
    import datetime as dt

    when = dt.datetime(2026, 9, 2, 12, 30, 45, 123456, tzinfo=dt.UTC)
    memory_id = uuid.uuid4()
    decoded_time, decoded_id = service._decode_cursor(service._encode_cursor(when, memory_id))
    assert decoded_time == when
    assert decoded_id == memory_id


@pytest.mark.parametrize("cursor", ["not-base64!!", "", "YWJj", "bm90LWEtdGltZXwxMjM="])
def test_a_cursor_we_did_not_issue_is_a_validation_error(cursor: str) -> None:
    """Never a 500, and never a silent "start from the beginning" — a client
    paging with a stale cursor should be told, not quietly given page one."""
    with pytest.raises(errors.ValidationError) as caught:
        service._decode_cursor(cursor)
    assert caught.value.code == "VALIDATION"


# -- LIKE escaping ----------------------------------------------------------


def test_like_wildcards_in_a_query_are_escaped() -> None:
    """Without this, searching for ``100%`` matches every memory in the vault."""
    assert service._like_escape("100%") == "100\\%"
    assert service._like_escape("a_b") == "a\\_b"
    # The backslash itself must be escaped first, or it escapes the escapes.
    assert service._like_escape("c:\\path") == "c:\\\\path"

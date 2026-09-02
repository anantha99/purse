"""C2.1: token format, hashing, and the redaction that keeps a token out of logs."""

from __future__ import annotations

import base64
import logging
import re

import pytest

from purse.auth.tokens import (
    SECRET_CHARS,
    SECRET_ENTROPY_BYTES,
    TOKEN_PREFIX,
    RawToken,
    generate_token,
    hash_token,
    is_well_formed,
    token_hashes_match,
)

TOKEN_RE = re.compile(rf"^{re.escape(TOKEN_PREFIX)}[A-Za-z0-9_-]{{{SECRET_CHARS},}}$")


def test_minted_token_matches_the_documented_format() -> None:
    token = generate_token().reveal()
    assert TOKEN_RE.fullmatch(token), token


def test_secret_part_is_exactly_the_documented_length() -> None:
    """43 chars is base64url of 32 bytes, unpadded. The format is a wire contract."""
    body = generate_token().reveal().removeprefix(TOKEN_PREFIX)
    assert len(body) == SECRET_CHARS
    # Decodes back to the full 256 bits — no entropy lost in the encoding.
    assert len(base64.urlsafe_b64decode(body + "=")) == SECRET_ENTROPY_BYTES


def test_minted_tokens_are_unique() -> None:
    tokens = {generate_token().reveal() for _ in range(256)}
    assert len(tokens) == 256


def test_prefix_is_the_documented_one() -> None:
    """Secret scanners and docs both key off this literal; changing it is a breaking change."""
    assert TOKEN_PREFIX == "purse_pat_"  # noqa: S105 - a public format marker, not a secret


# -- redaction ---------------------------------------------------------------


def _secret_body(token: RawToken) -> str:
    return token.reveal().removeprefix(TOKEN_PREFIX)


def test_str_and_repr_redact_the_secret() -> None:
    token = generate_token()
    body = _secret_body(token)
    for rendered in (str(token), repr(token), f"{token}", "%s" % token, format(token)):  # noqa: UP031
        assert token.reveal() not in rendered
        # Only a 4-char recognition hint survives; the rest is gone.
        assert body[4:] not in rendered
        assert body[:4] in rendered
        assert TOKEN_PREFIX in rendered


def test_repr_of_a_container_holding_a_token_is_also_redacted() -> None:
    """The realistic leak: a token inside a dict or list that something logs."""
    token = generate_token()
    rendered = repr({"token": token, "list": [token]})
    assert token.reveal() not in rendered
    assert _secret_body(token)[4:] not in rendered


def test_logging_a_token_does_not_log_the_token(caplog: pytest.LogCaptureFixture) -> None:
    token = generate_token()
    with caplog.at_level(logging.INFO):
        logging.getLogger("purse.test").info("minted %s", token)
    assert token.reveal() not in caplog.text
    assert _secret_body(token)[4:] not in caplog.text


def test_exception_carrying_a_token_does_not_expose_it() -> None:
    token = generate_token()
    error = RuntimeError(f"failed for {token}")
    assert token.reveal() not in str(error)


def test_raw_token_is_not_hashable_or_comparable() -> None:
    """No __eq__/__hash__ on a secret: no timing oracle, no set/dict membership by value."""
    token = generate_token()
    with pytest.raises(TypeError):
        hash(token)
    assert token != RawToken(token.reveal())  # identity comparison, deliberately


# -- hashing -----------------------------------------------------------------


def test_hash_is_sha256_hex_and_deterministic() -> None:
    token = generate_token()
    digest = hash_token(token.reveal())
    assert re.fullmatch(r"[0-9a-f]{64}", digest)
    assert digest == hash_token(token.reveal())
    assert token.digest() == digest


def test_hash_covers_the_prefix_not_just_the_secret() -> None:
    token = generate_token()
    body = _secret_body(token)
    assert hash_token(token.reveal()) != hash_token(body)


def test_different_tokens_hash_differently() -> None:
    assert hash_token(generate_token().reveal()) != hash_token(generate_token().reveal())


def test_token_hashes_match_is_exact() -> None:
    digest = hash_token(generate_token().reveal())
    assert token_hashes_match(digest, digest)
    assert not token_hashes_match(digest, digest[:-1] + ("0" if digest[-1] != "0" else "1"))


# -- shape check -------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "",
        "not-a-token",
        "purse_pat_",
        "purse_pat_tooshort",
        "PURSE_PAT_" + "a" * 43,
        "purse_oauth_" + "a" * 43,
        "purse_pat_" + "a" * 42,
        "purse_pat_" + "a" * 42 + "!",
        "purse_pat_" + "a" * 42 + "+",  # base64 standard alphabet, not urlsafe
        " purse_pat_" + "a" * 43,
    ],
)
def test_malformed_tokens_are_rejected(value: str) -> None:
    assert not is_well_formed(value)


def test_well_formed_token_is_accepted() -> None:
    assert is_well_formed(generate_token().reveal())

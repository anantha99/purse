"""Personal access token format, generation, and hashing (C2.1).

**Format.** ``purse_pat_<43 urlsafe-base64 chars>``. The random part is
``secrets.token_urlsafe(32)`` — 256 bits of CSPRNG output, which base64url
encodes to 43 characters with no padding.

The ``purse_pat_`` prefix is not decoration. It gives GitHub/GitLab secret
scanners and ``gitleaks``-style tooling a stable pattern to match, it makes a
leaked token identifiable in a log or a paste without anyone having to guess
whose it is, and it lets support say "the thing starting with ``purse_pat_``"
in docs without ambiguity.

**Why plain SHA-256 and not argon2/bcrypt/scrypt.** Password hashing exists to
make *offline guessing* expensive, because human-chosen passwords come from a
small, skewed distribution. A Purse PAT is not a password: it is 256 bits of
uniform CSPRNG output that no human ever chose, typed, or reused. Brute-forcing
one is 2^255 expected SHA-256 evaluations — a work factor no KDF can meaningfully
improve on and no attacker can afford. What a KDF *would* cost us is real:
argon2 is deliberately slow and salted, so every authentication would have to
scan candidate rows and run the KDF per row instead of doing one indexed lookup.
Deterministic, unsalted SHA-256 of the full token gives O(1) lookup on a unique
index, which is exactly what a bearer-token check needs.

**What is stored.** Only the hex digest, in ``connections.token_hash`` (unique
where non-null). The raw token exists in exactly two places: the return value of
:func:`generate_token`, and the caller's hands. It is never persisted, never
logged, and never placed in an exception message — :class:`RawToken` exists to
make the accidental version of that mistake impossible, because printing,
formatting, or f-stringing one yields a redacted preview and nothing else.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

__all__ = [
    "SECRET_CHARS",
    "SECRET_ENTROPY_BYTES",
    "TOKEN_PREFIX",
    "RawToken",
    "generate_token",
    "hash_token",
    "is_well_formed",
    "token_hashes_match",
]

#: Identifies a Purse personal access token to secret scanners and to humans.
TOKEN_PREFIX = "purse_pat_"  # noqa: S105 - a public format marker, not a secret

#: Bytes of CSPRNG entropy behind the random part. 32 bytes = 256 bits.
SECRET_ENTROPY_BYTES = 32

#: Characters ``secrets.token_urlsafe(32)`` produces (base64url of 32 bytes,
#: stripped of ``=`` padding). Asserted rather than assumed, because the token
#: format is a wire contract with every client that stores one.
SECRET_CHARS = 43

_PREVIEW_CHARS = 4

# base64url alphabet, which is what token_urlsafe emits.
_SECRET_ALPHABET = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")


class RawToken:
    """A bearer token in the clear, wrapped so it cannot leak by accident.

    ``str()``, ``repr()``, f-strings, ``logging`` and ``%``-formatting all go
    through the redacted preview. The full value comes out only via the
    explicit, greppable :meth:`reveal` — so "who can see this token" is a
    question ``rg 'reveal()'`` answers.

    Deliberately not comparable and not hashable: an ``__eq__`` on a secret is
    a timing oracle waiting to be written, and authentication compares
    *digests* (:func:`token_hashes_match`), never raw tokens.
    """

    __slots__ = ("_token",)

    def __init__(self, token: str) -> None:
        self._token = token

    def reveal(self) -> str:
        """Return the token in the clear. The only way out of this wrapper."""
        return self._token

    @property
    def preview(self) -> str:
        """``purse_pat_abcd…`` — enough to recognise, not enough to use."""
        body = self._token.removeprefix(TOKEN_PREFIX)
        return f"{TOKEN_PREFIX}{body[:_PREVIEW_CHARS]}…"

    def digest(self) -> str:
        """SHA-256 hex of the full token — the value stored in the database."""
        return hash_token(self._token)

    def __str__(self) -> str:
        return self.preview

    def __repr__(self) -> str:
        return f"RawToken({self.preview})"

    # Unhashable and uncomparable on purpose; see the class docstring.
    __hash__ = None  # type: ignore[assignment]


def generate_token() -> RawToken:
    """Mint a fresh PAT. 256 bits of CSPRNG entropy behind the public prefix."""
    return RawToken(f"{TOKEN_PREFIX}{secrets.token_urlsafe(SECRET_ENTROPY_BYTES)}")


def hash_token(token: str) -> str:
    """SHA-256 hex digest of the **full** token, prefix included.

    Hashing the full string (rather than just the random part) means a stored
    digest is not reusable under any other token format we might add later.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def token_hashes_match(left: str, right: str) -> bool:
    """Constant-time digest comparison.

    Digests are not secret, so this is belt-and-braces rather than strictly
    required — but the authentication path is the last place to start making
    exceptions about comparison primitives.
    """
    return hmac.compare_digest(left, right)


def is_well_formed(token: str) -> bool:
    """Cheap shape check: correct prefix, correct length, base64url body.

    Rejecting malformed input before touching the database keeps unauthenticated
    traffic from turning into query load. Callers must **not** surface which
    check failed — see :func:`purse.auth.pat.authenticate_pat`.
    """
    if not token.startswith(TOKEN_PREFIX):
        return False
    body = token[len(TOKEN_PREFIX) :]
    if len(body) < SECRET_CHARS:
        return False
    return all(character in _SECRET_ALPHABET for character in body)

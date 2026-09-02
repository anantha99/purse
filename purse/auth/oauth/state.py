"""In-memory, single-instance OAuth state: auth codes, refresh tokens, expiry.

Purse persists the durable half of a token to Postgres — the ``connections``
row, whose ``token_hash`` is the access token's identity and whose ``revoked_at``
makes revocation instant (C2.11). What has no column, and therefore lives here in
the provider instance, is the *transient* half:

* **authorization codes** — short-lived, single-use, PKCE-bound;
* **refresh tokens** — hashed, mapped back to their connection;
* **access-token expiry** — the connections table has no expiry column, so the
  clock lives beside the token hash here.

.. warning::
   This state is process-local. A **multi-instance** deployment needs a shared
   store (Redis/Postgres) or sticky routing, or a client that lands on a second
   replica will find its auth code / refresh token / token expiry missing. This
   is an accepted limitation for the single-instance self-host and staging
   targets of M2; it is called out loudly here and in the module that wires it.

The clock is injectable so expiry is testable without sleeping.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from mcp.server.auth.provider import AuthorizationCode
from pydantic import AnyUrl

from purse.db.models import AuthMode

__all__ = [
    "DEFAULT_ACCESS_TOKEN_TTL_SECONDS",
    "DEFAULT_AUTH_CODE_TTL_SECONDS",
    "DEFAULT_REFRESH_TOKEN_TTL_SECONDS",
    "AccessTokenRegistry",
    "AuthorizationCodeStore",
    "Grant",
    "IssuedCode",
    "RefreshRecord",
    "RefreshTokenStore",
    "hash_opaque_token",
    "verify_code_challenge",
]

#: 5 minutes — RFC 6749 §10.5 wants auth codes short-lived and single-use.
DEFAULT_AUTH_CODE_TTL_SECONDS = 5 * 60
#: 1 hour — short-lived access token; refresh rotates it.
DEFAULT_ACCESS_TOKEN_TTL_SECONDS = 60 * 60
#: 30 days — refresh token lifetime.
DEFAULT_REFRESH_TOKEN_TTL_SECONDS = 60 * 60 * 24 * 30

_Clock = Callable[[], float]


def hash_opaque_token(token: str) -> str:
    """SHA-256 hex of an opaque OAuth token, matching the PAT hashing scheme.

    Opaque access/refresh tokens are 256-bit CSPRNG output, so — exactly as with
    PATs (see :mod:`purse.auth.tokens`) — a plain SHA-256 is the right hash: it
    gives an O(1) indexed lookup and no KDF can improve on brute-forcing uniform
    256-bit randomness.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def verify_code_challenge(code_verifier: str, code_challenge: str) -> bool:
    """Constant-time PKCE S256 check: ``BASE64URL(SHA256(verifier)) == challenge``.

    The MCP SDK's token handler performs this same check before calling the
    provider's exchange; it is reproduced here so the rule is unit-testable
    without standing up the HTTP layer, and so a direct provider-level flow test
    can gate the exchange on it.
    """
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    computed = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return hmac.compare_digest(computed, code_challenge)


@dataclass(frozen=True, slots=True)
class Grant:
    """The consented terms a token will carry, decided at approval time.

    ``scopes`` are canonical scope strings; ``auth_mode`` records which of the
    OAuth modes minted the connection so the ``connections`` row is labelled
    correctly (``oauth_cimd`` / ``oauth_dcr`` / ``oauth_static``).
    """

    workspace_id: uuid.UUID
    client_id: str
    client_name: str
    scopes: tuple[str, ...]
    writes_enabled: bool
    auth_mode: AuthMode
    resource: str | None


@dataclass(frozen=True, slots=True)
class IssuedCode:
    """An authorization code and the grant it will realise on exchange."""

    code: AuthorizationCode
    grant: Grant


class AuthorizationCodeStore:
    """Short-lived, single-use authorization codes, PKCE-bound."""

    def __init__(
        self,
        *,
        ttl_seconds: int = DEFAULT_AUTH_CODE_TTL_SECONDS,
        clock: _Clock = time.time,
    ) -> None:
        self._codes: dict[str, IssuedCode] = {}
        self._ttl = ttl_seconds
        self._clock = clock

    def issue(
        self,
        *,
        client_id: str,
        redirect_uri: AnyUrl,
        redirect_uri_provided_explicitly: bool,
        code_challenge: str,
        grant: Grant,
    ) -> str:
        """Mint, store, and return a fresh authorization code."""
        code = f"purse_ac_{secrets.token_urlsafe(32)}"
        expires_at = self._clock() + self._ttl
        self._codes[code] = IssuedCode(
            code=AuthorizationCode(
                code=code,
                client_id=client_id,
                redirect_uri=redirect_uri,
                redirect_uri_provided_explicitly=redirect_uri_provided_explicitly,
                scopes=list(grant.scopes),
                expires_at=expires_at,
                code_challenge=code_challenge,
                resource=grant.resource,
            ),
            grant=grant,
        )
        return code

    def load(self, code: str) -> IssuedCode | None:
        """Return the code if present and unexpired; drop and return None if expired."""
        issued = self._codes.get(code)
        if issued is None:
            return None
        if issued.code.expires_at < self._clock():
            del self._codes[code]
            return None
        return issued

    def consume(self, code: str) -> IssuedCode | None:
        """Load and atomically remove the code — the single-use guarantee."""
        issued = self.load(code)
        if issued is None:
            return None
        del self._codes[code]
        return issued


@dataclass(frozen=True, slots=True)
class RefreshRecord:
    """What a stored refresh-token hash maps to."""

    connection_id: uuid.UUID
    client_id: str
    scopes: tuple[str, ...]
    expires_at: int
    access_hash: str


class RefreshTokenStore:
    """Refresh tokens by hash, rotated on every use."""

    def __init__(self, *, clock: _Clock = time.time) -> None:
        self._records: dict[str, RefreshRecord] = {}
        self._clock = clock

    def register(self, token_hash: str, record: RefreshRecord) -> None:
        self._records[token_hash] = record

    def load(self, token_hash: str) -> RefreshRecord | None:
        record = self._records.get(token_hash)
        if record is None:
            return None
        if record.expires_at < self._clock():
            del self._records[token_hash]
            return None
        return record

    def consume(self, token_hash: str) -> RefreshRecord | None:
        record = self.load(token_hash)
        if record is None:
            return None
        del self._records[token_hash]
        return record


@dataclass(frozen=True, slots=True)
class _AccessRecord:
    expires_at: int
    connection_id: uuid.UUID


class AccessTokenRegistry:
    """Access-token expiry and its connection, keyed by token hash.

    The ``connections`` row is the durable identity and the source of truth for
    revocation and scopes; this registry only holds the expiry clock the schema
    has no column for, plus the connection id so the ``/revoke`` endpoint can act
    on the connection.
    """

    def __init__(self, *, clock: _Clock = time.time) -> None:
        self._records: dict[str, _AccessRecord] = {}
        self._clock = clock

    def register(self, token_hash: str, *, expires_at: int, connection_id: uuid.UUID) -> None:
        self._records[token_hash] = _AccessRecord(expires_at, connection_id)

    def expires_at(self, token_hash: str) -> int | None:
        record = self._records.get(token_hash)
        return None if record is None else record.expires_at

    def connection_id(self, token_hash: str) -> uuid.UUID | None:
        record = self._records.get(token_hash)
        return None if record is None else record.connection_id

    def is_live(self, token_hash: str) -> bool:
        expires_at = self.expires_at(token_hash)
        return expires_at is not None and expires_at >= self._clock()

    def forget(self, token_hash: str) -> None:
        self._records.pop(token_hash, None)

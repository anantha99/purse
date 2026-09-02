"""The bridge from a verified token to an :class:`AuthContext` (C2.3, seam to C4).

Purse's access tokens are opaque, so their claims are carried in-process on the
FastMCP :class:`AccessToken` rather than inside a JWT. The MCP tool layer (C4)
receives that ``AccessToken`` from FastMCP and calls
:func:`auth_context_from_access_token` to get the exact same
:class:`~purse.auth.context.AuthContext` a PAT would have produced — so a tool
handler never has to care which of the six auth modes the caller used.

Both the OAuth provider and the PAT verifier build their tokens through
:func:`build_access_token`, so the claim shape is defined in one place.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable

from fastmcp.server.auth.auth import AccessToken

from purse.auth.context import AuthContext
from purse.auth.scopes import parse_scopes

__all__ = [
    "CLAIM_CLIENT_NAME",
    "CLAIM_CONNECTION_ID",
    "CLAIM_WORKSPACE_ID",
    "CLAIM_WRITES_ENABLED",
    "auth_context_from_access_token",
    "build_access_token",
]

#: Claim keys carried on every Purse-issued :class:`AccessToken`. Namespaced so
#: they never collide with a standard OAuth/JWT claim a future mode might add.
CLAIM_CONNECTION_ID = "purse.connection_id"
CLAIM_WORKSPACE_ID = "purse.workspace_id"
CLAIM_WRITES_ENABLED = "purse.writes_enabled"
CLAIM_CLIENT_NAME = "purse.client_name"


def build_access_token(
    *,
    token: str,
    connection_id: uuid.UUID,
    workspace_id: uuid.UUID,
    scopes: Iterable[str],
    writes_enabled: bool,
    client_name: str,
    expires_at: int | None = None,
) -> AccessToken:
    """Assemble the FastMCP token for a verified caller, claims and all.

    ``client_id`` on the token is set to ``client_name`` — an opaque Purse token
    has no separate OAuth client identifier once issued, and the connection is
    the trusted identity (via the claims), not this field.
    """
    return AccessToken(
        token=token,
        client_id=client_name or str(connection_id),
        scopes=list(scopes),
        expires_at=expires_at,
        claims={
            CLAIM_CONNECTION_ID: str(connection_id),
            CLAIM_WORKSPACE_ID: str(workspace_id),
            CLAIM_WRITES_ENABLED: writes_enabled,
            CLAIM_CLIENT_NAME: client_name,
        },
    )


def auth_context_from_access_token(token: AccessToken) -> AuthContext:
    """Reconstruct the :class:`AuthContext` a Purse token stands for.

    Raises :class:`KeyError` if the token was not minted by Purse (its claims
    lack the namespaced keys) and :class:`~purse.auth.scopes.UnknownScopeError`
    if a stored scope is outside the vocabulary — both are server bugs, not bad
    credentials, and are left to surface rather than being folded into a generic
    authentication failure.
    """
    claims = token.claims
    return AuthContext(
        connection_id=uuid.UUID(str(claims[CLAIM_CONNECTION_ID])),
        workspace_id=uuid.UUID(str(claims[CLAIM_WORKSPACE_ID])),
        scopes=parse_scopes(token.scopes),
        writes_enabled=bool(claims[CLAIM_WRITES_ENABLED]),
        client_name=str(claims[CLAIM_CLIENT_NAME]),
    )

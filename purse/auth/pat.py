"""Verifying a personal access token (C2.1).

One function, one job: turn a bearer string into an :class:`AuthContext`, or
refuse. Every refusal is the same refusal — see :mod:`purse.auth.errors` for
why the failure modes are deliberately indistinguishable.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from purse.auth.context import AuthContext
from purse.auth.errors import AuthenticationError
from purse.auth.scopes import parse_scopes
from purse.auth.tokens import hash_token, is_well_formed, token_hashes_match
from purse.db.models import AuthMode, Connection

__all__ = ["authenticate_pat"]


def authenticate_pat(session: Session, token: str) -> AuthContext:
    """Verify a PAT and return the context it authorises.

    Raises :class:`~purse.auth.errors.AuthenticationError` — with the same
    message every time — if the token is malformed, unknown, belongs to a
    non-PAT connection, or belongs to a connection that has been revoked.

    Not workspace-scoped, and cannot be: the token *is* what selects the
    workspace. The returned context is the boundary, and everything downstream
    takes its ``workspace_id`` from here rather than from the request.
    """
    if not is_well_formed(token):
        raise AuthenticationError

    digest = hash_token(token)
    # Revocation is filtered in SQL, so a revoked connection and a token that
    # never existed both produce "no row" and therefore the identical error.
    # `token_hash` is unique where non-null (C1.2), so this is O(1) on the
    # index and can never match more than one row.
    stmt = select(Connection).where(
        Connection.token_hash == digest,
        Connection.auth_mode == AuthMode.PAT,
        Connection.revoked_at.is_(None),
    )
    connection = session.scalars(stmt).one_or_none()
    if connection is None or connection.token_hash is None:
        raise AuthenticationError

    # The SQL equality already matched; this is the belt-and-braces constant-time
    # confirmation, so the final word on "is this the right token" is never a
    # short-circuiting Python string compare.
    if not token_hashes_match(connection.token_hash, digest):
        raise AuthenticationError

    # A stored scope string outside the vocabulary means the row was written by
    # something that bypassed `mint_pat`. `parse_scopes` raises `UnknownScopeError`
    # and it is left to propagate: that is a server bug, not a bad credential, and
    # authenticating with a silently narrowed grant would hide it. It is
    # deliberately *not* folded into `AuthenticationError` — the identical-message
    # rule exists to protect credential probing, not to bury misconfiguration.
    scopes = parse_scopes(connection.scopes)

    return AuthContext(
        connection_id=connection.id,
        workspace_id=connection.workspace_id,
        scopes=scopes,
        writes_enabled=connection.writes_enabled,
        client_name=connection.client_name,
    )

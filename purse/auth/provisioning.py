"""Minting and revoking personal access tokens (C2.1, C2.11).

Shown once, hashed at rest, revocable. :func:`mint_pat` is the only supported
way a ``connections`` row acquires a ``token_hash``: it validates the grant
before it writes, so every stored scope list is one :func:`parse_scopes` can
read back, and it returns the raw token exactly once.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable

from sqlalchemy import func, update
from sqlalchemy.orm import Session

from purse.auth.scopes import Scope, format_scopes, parse_scope
from purse.auth.tokens import RawToken, generate_token
from purse.db.models import AuthMode, Connection
from purse.db.repo import Repo

__all__ = ["ProvisioningError", "mint_pat", "revoke_connection"]


class ProvisioningError(ValueError):
    """Raised when a requested connection could not be provisioned as asked."""


def _normalize_scopes(scopes: Iterable[str | Scope]) -> list[str]:
    """Validate and canonicalise a requested grant.

    Accepts strings or :class:`Scope` members because callers come from both
    sides: a REST handler holds JSON strings, internal code holds the enum.
    """
    return format_scopes(parse_scope(str(scope)) for scope in scopes)


def mint_pat(
    session: Session,
    *,
    workspace_id: uuid.UUID,
    client_name: str,
    scopes: Iterable[str | Scope],
    writes_enabled: bool = False,
) -> tuple[Connection, RawToken]:
    """Create a PAT connection and return it with its one-and-only raw token.

    The returned :class:`~purse.auth.tokens.RawToken` is the only copy that
    will ever exist — the database gets the SHA-256 digest. Show it to the user
    now or it is gone.

    Raises :class:`ProvisioningError` for an empty ``client_name``,
    :class:`~purse.auth.scopes.UnknownScopeError` for a scope outside the
    vocabulary, and :class:`~purse.db.repo.NotFoundError` if the workspace does
    not exist.

    Keyword-only past ``session``: five arguments of which two are a bool and a
    list is exactly the shape that gets silently transposed at a call site.
    """
    name = client_name.strip()
    if not name:
        raise ProvisioningError("client_name must not be empty")

    granted = _normalize_scopes(scopes)
    token = generate_token()

    # Through the workspace-scoped Repo (C1.8) rather than a bare INSERT, so
    # provisioning gets the same "does this workspace exist" check as every
    # other write, and there is no second code path writing `connections`.
    repo = Repo.open(session, workspace_id)
    connection = repo.add_connection(
        client_name=name,
        auth_mode=AuthMode.PAT,
        scopes=granted,
        writes_enabled=writes_enabled,
        token_hash=token.digest(),
    )
    return connection, token


def revoke_connection(session: Session, connection_id: uuid.UUID) -> bool:
    """Revoke a connection. Returns True if this call is what revoked it.

    Idempotent: revoking an already-revoked or non-existent connection returns
    False and changes nothing. After this, :func:`~purse.auth.pat.authenticate_pat`
    rejects the token — the ``revoked_at IS NULL`` filter is part of the lookup,
    so there is no cache to invalidate and no window where an old token still
    works.

    Not workspace-scoped, unlike :meth:`purse.db.repo.Repo.revoke_connection`:
    revocation is reachable from an admin/self-host path that holds a connection
    id and nothing else. Where the caller *does* know the workspace, prefer the
    ``Repo`` method — it refuses ids from other workspaces.
    """
    stmt = (
        update(Connection)
        .where(Connection.id == connection_id, Connection.revoked_at.is_(None))
        .values(revoked_at=func.now())
        .returning(Connection.id)
        .execution_options(synchronize_session=False)
    )
    revoked = session.execute(stmt).scalar_one_or_none() is not None
    if revoked:
        session.expire_all()
    return revoked

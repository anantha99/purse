"""Personal access tokens as a FastMCP ``TokenVerifier`` (C2.1 → MCP, PRD §8.5 mode 6).

PAT verification already exists and is complete — :func:`authenticate_pat`. This
module does one thing: expose it as a FastMCP :class:`TokenVerifier` so the MCP
server accepts a ``purse_pat_`` bearer alongside OAuth access tokens, combined via
:class:`MultiAuth` (see :func:`purse.auth.oauth.provider.build_purse_auth`). It
adds no new verification logic; it hashes nothing itself and reuses the constant
authentication-failure behaviour of the underlying function.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager

from fastmcp.server.auth.auth import AccessToken, TokenVerifier
from pydantic import AnyHttpUrl
from sqlalchemy.orm import Session

from purse.auth.errors import AuthenticationError
from purse.auth.oauth.claims import build_access_token
from purse.auth.pat import authenticate_pat

__all__ = ["PatVerifier"]

_SessionScopeFactory = Callable[[], AbstractContextManager[Session]]


class PatVerifier(TokenVerifier):
    """Verifies ``purse_pat_`` bearer tokens against the ``connections`` table.

    ``verify_token`` returns ``None`` for anything that is not a valid, unrevoked
    PAT — which is exactly what :class:`MultiAuth` needs to fall through to (or
    past) this verifier — and a fully populated :class:`AccessToken` (claims and
    all, via :func:`build_access_token`) for a good one, so an OAuth token and a
    PAT resolve to the identical :class:`~purse.auth.context.AuthContext`
    downstream.
    """

    def __init__(
        self,
        session_scope_factory: _SessionScopeFactory,
        *,
        base_url: AnyHttpUrl | str | None = None,
        resource_base_url: AnyHttpUrl | str | None = None,
    ) -> None:
        super().__init__(base_url=base_url, resource_base_url=resource_base_url)
        self._session_scope = session_scope_factory

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            with self._session_scope() as session:
                ctx = authenticate_pat(session, token)
        except AuthenticationError:
            return None
        return build_access_token(
            token=token,
            connection_id=ctx.connection_id,
            workspace_id=ctx.workspace_id,
            scopes=ctx.scope_strings(),
            writes_enabled=ctx.writes_enabled,
            client_name=ctx.client_name,
        )

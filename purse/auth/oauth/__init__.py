"""OAuth 2.1 authorization server for Purse (C2.3-C2.9, PRD §8.5).

Purse is its own AS — it subclasses FastMCP's ``OAuthProvider`` rather than
proxying an upstream IdP, because a self-hostable vault that cannot authenticate
without a third party is a worse product. The six auth modes of §8.5 are served
from one MCP endpoint via :func:`build_purse_auth`, which composes the OAuth
provider with the existing PAT path through FastMCP's ``MultiAuth``.

The one function the orchestrator needs is :func:`build_purse_auth` (or
:func:`build_purse_auth_from_env`): hand its return value to
``create_mcp_server(auth=...)``. Everything else here is the machinery behind it.

The reverse seam — turning a verified token back into the same
:class:`~purse.auth.context.AuthContext` a PAT produces, whichever mode issued
it — is :func:`auth_context_from_access_token`, for the MCP tool layer (C4).
"""

from __future__ import annotations

from purse.auth.oauth.claims import (
    auth_context_from_access_token,
    build_access_token,
)
from purse.auth.oauth.consent import (
    CONSENT_PATH,
    ConsentError,
    PendingAuthorization,
)
from purse.auth.oauth.loopback import PurseClient, matches_loopback_redirect_uri
from purse.auth.oauth.metadata import (
    CLAUDE_AI_CALLBACK,
    CLAUDE_COM_CALLBACK,
    KNOWN_CLIENT_CALLBACKS,
    purse_authorization_server_metadata,
)
from purse.auth.oauth.pat_verifier import PatVerifier
from purse.auth.oauth.provider import (
    PURSE_OAUTH_SECRET_ENV,
    PURSE_PUBLIC_URL_ENV,
    PurseOAuthProvider,
    PurseOAuthStore,
    StaticClient,
    build_purse_auth,
    build_purse_auth_from_env,
)

__all__ = [
    "CLAUDE_AI_CALLBACK",
    "CLAUDE_COM_CALLBACK",
    "CONSENT_PATH",
    "KNOWN_CLIENT_CALLBACKS",
    "PURSE_OAUTH_SECRET_ENV",
    "PURSE_PUBLIC_URL_ENV",
    "ConsentError",
    "PatVerifier",
    "PendingAuthorization",
    "PurseClient",
    "PurseOAuthProvider",
    "PurseOAuthStore",
    "StaticClient",
    "auth_context_from_access_token",
    "build_access_token",
    "build_purse_auth",
    "build_purse_auth_from_env",
    "matches_loopback_redirect_uri",
    "purse_authorization_server_metadata",
]

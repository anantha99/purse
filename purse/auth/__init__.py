"""Auth (C2): OAuth 2.1 authorization server, personal access tokens, and per-connection scopes.

This package is a library, not a server. It exposes functions the REST and MCP
layers call; it knows nothing about HTTP, FastMCP, or request objects. The
shape of the PAT path (C2.1 + C2.2), which is what M1 ships:

``mint_pat`` → ``(Connection, RawToken)``
    Provision a scoped, revocable token. The raw token is returned once and
    never stored — only its SHA-256 digest reaches the database.
``authenticate_pat`` → ``AuthContext``
    Turn a bearer string into a verified caller, or raise
    ``AuthenticationError``. Every failure mode raises the identical error.
``require_scope(ctx, scope)``
    The authorisation gate. Raises ``ScopeError`` carrying
    ``UNAUTHORIZED_SCOPE`` (PRD §10), including when a ``:write`` scope is
    granted but the connection's writes toggle is off.

First boot lives in :mod:`purse.auth.bootstrap` and is deliberately **not**
re-exported here: it is an operational entrypoint (``python -m
purse.auth.bootstrap``), and importing it from this ``__init__`` would make
runpy load the module twice and warn about it. Import it directly.

OAuth 2.1, discovery, CIMD, DCR, static clients and loopback redirects (C2.3
onward) land in later modules; nothing here presumes PAT is the only mode.
"""

from purse.auth.context import AuthContext, require_scope
from purse.auth.errors import (
    AUTHENTICATION_FAILED_MESSAGE,
    AuthenticationError,
    AuthError,
    ErrorCode,
    ScopeError,
)
from purse.auth.oauth import (
    PatVerifier,
    PurseOAuthProvider,
    StaticClient,
    auth_context_from_access_token,
    build_purse_auth,
    build_purse_auth_from_env,
)
from purse.auth.pat import authenticate_pat
from purse.auth.provisioning import ProvisioningError, mint_pat, revoke_connection
from purse.auth.scopes import (
    ALL_SCOPES,
    DEFAULT_CONNECTION_SCOPES,
    ONBOARDING_SCOPES,
    WRITE_SCOPES,
    Scope,
    UnknownScopeError,
    format_scopes,
    has_scope,
    parse_scope,
    parse_scopes,
)
from purse.auth.tokens import TOKEN_PREFIX, RawToken, generate_token, hash_token

__all__ = [
    "ALL_SCOPES",
    "AUTHENTICATION_FAILED_MESSAGE",
    "DEFAULT_CONNECTION_SCOPES",
    "ONBOARDING_SCOPES",
    "TOKEN_PREFIX",
    "WRITE_SCOPES",
    "AuthContext",
    "AuthError",
    "AuthenticationError",
    "ErrorCode",
    "PatVerifier",
    "ProvisioningError",
    "PurseOAuthProvider",
    "RawToken",
    "Scope",
    "ScopeError",
    "StaticClient",
    "UnknownScopeError",
    "auth_context_from_access_token",
    "authenticate_pat",
    "build_purse_auth",
    "build_purse_auth_from_env",
    "format_scopes",
    "generate_token",
    "has_scope",
    "hash_token",
    "mint_pat",
    "parse_scope",
    "parse_scopes",
    "require_scope",
    "revoke_connection",
]

"""Purse as its own OAuth 2.1 authorization server (C2.3-C2.9).

Purse subclasses FastMCP's :class:`OAuthProvider` — it *is* the AS, with no
upstream IdP, which the self-host story demands. The reference implementation is
FastMCP's ``InMemoryOAuthProvider``; this swaps its dicts for Postgres
``connections`` rows and fills the two gaps the spike named:

* **CIMD** (the primary OAuth path since MCP spec 2026-07-28): ``get_routes``
  rewrites the AS metadata to advertise ``client_id_metadata_document_supported``
  and ``"none"`` (see :mod:`purse.auth.oauth.metadata`) and swaps the ``/token``
  client authenticator for FastMCP's ``PrivateKeyJWTClientAuthenticator``;
  ``get_client`` resolves CIMD ``client_id`` URLs through ``CIMDClientManager``.
* **Port-agnostic loopback** (Claude Code and native CLIs): static and DCR
  clients are :class:`~purse.auth.oauth.loopback.PurseClient`, whose redirect
  validation ignores the loopback port.

Tokens are **opaque**. An access token's identity is the SHA-256 in a
``connections`` row (``auth_mode`` = ``oauth_cimd`` / ``oauth_dcr`` /
``oauth_static``); ``revoked_at`` makes revocation instant (C2.11). Authorization
codes, refresh tokens, and access-token expiry live in the provider instance
(:mod:`purse.auth.oauth.state`) — single-instance state, flagged there.

The ``/authorize`` endpoint does not silently issue a code: it redirects to a
signed approve page (:mod:`purse.auth.oauth.consent`) that the single operator
approves, at which point the connection is minted with the consented scopes.
"""

from __future__ import annotations

import os
import secrets
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import cast

from fastmcp.server.auth.auth import (
    AccessToken,
    MultiAuth,
    OAuthProvider,
    PrivateKeyJWTClientAuthenticator,
    TokenHandler,
)
from fastmcp.server.auth.cimd import CIMDClientManager
from fastmcp.server.auth.redirect_validation import build_client_redirect
from mcp.server.auth.handlers.metadata import MetadataHandler
from mcp.server.auth.provider import (
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    TokenError,
)
from mcp.server.auth.routes import cors_middleware
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyHttpUrl, AnyUrl
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Route

from purse.auth.oauth.claims import build_access_token
from purse.auth.oauth.consent import (
    CONSENT_PATH,
    ConsentError,
    PendingAuthorization,
    consume_nonce,
    new_nonce,
    render_consent_page,
    sign_pending,
    unsign_pending,
)
from purse.auth.oauth.loopback import PurseClient
from purse.auth.oauth.metadata import purse_authorization_server_metadata
from purse.auth.oauth.pat_verifier import PatVerifier
from purse.auth.oauth.state import (
    DEFAULT_ACCESS_TOKEN_TTL_SECONDS,
    DEFAULT_AUTH_CODE_TTL_SECONDS,
    DEFAULT_REFRESH_TOKEN_TTL_SECONDS,
    AccessTokenRegistry,
    AuthorizationCodeStore,
    Grant,
    RefreshRecord,
    RefreshTokenStore,
    hash_opaque_token,
)
from purse.auth.provisioning import revoke_connection as revoke_connection_row
from purse.auth.scopes import (
    ALL_SCOPES,
    DEFAULT_CONNECTION_SCOPES,
    ONBOARDING_SCOPES,
    format_scopes,
)
from purse.db.models import AuthMode, Connection, OAuthClient, OAuthClientKind, User
from purse.db.repo import Repo, list_user_workspaces

__all__ = [
    "PURSE_OAUTH_SECRET_ENV",
    "PURSE_PUBLIC_URL_ENV",
    "ConnectionSnapshot",
    "PurseOAuthProvider",
    "PurseOAuthStore",
    "StaticClient",
    "WorkspaceResolutionError",
    "build_purse_auth",
    "build_purse_auth_from_env",
]

#: The public base URL the AS advertises in discovery metadata and redirects.
PURSE_PUBLIC_URL_ENV = "PURSE_PUBLIC_URL"
#: The itsdangerous secret that signs the approve-page state.
PURSE_OAUTH_SECRET_ENV = "PURSE_OAUTH_SECRET"  # noqa: S105 - env var name, not a secret

_PERSONAL_WORKSPACE_NAME = "Personal"

_SessionScopeFactory = Callable[[], AbstractContextManager[Session]]
_WorkspaceResolver = Callable[[Session], uuid.UUID]


class WorkspaceResolutionError(RuntimeError):
    """Raised when no operator workspace can be resolved to mint a connection into."""


@dataclass(frozen=True, slots=True)
class StaticClient:
    """A pre-registered client (Cursor's pattern, PRD §8.5 mode 4).

    ``client_secret`` is ``None`` for a public client (``token_endpoint_auth_method``
    becomes ``none``); a value makes it confidential (``client_secret_post``).
    """

    client_id: str
    redirect_uris: tuple[str, ...]
    client_secret: str | None = None
    scopes: tuple[str, ...] = ()
    client_name: str = ""


# First-party CIMD clients whose metadata documents we bundle instead of fetching.
#
# CIMD normally resolves a URL client_id by fetching that URL. In practice the
# major clients host their metadata behind a CDN (Cloudflare) that 403s requests
# from datacenter egress IPs — so a self-hosted AS on Fly/Render/etc. cannot fetch
# them, and CIMD breaks for exactly the clients it exists to serve. Seeding the
# public, stable first-party documents makes these clients resolve with no network
# call; genuinely unknown CIMD URLs still go through the SSRF-guarded fetch path.
# Loopback-only redirects mean a bundled entry can only ever deliver a code to the
# user's own machine, so trusting the client_id URL here carries minimal risk.
#
# Verified from the live document on 2026-09-02. Add Claude web / ChatGPT here as
# each is tested (their CIMD URLs and callbacks differ from Claude Code's).
# Registered with the full scope set: a client may *request* any scope; what is
# actually granted is still decided by the consent policy (first connection gets
# writes, later ones default read-only, PRD §7.1). Without this, the SDK rejects
# the request with invalid_scope before it ever reaches the approve page.
_ALL_SCOPE_STRINGS: tuple[str, ...] = tuple(sorted(scope.value for scope in ALL_SCOPES))

KNOWN_CIMD_CLIENTS: tuple[StaticClient, ...] = (
    StaticClient(
        client_id="https://claude.ai/oauth/claude-code-client-metadata",
        redirect_uris=("http://localhost/callback", "http://127.0.0.1/callback"),
        client_secret=None,  # public client (token_endpoint_auth_method = none)
        scopes=_ALL_SCOPE_STRINGS,
        client_name="Claude Code",
    ),
)


@dataclass(frozen=True, slots=True)
class ConnectionSnapshot:
    """The connection fields a verified access token resolves to."""

    connection_id: uuid.UUID
    workspace_id: uuid.UUID
    scopes: tuple[str, ...]
    writes_enabled: bool
    client_name: str


def _default_resolve_workspace_id(session: Session) -> uuid.UUID:
    """The single operator's workspace: first user, their ``Personal`` workspace.

    Purse is one-user-one-vault (PRD §8.1); on a self-host instance there is
    exactly one user and a ``Personal`` workspace created at bootstrap. This
    resolver is what an OAuth approval mints into. A deployment with real
    multi-tenancy must pass its own resolver.
    """
    user = session.scalars(select(User).order_by(User.created_at, User.id)).first()
    if user is None:
        raise WorkspaceResolutionError("no user exists to own an OAuth connection")
    workspaces = list_user_workspaces(session, user.id)
    if not workspaces:
        raise WorkspaceResolutionError("the vault owner has no workspace to connect into")
    for workspace in workspaces:
        if workspace.name == _PERSONAL_WORKSPACE_NAME:
            return workspace.id
    return workspaces[0].id


def _static_to_client(config: StaticClient) -> PurseClient:
    auth_method = "none" if config.client_secret is None else "client_secret_post"
    return PurseClient(
        client_id=config.client_id,
        client_secret=config.client_secret,
        redirect_uris=[AnyUrl(uri) for uri in config.redirect_uris],
        token_endpoint_auth_method=auth_method,
        grant_types=["authorization_code", "refresh_token"],
        scope=" ".join(config.scopes) or None,
        client_name=config.client_name or config.client_id,
        application_type="native",
    )


class PurseOAuthStore:
    """All database access the OAuth provider needs, isolated behind one object.

    Each method opens its own session scope, so it is one transaction that
    commits on success — the provider never holds a session across an ``await``.
    """

    def __init__(
        self,
        session_scope_factory: _SessionScopeFactory,
        *,
        resolve_workspace_id: _WorkspaceResolver | None = None,
    ) -> None:
        self._session_scope = session_scope_factory
        self._resolve = resolve_workspace_id or _default_resolve_workspace_id

    def workspace_for_new_connection(self) -> tuple[uuid.UUID, bool]:
        """Resolve the operator workspace and whether it has no connections yet."""
        with self._session_scope() as session:
            workspace_id = self._resolve(session)
            count = session.execute(
                select(func.count())
                .select_from(Connection)
                .where(Connection.workspace_id == workspace_id)
            ).scalar_one()
            return workspace_id, int(count) == 0

    def mint_connection(self, grant: Grant, *, token_hash: str) -> uuid.UUID:
        """Create the ``connections`` row for a newly issued access token."""
        with self._session_scope() as session:
            repo = Repo.open(session, grant.workspace_id)
            connection = repo.add_connection(
                client_name=grant.client_name,
                auth_mode=grant.auth_mode,
                scopes=list(grant.scopes),
                writes_enabled=grant.writes_enabled,
                token_hash=token_hash,
            )
            return connection.id

    def connection_for_token(self, token_hash: str) -> ConnectionSnapshot | None:
        """The active (unrevoked) OAuth connection a token hash resolves to."""
        with self._session_scope() as session:
            stmt = select(Connection).where(
                Connection.token_hash == token_hash,
                Connection.revoked_at.is_(None),
                Connection.auth_mode != AuthMode.PAT,
            )
            connection = session.scalars(stmt).one_or_none()
            if connection is None:
                return None
            return ConnectionSnapshot(
                connection_id=connection.id,
                workspace_id=connection.workspace_id,
                scopes=tuple(connection.scopes),
                writes_enabled=connection.writes_enabled,
                client_name=connection.client_name,
            )

    def rotate_token_hash(self, connection_id: uuid.UUID, new_hash: str) -> bool:
        """Point an active connection at a rotated access token. False if it is gone."""
        with self._session_scope() as session:
            stmt = (
                update(Connection)
                .where(Connection.id == connection_id, Connection.revoked_at.is_(None))
                .values(token_hash=new_hash)
                .returning(Connection.id)
                .execution_options(synchronize_session=False)
            )
            return session.execute(stmt).scalar_one_or_none() is not None

    def revoke_connection(self, connection_id: uuid.UUID) -> bool:
        with self._session_scope() as session:
            return revoke_connection_row(session, connection_id)

    def save_registered_client(
        self, client_info: OAuthClientInformationFull, kind: OAuthClientKind
    ) -> None:
        with self._session_scope() as session:
            session.add(
                OAuthClient(
                    kind=kind,
                    client_id=client_info.client_id,
                    client_metadata=client_info.model_dump(mode="json"),
                )
            )
            session.flush()

    def load_registered_client(self, client_id: str) -> PurseClient | None:
        with self._session_scope() as session:
            row = session.scalars(
                select(OAuthClient).where(OAuthClient.client_id == client_id)
            ).one_or_none()
            if row is None:
                return None
            return PurseClient.model_validate(row.client_metadata)


class PurseOAuthProvider(OAuthProvider):
    """Purse's OAuth 2.1 authorization server (own-AS, CIMD-first)."""

    def __init__(
        self,
        *,
        base_url: AnyHttpUrl | str,
        session_scope_factory: _SessionScopeFactory,
        secret: str,
        issuer_url: AnyHttpUrl | str | None = None,
        resource_base_url: AnyHttpUrl | str | None = None,
        static_clients: Sequence[StaticClient] = (),
        resolve_workspace_id: _WorkspaceResolver | None = None,
        access_token_ttl_seconds: int = DEFAULT_ACCESS_TOKEN_TTL_SECONDS,
        refresh_token_ttl_seconds: int = DEFAULT_REFRESH_TOKEN_TTL_SECONDS,
        auth_code_ttl_seconds: int = DEFAULT_AUTH_CODE_TTL_SECONDS,
        consent_max_age_seconds: int = DEFAULT_AUTH_CODE_TTL_SECONDS,
    ) -> None:
        super().__init__(
            base_url=base_url,
            issuer_url=issuer_url,
            resource_base_url=resource_base_url,
            client_registration_options=ClientRegistrationOptions(
                enabled=True,
                valid_scopes=sorted(scope.value for scope in ALL_SCOPES),
            ),
            revocation_options=RevocationOptions(enabled=True),
        )
        if not secret:
            raise ValueError(f"{PURSE_OAUTH_SECRET_ENV} must be a non-empty signing secret")
        self._secret = secret
        self._store = PurseOAuthStore(
            session_scope_factory, resolve_workspace_id=resolve_workspace_id
        )
        self._cimd = CIMDClientManager(enable_cimd=True)
        # Known first-party CIMD clients are seeded as local clients so get_client
        # resolves them (below) before ever attempting a fetch; caller-supplied
        # static_clients come last so a deployment can override a bundled entry.
        self._statics: dict[str, PurseClient] = {
            config.client_id: _static_to_client(config)
            for config in (*KNOWN_CIMD_CLIENTS, *static_clients)
        }
        self._codes = AuthorizationCodeStore(ttl_seconds=auth_code_ttl_seconds)
        self._refresh = RefreshTokenStore()
        self._access = AccessTokenRegistry()
        self._consumed_nonces: set[str] = set()
        self._access_ttl = access_token_ttl_seconds
        self._refresh_ttl = refresh_token_ttl_seconds
        self._consent_max_age = consent_max_age_seconds

    # -- client resolution --------------------------------------------------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        static = self._statics.get(client_id)
        if static is not None:
            return static
        if self._cimd.is_cimd_client_id(client_id):
            return cast(
                "OAuthClientInformationFull | None",
                await self._cimd.get_client(client_id),
            )
        return self._store.load_registered_client(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        """Persist a DCR registration (RFC 7591) to ``oauth_clients``."""
        self._store.save_registered_client(client_info, OAuthClientKind.DCR)

    def _auth_mode_for(self, client_id: str) -> AuthMode:
        if self._cimd.is_cimd_client_id(client_id):
            return AuthMode.OAUTH_CIMD
        if client_id in self._statics:
            return AuthMode.OAUTH_STATIC
        return AuthMode.OAUTH_DCR

    # -- authorization + consent -------------------------------------------

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        """Redirect to the signed approve page instead of issuing a code directly."""
        pending = PendingAuthorization(
            client_id=client.client_id,
            client_name=client.client_name or client.client_id,
            redirect_uri=str(params.redirect_uri),
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            scopes=tuple(params.scopes or ()),
            code_challenge=params.code_challenge,
            state=params.state,
            resource=params.resource,
            nonce=new_nonce(),
        )
        token = sign_pending(pending, self._secret)
        base = str(self.base_url).rstrip("/")
        return f"{base}{CONSENT_PATH}?txn={token}"

    def _grant_terms(
        self, pending: PendingAuthorization, first_connection: bool
    ) -> tuple[tuple[str, ...], bool]:
        """Resolve the scopes and writes flag a consent will realise.

        Requested scopes are honoured (filtered to the known vocabulary);
        absent, they default to onboarding scopes for the first connection and
        read-only for every later one (PRD §7.1). Writes follow the same rule:
        the first connection gets the "writes on" badge, later ones stay off
        even when a write scope was granted.
        """
        known = {scope.value for scope in ALL_SCOPES}
        requested = tuple(
            format_scopes(scope for scope in ALL_SCOPES if scope.value in pending.scopes)
        )
        if any(scope in known for scope in pending.scopes):
            scopes = requested
        else:
            default = ONBOARDING_SCOPES if first_connection else DEFAULT_CONNECTION_SCOPES
            scopes = tuple(format_scopes(default))
        return scopes, first_connection

    def _build_grant(self, pending: PendingAuthorization) -> Grant:
        workspace_id, first_connection = self._store.workspace_for_new_connection()
        scopes, writes_enabled = self._grant_terms(pending, first_connection)
        return Grant(
            workspace_id=workspace_id,
            client_id=pending.client_id,
            client_name=pending.client_name,
            scopes=scopes,
            writes_enabled=writes_enabled,
            auth_mode=self._auth_mode_for(pending.client_id),
            resource=pending.resource,
        )

    async def _consent(self, request: Request) -> Response:
        if request.method == "POST":
            return await self._submit_consent(request)
        return await self._render_consent(request)

    async def _render_consent(self, request: Request) -> Response:
        token = request.query_params.get("txn")
        if not token:
            return HTMLResponse("Missing authorization request.", status_code=400)
        try:
            pending = unsign_pending(token, self._secret, max_age=self._consent_max_age)
        except ConsentError:
            return HTMLResponse(
                "This authorization request is invalid or expired.", status_code=400
            )
        _, first_connection = self._store.workspace_for_new_connection()
        scopes, writes_enabled = self._grant_terms(pending, first_connection)
        page = render_consent_page(
            client_name=pending.client_name,
            scopes=scopes,
            writes_enabled=writes_enabled,
            first_connection=first_connection,
            txn=token,
        )
        return HTMLResponse(page)

    async def _submit_consent(self, request: Request) -> Response:
        form = await request.form()
        token = form.get("txn")
        decision = form.get("decision")
        if not isinstance(token, str):
            return HTMLResponse("Missing authorization request.", status_code=400)
        try:
            pending = unsign_pending(token, self._secret, max_age=self._consent_max_age)
            consume_nonce(pending.nonce, self._consumed_nonces)
        except ConsentError:
            return HTMLResponse(
                "This authorization request is invalid, expired, or already used.",
                status_code=400,
            )

        if decision != "approve":
            return self._client_redirect(pending, {"error": "access_denied"})

        grant = self._build_grant(pending)
        code = self._codes.issue(
            client_id=pending.client_id,
            redirect_uri=AnyUrl(pending.redirect_uri),
            redirect_uri_provided_explicitly=pending.redirect_uri_provided_explicitly,
            code_challenge=pending.code_challenge,
            grant=grant,
        )
        return self._client_redirect(pending, {"code": code})

    def _client_redirect(
        self, pending: PendingAuthorization, params: dict[str, str]
    ) -> RedirectResponse:
        payload = dict(params)
        if pending.state is not None:
            payload["state"] = pending.state
        url = build_client_redirect(pending.redirect_uri, payload, iss=str(self.issuer_url))
        return RedirectResponse(url, status_code=302, headers={"Cache-Control": "no-store"})

    # -- token issuance -----------------------------------------------------

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        issued = self._codes.load(authorization_code)
        if issued is None or issued.code.client_id != client.client_id:
            return None
        return issued.code

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        issued = self._codes.consume(authorization_code.code)
        if issued is None:
            raise TokenError("invalid_grant", "authorization code not found or already used")
        grant = issued.grant

        access_token = f"purse_at_{secrets.token_urlsafe(32)}"
        refresh_token = f"purse_rt_{secrets.token_urlsafe(32)}"
        now = time.time()
        access_expires_at = int(now + self._access_ttl)
        refresh_expires_at = int(now + self._refresh_ttl)

        connection_id = self._store.mint_connection(
            grant, token_hash=hash_opaque_token(access_token)
        )
        self._register_tokens(
            connection_id=connection_id,
            client_id=grant.client_id,
            scopes=grant.scopes,
            access_token=access_token,
            access_expires_at=access_expires_at,
            refresh_token=refresh_token,
            refresh_expires_at=refresh_expires_at,
        )
        return OAuthToken(
            access_token=access_token,
            token_type="Bearer",  # noqa: S106 - OAuth token type, not a credential
            expires_in=self._access_ttl,
            refresh_token=refresh_token,
            scope=" ".join(grant.scopes),
        )

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        record = self._refresh.load(hash_opaque_token(refresh_token))
        if record is None or record.client_id != client.client_id:
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=record.client_id,
            scopes=list(record.scopes),
            expires_at=record.expires_at,
        )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        record = self._refresh.consume(hash_opaque_token(refresh_token.token))
        if record is None:
            raise TokenError("invalid_grant", "refresh token not found or already used")
        self._access.forget(record.access_hash)

        new_access = f"purse_at_{secrets.token_urlsafe(32)}"
        new_refresh = f"purse_rt_{secrets.token_urlsafe(32)}"
        now = time.time()
        access_expires_at = int(now + self._access_ttl)
        refresh_expires_at = int(now + self._refresh_ttl)

        if not self._store.rotate_token_hash(record.connection_id, hash_opaque_token(new_access)):
            raise TokenError("invalid_grant", "connection is no longer active")
        self._register_tokens(
            connection_id=record.connection_id,
            client_id=record.client_id,
            scopes=record.scopes,
            access_token=new_access,
            access_expires_at=access_expires_at,
            refresh_token=new_refresh,
            refresh_expires_at=refresh_expires_at,
        )
        return OAuthToken(
            access_token=new_access,
            token_type="Bearer",  # noqa: S106 - OAuth token type, not a credential
            expires_in=self._access_ttl,
            refresh_token=new_refresh,
            scope=" ".join(record.scopes),
        )

    def _register_tokens(
        self,
        *,
        connection_id: uuid.UUID,
        client_id: str,
        scopes: tuple[str, ...],
        access_token: str,
        access_expires_at: int,
        refresh_token: str,
        refresh_expires_at: int,
    ) -> None:
        access_hash = hash_opaque_token(access_token)
        self._access.register(
            access_hash, expires_at=access_expires_at, connection_id=connection_id
        )
        self._refresh.register(
            hash_opaque_token(refresh_token),
            RefreshRecord(
                connection_id=connection_id,
                client_id=client_id,
                scopes=scopes,
                expires_at=refresh_expires_at,
                access_hash=access_hash,
            ),
        )

    async def load_access_token(self, token: str) -> AccessToken | None:
        digest = hash_opaque_token(token)
        if not self._access.is_live(digest):
            return None
        snapshot = self._store.connection_for_token(digest)
        if snapshot is None:
            return None
        return build_access_token(
            token=token,
            connection_id=snapshot.connection_id,
            workspace_id=snapshot.workspace_id,
            scopes=snapshot.scopes,
            writes_enabled=snapshot.writes_enabled,
            client_name=snapshot.client_name,
            expires_at=self._access.expires_at(digest),
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        """Revoke a token by revoking its connection — instant via ``revoked_at``."""
        digest = hash_opaque_token(token.token)
        if isinstance(token, RefreshToken):
            record = self._refresh.consume(digest)
            if record is not None:
                self._access.forget(record.access_hash)
                self._store.revoke_connection(record.connection_id)
            return
        connection_id = self._access.connection_id(digest)
        self._access.forget(digest)
        if connection_id is not None:
            self._store.revoke_connection(connection_id)

    # -- routes -------------------------------------------------------------

    def get_routes(self, mcp_path: str | None = None) -> list[Route]:
        """OAuth routes with the CIMD metadata + token patches and the consent page."""
        routes = super().get_routes(mcp_path)
        base_url = self.base_url
        issuer_url = self.issuer_url
        if base_url is None or issuer_url is None:  # pragma: no cover - always set in __init__
            raise RuntimeError("PurseOAuthProvider requires base_url and issuer_url")
        token_endpoint = str(base_url).rstrip("/") + "/token"

        patched: list[Route] = []
        for route in routes:
            if isinstance(route, Route) and route.path == "/.well-known/oauth-authorization-server":
                metadata = purse_authorization_server_metadata(
                    base_url=base_url,
                    issuer_url=issuer_url,
                    client_registration_options=self.client_registration_options
                    or ClientRegistrationOptions(),
                    revocation_options=self.revocation_options or RevocationOptions(),
                )
                patched.append(
                    Route(
                        path=route.path,
                        endpoint=cors_middleware(
                            MetadataHandler(metadata).handle, ["GET", "OPTIONS"]
                        ),
                        methods=route.methods or ["GET", "OPTIONS"],
                        name=route.name,
                        include_in_schema=route.include_in_schema,
                    )
                )
            elif (
                isinstance(route, Route)
                and route.path == "/token"
                and route.methods is not None
                and "POST" in route.methods
            ):
                token_handler = TokenHandler(
                    provider=self,
                    client_authenticator=PrivateKeyJWTClientAuthenticator(
                        self, self._cimd, token_endpoint
                    ),
                )
                patched.append(
                    Route(
                        path="/token",
                        endpoint=cors_middleware(token_handler.handle, ["POST", "OPTIONS"]),
                        methods=["POST", "OPTIONS"],
                    )
                )
            else:
                patched.append(route)

        patched.append(Route(CONSENT_PATH, endpoint=self._consent, methods=["GET", "POST"]))
        return patched


def build_purse_auth(
    *,
    base_url: AnyHttpUrl | str,
    session_scope_factory: _SessionScopeFactory,
    secret: str,
    issuer_url: AnyHttpUrl | str | None = None,
    resource_base_url: AnyHttpUrl | str | None = None,
    static_clients: Sequence[StaticClient] = (),
    resolve_workspace_id: _WorkspaceResolver | None = None,
) -> MultiAuth:
    """The seam: the combined auth object for ``create_mcp_server(auth=...)``.

    Returns a :class:`MultiAuth` whose ``server`` is the Purse OAuth AS (it owns
    every route and all discovery metadata) and whose one verifier accepts
    ``purse_pat_`` bearers, so a single MCP endpoint takes all six auth modes.
    FastMCP mounts the AS routes (discovery, ``/authorize``, ``/token``,
    ``/register``, ``/revoke``, and the ``/purse/oauth/consent`` approve page)
    automatically from ``provider.get_routes()``.
    """
    provider = PurseOAuthProvider(
        base_url=base_url,
        session_scope_factory=session_scope_factory,
        secret=secret,
        issuer_url=issuer_url,
        resource_base_url=resource_base_url,
        static_clients=static_clients,
        resolve_workspace_id=resolve_workspace_id,
    )
    verifier = PatVerifier(
        session_scope_factory, base_url=base_url, resource_base_url=resource_base_url
    )
    return MultiAuth(server=provider, verifiers=[verifier])


def build_purse_auth_from_env(
    session_scope_factory: _SessionScopeFactory,
    *,
    env: Mapping[str, str] | None = None,
    static_clients: Sequence[StaticClient] = (),
) -> MultiAuth:
    """Build the combined auth from ``PURSE_PUBLIC_URL`` and ``PURSE_OAUTH_SECRET``."""
    source = os.environ if env is None else env
    base_url = (source.get(PURSE_PUBLIC_URL_ENV) or "").strip()
    if not base_url:
        raise ValueError(f"{PURSE_PUBLIC_URL_ENV} must be set to the server's public base URL")
    secret = (source.get(PURSE_OAUTH_SECRET_ENV) or "").strip()
    if not secret:
        raise ValueError(f"{PURSE_OAUTH_SECRET_ENV} must be set to a signing secret")
    return build_purse_auth(
        base_url=base_url,
        session_scope_factory=session_scope_factory,
        secret=secret,
        static_clients=static_clients,
    )

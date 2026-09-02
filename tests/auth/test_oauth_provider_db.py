"""C2.3-C2.8 against a real database: the full OAuth flow through the provider.

Marked ``db``: these mint real ``connections`` and ``oauth_clients`` rows and
prove behaviour the schema enforces (the ``auth_mode`` enum, the token-hash
lookup, ``revoked_at``). Every flow is driven by calling the provider's own
methods directly — the authorization handler's ``/authorize`` maps to
:meth:`PurseOAuthProvider.authorize`, the approve page POST to
:meth:`_submit_consent`, and ``/token`` to ``load_authorization_code`` +
``exchange_authorization_code`` — so no MCP server and no browser are needed, and
the PKCE + consent-signing steps a real client would take are exercised for real.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import pytest
from mcp.server.auth.provider import AuthorizationParams
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.requests import Request
from starlette.responses import Response

from purse.auth.oauth.claims import auth_context_from_access_token
from purse.auth.oauth.loopback import PurseClient
from purse.auth.oauth.provider import PurseOAuthProvider, StaticClient
from purse.auth.oauth.state import hash_opaque_token, verify_code_challenge
from purse.auth.scopes import Scope
from purse.db.models import AuthMode, Connection, OAuthClient, OAuthClientKind, Workspace

from .conftest import TwoWorkspaces  # noqa: F401 - keeps fixtures importable/consistent

pytestmark = pytest.mark.db

_REDIRECT_URI = "https://cursor.com/cb"


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _scope_factory(session: Session) -> Callable[[], AbstractContextManager[Session]]:
    @contextmanager
    def scope() -> Iterator[Session]:
        # Yield the test's transaction-bound session without committing or
        # closing it, so the outer rollback in the db_session fixture cleans up.
        yield session

    return scope


def _pkce_pair(verifier: str) -> tuple[str, str]:
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    )
    return verifier, challenge


def _post_request(form: dict[str, str]) -> Request:
    body = urlencode(form).encode()
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/purse/oauth/consent",
        "query_string": b"",
        "headers": [
            (b"content-type", b"application/x-www-form-urlencoded"),
            (b"content-length", str(len(body)).encode()),
        ],
    }

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(scope, receive)


def _make_provider(session: Session, **clients: Any) -> PurseOAuthProvider:
    static = clients.get(
        "static_clients",
        [
            StaticClient(
                client_id="cursor",
                redirect_uris=(_REDIRECT_URI,),
                scopes=("memory:read", "memory:write", "skills:read"),
                client_name="Cursor",
            )
        ],
    )
    return PurseOAuthProvider(
        base_url="https://vault.example.test",
        session_scope_factory=_scope_factory(session),
        secret="signing-secret-for-tests",  # noqa: S106 - test signing key
        static_clients=static,
    )


def _location(response: Response) -> dict[str, list[str]]:
    return parse_qs(urlparse(response.headers["location"]).query)


def _authorize_and_approve(
    provider: PurseOAuthProvider,
    client: OAuthClientInformationFull,
    *,
    challenge: str,
    requested_scopes: list[str] | None,
    decision: str = "approve",
) -> Response:
    params = AuthorizationParams(
        state="state-token",
        scopes=requested_scopes,
        code_challenge=challenge,
        redirect_uri=AnyUrl(_REDIRECT_URI),
        redirect_uri_provided_explicitly=True,
        resource=None,
    )
    consent_url = _run(provider.authorize(client, params))
    txn = parse_qs(urlparse(consent_url).query)["txn"][0]
    response: Response = _run(
        provider._submit_consent(_post_request({"txn": txn, "decision": decision}))
    )
    return response


def _run_flow(
    provider: PurseOAuthProvider,
    client_id: str,
    *,
    requested_scopes: list[str] | None,
) -> tuple[Any, Any]:
    """authorize -> approve -> exchange, returning (OAuthToken, AuthContext)."""
    client = _run(provider.get_client(client_id))
    assert client is not None
    verifier, challenge = _pkce_pair(f"{client_id}-code-verifier-abcdefghijklmnop")
    response = _authorize_and_approve(
        provider, client, challenge=challenge, requested_scopes=requested_scopes
    )
    assert response.status_code == 302
    code = _location(response)["code"][0]

    code_obj = _run(provider.load_authorization_code(client, code))
    assert code_obj is not None
    assert verify_code_challenge(verifier, code_obj.code_challenge)
    tokens = _run(provider.exchange_authorization_code(client, code_obj))

    access = _run(provider.load_access_token(tokens.access_token))
    assert access is not None
    return tokens, auth_context_from_access_token(access)


# -- full flow ---------------------------------------------------------------


def test_full_authorize_approve_exchange_verify(session: Session, workspace: Workspace) -> None:
    provider = _make_provider(session)
    client = _run(provider.get_client("cursor"))
    assert client is not None
    verifier, challenge = _pkce_pair("cursor-code-verifier-abcdefghijklmnop")

    response = _authorize_and_approve(
        provider,
        client,
        challenge=challenge,
        requested_scopes=["memory:read", "memory:write", "skills:read"],
    )
    assert response.status_code == 302
    query = _location(response)
    assert query["state"][0] == "state-token"
    assert query["iss"][0].rstrip("/") == "https://vault.example.test"
    code = query["code"][0]

    code_obj = _run(provider.load_authorization_code(client, code))
    assert code_obj is not None
    assert verify_code_challenge(verifier, code_obj.code_challenge)
    tokens = _run(provider.exchange_authorization_code(client, code_obj))
    assert tokens.access_token.startswith("purse_at_")
    assert tokens.refresh_token is not None
    assert tokens.refresh_token.startswith("purse_rt_")

    access = _run(provider.load_access_token(tokens.access_token))
    assert access is not None
    ctx = auth_context_from_access_token(access)
    assert ctx.workspace_id == workspace.id
    assert ctx.scopes == frozenset({Scope.MEMORY_READ, Scope.MEMORY_WRITE, Scope.SKILLS_READ})
    assert ctx.writes_enabled is True  # first connection gets writes on (§7.1)

    connection = session.get(Connection, ctx.connection_id)
    assert connection is not None
    assert connection.auth_mode is AuthMode.OAUTH_STATIC
    assert connection.token_hash == hash_opaque_token(tokens.access_token)
    assert connection.workspace_id == workspace.id

    # the authorization code is single-use
    assert _run(provider.load_authorization_code(client, code)) is None


def test_denied_consent_mints_nothing(session: Session, workspace: Workspace) -> None:
    provider = _make_provider(session)
    client = _run(provider.get_client("cursor"))
    assert client is not None
    _, challenge = _pkce_pair("cursor-code-verifier-abcdefghijklmnop")

    response = _authorize_and_approve(
        provider, client, challenge=challenge, requested_scopes=["memory:read"], decision="deny"
    )
    assert response.status_code == 302
    assert _location(response)["error"][0] == "access_denied"
    assert session.execute(select(func.count()).select_from(Connection)).scalar_one() == 0


# -- refresh -----------------------------------------------------------------


def test_refresh_rotates_and_kills_the_old_access_token(
    session: Session, workspace: Workspace
) -> None:
    provider = _make_provider(session)
    client = _run(provider.get_client("cursor"))
    assert client is not None
    tokens, _ = _run_flow(provider, "cursor", requested_scopes=["memory:read"])

    loaded = _run(provider.load_refresh_token(client, tokens.refresh_token))
    assert loaded is not None
    refreshed = _run(provider.exchange_refresh_token(client, loaded, list(loaded.scopes)))
    assert refreshed.access_token != tokens.access_token

    # the new access token works
    assert _run(provider.load_access_token(refreshed.access_token)) is not None
    # the old access token no longer resolves — its hash was rotated off the row
    assert _run(provider.load_access_token(tokens.access_token)) is None
    # the old refresh token is single-use
    assert _run(provider.load_refresh_token(client, tokens.refresh_token)) is None


# -- revocation (C2.11) ------------------------------------------------------


def test_revoke_makes_verify_fail_instantly(session: Session, workspace: Workspace) -> None:
    provider = _make_provider(session)
    tokens, _ = _run_flow(provider, "cursor", requested_scopes=["memory:read"])

    access = _run(provider.load_access_token(tokens.access_token))
    assert access is not None
    _run(provider.revoke_token(access))

    assert _run(provider.load_access_token(tokens.access_token)) is None


# -- first vs later connections (§7.1) ---------------------------------------


def test_first_connection_writes_on_second_read_only(
    session: Session, workspace: Workspace
) -> None:
    provider = _make_provider(
        session,
        static_clients=[
            StaticClient(client_id="client-a", redirect_uris=(_REDIRECT_URI,)),
            StaticClient(client_id="client-b", redirect_uris=(_REDIRECT_URI,)),
        ],
    )

    # First connection ever: onboarding scopes, writes on.
    _, first = _run_flow(provider, "client-a", requested_scopes=None)
    assert first.writes_enabled is True
    assert first.scopes == frozenset({Scope.MEMORY_READ, Scope.MEMORY_WRITE, Scope.SKILLS_READ})

    # Second connection: read-only defaults, writes off.
    _, second = _run_flow(provider, "client-b", requested_scopes=None)
    assert second.writes_enabled is False
    assert second.scopes == frozenset({Scope.MEMORY_READ, Scope.SKILLS_READ})


# -- DCR persistence (C2.8) --------------------------------------------------


def test_dcr_client_persists_to_oauth_clients_and_is_retrievable(
    session: Session, workspace: Workspace
) -> None:
    provider = _make_provider(session)
    info = OAuthClientInformationFull(
        client_id="dcr-abc123",
        redirect_uris=[AnyUrl("http://127.0.0.1/callback")],
        client_secret="minted-secret",  # noqa: S106 - test client secret
        token_endpoint_auth_method="client_secret_post",  # noqa: S106 - auth-method name
        grant_types=["authorization_code", "refresh_token"],
    )
    _run(provider.register_client(info))

    row = session.scalars(select(OAuthClient).where(OAuthClient.client_id == "dcr-abc123")).one()
    assert row.kind is OAuthClientKind.DCR
    assert row.client_metadata["client_id"] == "dcr-abc123"

    loaded = _run(provider.get_client("dcr-abc123"))
    assert isinstance(loaded, PurseClient)
    assert loaded.client_secret == "minted-secret"  # noqa: S105 - test client secret
    # a DCR client is loopback-aware too (Claude Code registers via DCR + PKCE)
    resolved = loaded.validate_redirect_uri(AnyUrl("http://127.0.0.1:50505/callback"))
    assert str(resolved).startswith("http://127.0.0.1:50505/callback")


# -- PAT as a TokenVerifier (mode 6, the MultiAuth seam) ---------------------


def test_pat_verifier_accepts_a_pat_and_rejects_the_rest(
    session: Session, workspace: Workspace
) -> None:
    from purse.auth.oauth.pat_verifier import PatVerifier
    from purse.auth.provisioning import mint_pat

    connection, token = mint_pat(
        session,
        workspace_id=workspace.id,
        client_name="codex",
        scopes=["memory:read"],
        writes_enabled=False,
    )
    verifier = PatVerifier(_scope_factory(session))

    access = _run(verifier.verify_token(token.reveal()))
    assert access is not None
    ctx = auth_context_from_access_token(access)
    assert ctx.connection_id == connection.id
    assert ctx.workspace_id == workspace.id
    assert ctx.scopes == frozenset({Scope.MEMORY_READ})

    # a well-formed but unknown PAT (and any junk) resolves to None, not an error
    assert _run(verifier.verify_token("purse_pat_" + "z" * 43)) is None
    assert _run(verifier.verify_token("not-a-token")) is None

"""C2.3-C2.9 without a database: metadata, loopback, PKCE, consent, codes, claims.

These exercise the OAuth provider's building blocks directly — no MCP server, no
browser, no Postgres. The headline is :func:`test_metadata_advertises_the_cimd_gate`:
the two flags Anthropic requires before it will pick CIMD instead of silently
downgrading to DCR.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import uuid
from collections.abc import Callable
from contextlib import AbstractContextManager

import pytest
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from mcp.shared.auth import InvalidRedirectUriError
from pydantic import AnyHttpUrl, AnyUrl
from sqlalchemy.orm import Session

from purse.auth.context import AuthContext
from purse.auth.oauth.claims import (
    CLAIM_CONNECTION_ID,
    auth_context_from_access_token,
    build_access_token,
)
from purse.auth.oauth.consent import (
    ConsentError,
    PendingAuthorization,
    consume_nonce,
    new_nonce,
    render_consent_page,
    sign_pending,
    unsign_pending,
)
from purse.auth.oauth.loopback import PurseClient, matches_loopback_redirect_uri
from purse.auth.oauth.metadata import (
    CHATGPT_CALLBACK,
    CLAUDE_AI_CALLBACK,
    CLAUDE_COM_CALLBACK,
    KNOWN_CLIENT_CALLBACKS,
    purse_authorization_server_metadata,
)
from purse.auth.oauth.provider import PurseOAuthProvider, StaticClient
from purse.auth.oauth.state import (
    AuthorizationCodeStore,
    Grant,
    verify_code_challenge,
)
from purse.auth.scopes import Scope
from purse.db.models import AuthMode


def _session_factory_that_must_not_run() -> Callable[[], AbstractContextManager[Session]]:
    def factory() -> AbstractContextManager[Session]:
        raise AssertionError("no database access expected in this test")

    return factory


def _pkce_pair(verifier: str) -> tuple[str, str]:
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    )
    return verifier, challenge


# -- CIMD anti-downgrade metadata (the critical assertion) -------------------


def test_metadata_advertises_the_cimd_gate() -> None:
    """Both flags Anthropic gates CIMD on, or it silently downgrades to DCR."""
    metadata = purse_authorization_server_metadata(
        base_url=AnyHttpUrl("https://vault.example.com"),
        issuer_url=AnyHttpUrl("https://vault.example.com"),
        client_registration_options=ClientRegistrationOptions(
            enabled=True, valid_scopes=["memory:read"]
        ),
        revocation_options=RevocationOptions(enabled=True),
    )
    dumped = metadata.model_dump()
    assert dumped["client_id_metadata_document_supported"] is True
    assert "none" in dumped["token_endpoint_auth_methods_supported"]
    assert "private_key_jwt" in dumped["token_endpoint_auth_methods_supported"]
    # RFC 9207 issuer parameter, expected to become mandatory.
    assert dumped["authorization_response_iss_parameter_supported"] is True
    # PKCE S256 is mandatory and must be advertised (clients refuse without it).
    assert dumped["code_challenge_methods_supported"] == ["S256"]


def test_metadata_issuer_can_differ_from_operational_base() -> None:
    metadata = purse_authorization_server_metadata(
        base_url=AnyHttpUrl("https://internal.host:8000"),
        issuer_url=AnyHttpUrl("https://vault.example.com"),
        client_registration_options=ClientRegistrationOptions(enabled=True),
        revocation_options=RevocationOptions(enabled=True),
    )
    assert str(metadata.issuer) == "https://vault.example.com/"
    # Endpoints stay on the operational base where the routes are mounted.
    assert str(metadata.token_endpoint).startswith("https://internal.host:8000")


def test_provider_routes_carry_the_patched_metadata() -> None:
    provider = PurseOAuthProvider(
        base_url="https://vault.example.com",
        session_scope_factory=_session_factory_that_must_not_run(),
        secret="signing-secret",  # noqa: S106 - test fixture value, not a real credential
    )
    from starlette.routing import Route

    paths = {route.path for route in provider.get_routes("/mcp") if isinstance(route, Route)}
    assert "/.well-known/oauth-authorization-server" in paths
    assert "/authorize" in paths
    assert "/token" in paths
    assert "/register" in paths  # DCR
    assert "/revoke" in paths
    assert "/purse/oauth/consent" in paths  # the approve page


def test_callback_allowlist_covers_both_claude_hosts() -> None:
    """PRD §8.5: claude.ai and claude.com are distinct OAuth identities."""
    assert CLAUDE_AI_CALLBACK == "https://claude.ai/api/mcp/auth_callback"
    assert CLAUDE_COM_CALLBACK == "https://claude.com/api/mcp/auth_callback"
    assert CLAUDE_AI_CALLBACK in KNOWN_CLIENT_CALLBACKS
    assert CLAUDE_COM_CALLBACK in KNOWN_CLIENT_CALLBACKS
    # ChatGPT's callback is distinct and never assumed to share Claude's identity.
    assert CHATGPT_CALLBACK not in {CLAUDE_AI_CALLBACK, CLAUDE_COM_CALLBACK}


# -- port-agnostic loopback (C2.6) -------------------------------------------


@pytest.mark.parametrize(
    ("registered", "requested"),
    [
        ("http://127.0.0.1/callback", "http://127.0.0.1:54321/callback"),
        ("http://localhost/callback", "http://localhost:8901/callback"),
        ("http://[::1]/callback", "http://[::1]:7777/callback"),
        ("http://127.0.0.1/callback", "http://127.0.0.1/callback"),
    ],
)
def test_loopback_accepts_any_port(registered: str, requested: str) -> None:
    assert matches_loopback_redirect_uri(AnyUrl(requested), AnyUrl(registered))


@pytest.mark.parametrize(
    ("registered", "requested"),
    [
        # different host is never a loopback match
        ("http://localhost/callback", "http://evil.com:80/callback"),
        # userinfo smuggling: http://localhost@evil.com
        ("http://localhost/callback", "http://localhost@evil.com/callback"),
        # different path
        ("http://127.0.0.1/callback", "http://127.0.0.1:9/other"),
        # non-loopback registration keeps exact-match (port matters)
        ("https://app.example.com/cb", "https://app.example.com:8443/cb"),
    ],
)
def test_loopback_rejects_non_matches(registered: str, requested: str) -> None:
    assert not matches_loopback_redirect_uri(AnyUrl(requested), AnyUrl(registered))


def test_purse_client_validate_redirect_uri_uses_loopback() -> None:
    client = PurseClient(
        client_id="claude-code",
        redirect_uris=[AnyUrl("http://127.0.0.1/callback")],
        application_type="native",
    )
    resolved = client.validate_redirect_uri(AnyUrl("http://127.0.0.1:61234/callback"))
    assert str(resolved).startswith("http://127.0.0.1:61234/callback")
    with pytest.raises(InvalidRedirectUriError):
        client.validate_redirect_uri(AnyUrl("http://127.0.0.1:61234/evil"))


def test_web_application_type_cannot_use_loopback_http() -> None:
    """SEP-837: a web client must not smuggle a loopback http redirect."""
    client = PurseClient(
        client_id="webby",
        redirect_uris=[AnyUrl("http://localhost/callback")],
        application_type="web",
    )
    with pytest.raises(InvalidRedirectUriError):
        client.validate_redirect_uri(AnyUrl("http://localhost:5000/callback"))


# -- static client lookup (C2.7) ---------------------------------------------


def test_static_client_lookup_returns_a_loopback_aware_client() -> None:
    provider = PurseOAuthProvider(
        base_url="https://vault.example.com",
        session_scope_factory=_session_factory_that_must_not_run(),
        secret="signing-secret",  # noqa: S106 - test fixture value, not a real credential
        static_clients=[
            StaticClient(
                client_id="cursor",
                redirect_uris=("http://127.0.0.1/callback",),
                scopes=("memory:read",),
                client_name="Cursor",
            )
        ],
    )
    client = asyncio.run(provider.get_client("cursor"))
    assert client is not None
    assert isinstance(client, PurseClient)
    assert client.token_endpoint_auth_method == "none"  # noqa: S105 - auth-method name, public client + PKCE
    # loopback port flexibility flows through the static client
    assert str(client.validate_redirect_uri(AnyUrl("http://127.0.0.1:5555/callback"))).startswith(
        "http://127.0.0.1:5555"
    )


def test_known_cimd_client_resolves_locally_without_a_fetch() -> None:
    # Claude Code's CIMD document is behind a CDN that 403s datacenter IPs, so the
    # SSRF fetch path can't reach it from a hosted instance. The bundled seed must
    # resolve it with no network call, and with loopback-port flexibility so an
    # ephemeral callback port is accepted.
    provider = PurseOAuthProvider(
        base_url="https://vault.example.com",
        session_scope_factory=_session_factory_that_must_not_run(),
        secret="signing-secret",  # noqa: S106 - test fixture value, not a real credential
    )
    client_id = "https://claude.ai/oauth/claude-code-client-metadata"
    client = asyncio.run(provider.get_client(client_id))
    assert client is not None
    assert isinstance(client, PurseClient)
    assert client.client_name == "Claude Code"
    assert client.token_endpoint_auth_method == "none"  # noqa: S105 - auth-method name, public client
    # Registered with the scopes it may request, or the SDK rejects the authorize
    # with invalid_scope before the consent page (what's granted is still policy).
    assert "memory:read" in (client.scope or "")
    assert "memory:write" in (client.scope or "")
    assert str(client.validate_redirect_uri(AnyUrl("http://localhost:3118/callback"))).startswith(
        "http://localhost:3118"
    )


def test_unknown_client_id_resolves_to_none_without_db() -> None:
    provider = PurseOAuthProvider(
        base_url="https://vault.example.com",
        session_scope_factory=_session_factory_that_must_not_run(),
        secret="signing-secret",  # noqa: S106 - test fixture value, not a real credential
    )
    # A CIMD-looking https client_id is handled by the CIMD manager (no DB), and
    # a fetch of a non-existent document resolves to None rather than a DB hit.
    assert asyncio.run(provider.get_client("https://example.com/.well-known/nope")) is None


# -- PKCE (C2.3) -------------------------------------------------------------


def test_pkce_verifier_accepts_the_matching_verifier() -> None:
    verifier, challenge = _pkce_pair("a-perfectly-good-code-verifier-value-01")
    assert verify_code_challenge(verifier, challenge)


def test_pkce_verifier_rejects_a_bad_code_verifier() -> None:
    _, challenge = _pkce_pair("a-perfectly-good-code-verifier-value-01")
    assert not verify_code_challenge("the-wrong-verifier", challenge)


# -- signed consent state (C2.3) ---------------------------------------------


def _pending() -> PendingAuthorization:
    return PendingAuthorization(
        client_id="cursor",
        client_name="Cursor",
        redirect_uri="http://127.0.0.1/callback",
        redirect_uri_provided_explicitly=True,
        scopes=("memory:read",),
        code_challenge="challenge",
        state="opaque-state",
        resource=None,
        nonce=new_nonce(),
    )


def test_signed_consent_round_trips() -> None:
    pending = _pending()
    token = sign_pending(pending, "secret")
    assert unsign_pending(token, "secret", max_age=300) == pending


def test_signed_consent_rejects_tampering() -> None:
    token = sign_pending(_pending(), "secret")
    tampered = token[:-3] + ("aaa" if not token.endswith("aaa") else "bbb")
    with pytest.raises(ConsentError):
        unsign_pending(tampered, "secret", max_age=300)


def test_signed_consent_rejects_a_wrong_secret() -> None:
    token = sign_pending(_pending(), "secret")
    with pytest.raises(ConsentError):
        unsign_pending(token, "a-different-secret", max_age=300)


def test_consent_nonce_is_single_use() -> None:
    pending = _pending()
    seen: set[str] = set()
    consume_nonce(pending.nonce, seen)  # first approval
    with pytest.raises(ConsentError):
        consume_nonce(pending.nonce, seen)  # replay


def test_consent_page_renders_scopes_and_the_writes_note() -> None:
    page = render_consent_page(
        client_name="Cursor <x>",
        scopes=("memory:read", "memory:write"),
        writes_enabled=True,
        first_connection=True,
        txn="signed-token",
    )
    assert "memory:read" in page
    assert "writes are on" in page
    assert "Cursor &lt;x&gt;" in page  # client name is HTML-escaped
    assert "signed-token" in page


# -- authorization code lifecycle (C2.3) -------------------------------------


def _grant() -> Grant:
    return Grant(
        workspace_id=uuid.uuid4(),
        client_id="cursor",
        client_name="Cursor",
        scopes=("memory:read",),
        writes_enabled=True,
        auth_mode=AuthMode.OAUTH_STATIC,
        resource=None,
    )


def test_authorization_code_is_single_use() -> None:
    store = AuthorizationCodeStore()
    code = store.issue(
        client_id="cursor",
        redirect_uri=AnyUrl("http://127.0.0.1/callback"),
        redirect_uri_provided_explicitly=True,
        code_challenge="challenge",
        grant=_grant(),
    )
    assert store.load(code) is not None
    assert store.consume(code) is not None
    assert store.load(code) is None  # gone after one exchange
    assert store.consume(code) is None


def test_authorization_code_expires() -> None:
    clock = {"now": 1_000.0}
    store = AuthorizationCodeStore(ttl_seconds=60, clock=lambda: clock["now"])
    code = store.issue(
        client_id="cursor",
        redirect_uri=AnyUrl("http://127.0.0.1/callback"),
        redirect_uri_provided_explicitly=True,
        code_challenge="challenge",
        grant=_grant(),
    )
    clock["now"] = 1_061.0  # 61s later, past the 60s TTL
    assert store.load(code) is None


# -- claims <-> AuthContext seam (C4) ----------------------------------------


def test_access_token_claims_round_trip_to_auth_context() -> None:
    connection_id = uuid.uuid4()
    workspace_id = uuid.uuid4()
    token = build_access_token(
        token="purse_at_example",  # noqa: S106 - test fixture value, not a real credential
        connection_id=connection_id,
        workspace_id=workspace_id,
        scopes=["memory:read", "memory:write"],
        writes_enabled=True,
        client_name="Cursor",
    )
    ctx = auth_context_from_access_token(token)
    assert isinstance(ctx, AuthContext)
    assert ctx.connection_id == connection_id
    assert ctx.workspace_id == workspace_id
    assert ctx.scopes == frozenset({Scope.MEMORY_READ, Scope.MEMORY_WRITE})
    assert ctx.writes_enabled is True
    assert ctx.client_name == "Cursor"


def test_auth_context_rejects_a_foreign_token() -> None:
    from fastmcp.server.auth.auth import AccessToken

    foreign = AccessToken(token="x", client_id="c", scopes=[], expires_at=None, claims={})  # noqa: S106 - test fixture value, not a real credential
    with pytest.raises(KeyError):
        auth_context_from_access_token(foreign)
    assert CLAIM_CONNECTION_ID not in foreign.claims

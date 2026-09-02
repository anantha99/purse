"""Authorization-server metadata with the CIMD anti-downgrade flags (C2.4, C2.5, C2.9).

A vanilla :class:`OAuthProvider` advertises neither
``client_id_metadata_document_supported`` nor ``"none"`` in
``token_endpoint_auth_methods_supported``. Anthropic's connector selection rule
requires **both** before it will pick CIMD; miss either and Claude silently
downgrades to DCR (spike §1, verified against the installed SDK's
``build_metadata`` at ``mcp/server/auth/routes.py:178``). This module rebuilds
the RFC 8414 metadata with both present, plus the RFC 9207 ``iss`` flag the spec
expects to become mandatory.

The callback allowlist (PRD §8.5) lives here too: Claude uses two distinct hosts
(``claude.ai`` and ``claude.com``) and never assume they share an OAuth
identity. ChatGPT's callback is distinct again and arrives inside its CIMD
document, so it is not hard-coded — it is documented below for the operator who
pre-registers a static client.
"""

from __future__ import annotations

from mcp.server.auth.routes import build_metadata
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from mcp.shared.auth import OAuthMetadata
from pydantic import AnyHttpUrl

__all__ = [
    "CHATGPT_CALLBACK",
    "CIMD_TOKEN_ENDPOINT_AUTH_METHODS",
    "CLAUDE_AI_CALLBACK",
    "CLAUDE_COM_CALLBACK",
    "KNOWN_CLIENT_CALLBACKS",
    "purse_authorization_server_metadata",
]

#: Claude's two callback hosts (PRD §8.5). Both are registered because
#: ``claude.ai`` and ``claude.com`` are different OAuth identities — a client
#: pre-registered with one cannot redirect to the other.
CLAUDE_AI_CALLBACK = "https://claude.ai/api/mcp/auth_callback"
CLAUDE_COM_CALLBACK = "https://claude.com/api/mcp/auth_callback"

#: ChatGPT's callback is distinct from Claude's and is carried in ChatGPT's own
#: CIMD document, so the CIMD path validates it from there rather than from this
#: list. Recorded for the operator who registers ChatGPT as a *static* client.
CHATGPT_CALLBACK = "https://chatgpt.com/connector_platform_oauth_redirect"

#: Callbacks a freshly registered static client may reuse without the operator
#: retyping them. Advisory: static clients still declare their own redirect URIs.
KNOWN_CLIENT_CALLBACKS: frozenset[str] = frozenset(
    {CLAUDE_AI_CALLBACK, CLAUDE_COM_CALLBACK, CHATGPT_CALLBACK}
)

#: The token-endpoint auth methods Purse advertises. ``"none"`` is the flag half
#: of Anthropic's CIMD gate; ``"private_key_jwt"`` is the CIMD confidential-client
#: method; the two ``client_secret_*`` methods keep static/DCR clients working.
CIMD_TOKEN_ENDPOINT_AUTH_METHODS: list[str] = [
    "none",
    "private_key_jwt",
    "client_secret_post",
    "client_secret_basic",
]


def purse_authorization_server_metadata(
    *,
    base_url: AnyHttpUrl,
    issuer_url: AnyHttpUrl,
    client_registration_options: ClientRegistrationOptions,
    revocation_options: RevocationOptions,
) -> OAuthMetadata:
    """RFC 8414 metadata with the CIMD flags and RFC 9207 ``iss`` set.

    Endpoint URLs are derived from *base_url* (where the routes are actually
    mounted); ``issuer`` is set to *issuer_url* separately, because RFC 8414 §3.3
    requires the advertised ``issuer`` to byte-match the discovery URL clients
    used — which may differ from the operational base when behind a proxy. This
    mirrors what ``OAuthProvider.get_routes`` already does for the issuer, and
    adds the three fields a vanilla provider omits.
    """
    metadata = build_metadata(
        base_url,
        None,
        client_registration_options,
        revocation_options,
    )
    metadata.issuer = issuer_url
    metadata.client_id_metadata_document_supported = True
    metadata.token_endpoint_auth_methods_supported = list(CIMD_TOKEN_ENDPOINT_AUTH_METHODS)
    metadata.authorization_response_iss_parameter_supported = True
    return metadata

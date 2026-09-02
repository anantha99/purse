"""Port-agnostic loopback redirect validation (C2.6, PRD §8.5 mode 5).

The MCP SDK's :meth:`OAuthClientInformationFull.validate_redirect_uri` is
exact-match (``mcp/shared/auth.py:187``). Native CLIs like Claude Code register
``http://127.0.0.1/callback`` and then bind an arbitrary ephemeral port, which
exact-match rejects. RFC 8252 §7.3 says the loopback port is not part of the
identity of the redirect and must be ignored.

This module extends the SDK client with the ~25-line matcher FastMCP already
ships for its proxy (``oauth_proxy/models.py:_matches_registered_loopback_redirect_uri``),
ported here so Purse's own-AS clients get the same behaviour without importing a
private proxy helper. Every component except the port must match; userinfo is
rejected outright to block ``http://localhost@evil.com``; and the loopback check
reuses FastMCP's :func:`is_loopback_host`, which correctly covers ``127.0.0.0/8``,
``::1``, and the reserved ``.localhost`` namespace.
"""

from __future__ import annotations

from urllib.parse import urlparse

from fastmcp.server.auth.redirect_validation import (
    is_loopback_host,
    is_redirect_uri_allowed_for_application_type,
)
from mcp.shared.auth import InvalidRedirectUriError, OAuthClientInformationFull
from pydantic import AnyUrl

__all__ = [
    "PurseClient",
    "matches_loopback_redirect_uri",
    "redirect_uri_is_registered",
]


def _path_or_root(path: str) -> str:
    return path or "/"


def matches_loopback_redirect_uri(requested: AnyUrl, registered: AnyUrl) -> bool:
    """True when *requested* is *registered* differing only in loopback port.

    Ported from FastMCP's proxy matcher. Scheme, host, path, params, query and
    fragment must match exactly; the port is ignored, but only when the
    registered host is a loopback host — a non-loopback registration keeps
    exact-match semantics. Any userinfo on either side is a hard reject.
    """
    req = urlparse(str(requested))
    reg = urlparse(str(registered))

    if req.username or req.password or reg.username or reg.password:
        return False

    req_host = req.hostname.lower() if req.hostname else None
    reg_host = reg.hostname.lower() if reg.hostname else None

    if not is_loopback_host(reg_host):
        return False
    if req_host != reg_host:
        return False

    return (
        req.scheme.lower() == reg.scheme.lower()
        and _path_or_root(req.path) == _path_or_root(reg.path)
        and req.params == reg.params
        and req.query == reg.query
        and req.fragment == reg.fragment
    )


def redirect_uri_is_registered(requested: AnyUrl, registered_uris: list[AnyUrl] | None) -> bool:
    """True when *requested* exactly matches, or loopback-matches, a registered URI."""
    if not registered_uris:
        return False
    return any(
        requested == registered or matches_loopback_redirect_uri(requested, registered)
        for registered in registered_uris
    )


class PurseClient(OAuthClientInformationFull):
    """An SDK client whose redirect validation honours loopback ports.

    Used for static and DCR-registered clients (CIMD clients get FastMCP's own
    ``ProxyDCRClient`` from :class:`CIMDClientManager`). SEP-837
    ``application_type`` is enforced on the same path: a ``"web"`` client cannot
    smuggle a loopback ``http`` redirect past the check, while a ``"native"``
    client (the SDK default) keeps loopback ``http`` on any port.
    """

    def validate_redirect_uri(self, redirect_uri: AnyUrl | None) -> AnyUrl:
        if redirect_uri is not None:
            if not is_redirect_uri_allowed_for_application_type(
                redirect_uri, self.application_type
            ):
                raise InvalidRedirectUriError(
                    f"Redirect URI '{redirect_uri}' is not allowed for "
                    f"application_type '{self.application_type}'."
                )
            if redirect_uri_is_registered(redirect_uri, self.redirect_uris):
                return redirect_uri
            raise InvalidRedirectUriError(
                f"Redirect URI '{redirect_uri}' not registered for client"
            )
        # No redirect_uri supplied: defer to the SDK's single-registered-URI
        # shortcut, which raises when the client has zero or many.
        return super().validate_redirect_uri(redirect_uri)

"""The standalone approve page and its signed, replay-proof state (C2.3, PRD §7.1).

Purse is a single-operator vault, so ``/authorize`` does not silently mint a
token — it renders one bare page: *"<client> requests <scopes> — Approve /
Deny"*. On approve the connection is minted with the consented scopes; the first
connection ever gets writes on (the "writes on" badge) and onboarding scopes,
later ones default read-only (PRD §7.1).

The page carries no server-side session. Instead the pending authorization is
signed with :mod:`itsdangerous` (``PURSE_OAUTH_SECRET``): the approve POST is
trusted only if it presents a token this server signed, so it cannot be forged.
Each token carries a one-time ``nonce`` the provider records on use, so an
approve cannot be replayed into a second connection, and a ``max_age`` bounds the
window regardless.

Everything here is framework-light and unit-testable in isolation; the provider
mounts the two handlers as one Starlette route. This whole surface is standalone
for M2 — C7.5 folds it into the real web UI later, which is why the page is a
single self-contained template.
"""

from __future__ import annotations

import html
import uuid
from collections.abc import MutableSet
from dataclasses import asdict, dataclass
from typing import Any

from itsdangerous import BadData, URLSafeTimedSerializer

__all__ = [
    "CONSENT_PATH",
    "ConsentError",
    "PendingAuthorization",
    "consume_nonce",
    "new_nonce",
    "render_consent_page",
    "sign_pending",
    "unsign_pending",
]

#: Where the approve page is mounted, relative to the server base URL.
CONSENT_PATH = "/purse/oauth/consent"

_SIGNER_SALT = "purse.oauth.consent"


class ConsentError(Exception):
    """A pending-authorization token was forged, expired, tampered, or replayed."""


@dataclass(frozen=True, slots=True)
class PendingAuthorization:
    """The authorization request, held in a signed token between authorize and approve."""

    client_id: str
    client_name: str
    redirect_uri: str
    redirect_uri_provided_explicitly: bool
    scopes: tuple[str, ...]
    code_challenge: str
    state: str | None
    resource: str | None
    nonce: str


def new_nonce() -> str:
    """A fresh single-use nonce for a pending authorization."""
    return uuid.uuid4().hex


def _serializer(secret: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(secret, salt=_SIGNER_SALT)


def sign_pending(pending: PendingAuthorization, secret: str) -> str:
    """Serialise and sign a pending authorization into a URL-safe token."""
    payload = asdict(pending)
    payload["scopes"] = list(pending.scopes)
    result: str = _serializer(secret).dumps(payload)
    return result


def unsign_pending(token: str, secret: str, *, max_age: int) -> PendingAuthorization:
    """Verify a token's signature and age and rebuild the pending authorization.

    Raises :class:`ConsentError` for any bad, tampered, or expired token — the
    :mod:`itsdangerous` failure modes are collapsed into one so the caller cannot
    accidentally build an oracle out of them. Does **not** consume the nonce;
    replay protection is :func:`consume_nonce`, so the GET render and the POST
    submit can both verify the same token.
    """
    try:
        payload = _serializer(secret).loads(token, max_age=max_age)
    except BadData as exc:
        raise ConsentError("invalid or expired authorization request") from exc
    if not isinstance(payload, dict):
        raise ConsentError("malformed authorization request")
    data: dict[str, Any] = payload
    try:
        return PendingAuthorization(
            client_id=str(data["client_id"]),
            client_name=str(data["client_name"]),
            redirect_uri=str(data["redirect_uri"]),
            redirect_uri_provided_explicitly=bool(data["redirect_uri_provided_explicitly"]),
            scopes=tuple(str(scope) for scope in data["scopes"]),
            code_challenge=str(data["code_challenge"]),
            state=None if data["state"] is None else str(data["state"]),
            resource=None if data["resource"] is None else str(data["resource"]),
            nonce=str(data["nonce"]),
        )
    except (KeyError, TypeError) as exc:
        raise ConsentError("malformed authorization request") from exc


def consume_nonce(nonce: str, seen: MutableSet[str]) -> None:
    """Record a nonce as used, or raise :class:`ConsentError` if it already was.

    The single-use half of replay protection: *seen* is the provider-held set of
    spent nonces, so a signed token that is valid but has already been approved
    is refused the second time.
    """
    if nonce in seen:
        raise ConsentError("authorization request already used")
    seen.add(nonce)


def render_consent_page(
    *,
    client_name: str,
    scopes: tuple[str, ...],
    writes_enabled: bool,
    first_connection: bool,
    txn: str,
) -> str:
    """The single self-contained approve page.

    ``client_name`` and scopes are the client's own strings, so they are HTML
    escaped. When *first_connection* is true the page shows the visible "writes
    on" note the onboarding flow (PRD §7.1) calls for.
    """
    safe_name = html.escape(client_name or "This client")
    scope_items = "".join(f"<li><code>{html.escape(scope)}</code></li>" for scope in scopes)
    if not scope_items:
        scope_items = "<li><em>read-only default access</em></li>"
    writes_note = (
        '<p class="writes">This is your first connection, so <strong>writes are on</strong> '
        "and it may save to your vault. You can turn writes off at any time.</p>"
        if writes_enabled and first_connection
        else '<p class="writes">This connection will be <strong>read-only</strong> '
        "until you turn writes on.</p>"
    )
    safe_txn = html.escape(txn)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Authorize {safe_name} — Purse</title>
<style>
  body {{ font-family: system-ui, sans-serif; max-width: 32rem; margin: 4rem auto;
         padding: 0 1rem; color: #1a1a1a; }}
  .card {{ border: 1px solid #ddd; border-radius: 12px; padding: 1.5rem 2rem; }}
  h1 {{ font-size: 1.25rem; }}
  ul {{ padding-left: 1.25rem; }}
  code {{ background: #f2f2f2; padding: 0.1rem 0.35rem; border-radius: 4px; }}
  .writes {{ background: #fff7e6; border-left: 3px solid #e6a700; padding: 0.5rem 0.75rem; }}
  .actions {{ display: flex; gap: 0.75rem; margin-top: 1.5rem; }}
  button {{ font-size: 1rem; padding: 0.6rem 1.2rem; border-radius: 8px; cursor: pointer;
           border: 1px solid #bbb; }}
  button.approve {{ background: #1a7f37; color: #fff; border-color: #1a7f37; }}
</style>
</head>
<body>
  <div class="card">
    <h1>{safe_name} wants to connect to your Purse vault</h1>
    <p>It is requesting these scopes:</p>
    <ul>{scope_items}</ul>
    {writes_note}
    <form method="post" action="{CONSENT_PATH}">
      <input type="hidden" name="txn" value="{safe_txn}">
      <div class="actions">
        <button class="approve" type="submit" name="decision" value="approve">Approve</button>
        <button class="deny" type="submit" name="decision" value="deny">Deny</button>
      </div>
    </form>
  </div>
</body>
</html>"""

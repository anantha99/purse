"""Session auth for the operator dashboard (C7, ``docs/web-api-contract.md``).

Purse is a single-operator vault (PRD §8.1). The dashboard authenticates that
one human with a password, then rides an **opaque signed session token** — no
per-user password in the database (so no migration), no server-side sessions
table.

.. rubric:: The password

``PURSE_OWNER_PASSWORD`` (env) is the whole credential. Login compares it
constant-time (:func:`hmac.compare_digest`). If it is unset, login is disabled
and ``/web/login`` returns a clear :class:`~purse.web.errors.LoginDisabledError`
— the routes still mount, boot never crashes.

.. rubric:: The token

On success a token is signed with :mod:`itsdangerous`
(``URLSafeTimedSerializer``, ``PURSE_SESSION_SECRET``, ~12 h expiry). It encodes
only the operator's user id + Personal workspace id — the same "the operator"
the OAuth provider resolves (:func:`resolve_operator`, mirroring
``purse.auth.oauth.provider._default_resolve_workspace_id``). Nothing secret is
in the token; its integrity is the signature, its lifetime is the ``max_age``.

.. rubric:: The provenance connection

Memory and skill *writes* the operator makes from the UI still need a
``connections`` row for provenance (every canonical write and audit entry
references one — PRD §10/§13). There is no "web" auth mode, so the dashboard
lazily get-or-creates one dedicated PAT-mode connection with a null
``token_hash`` (it can never authenticate as a PAT) named
:data:`WEB_CONNECTION_CLIENT_NAME`. Operator edits are attributed to it, exactly
as an agent's edits are attributed to its connection.
"""

from __future__ import annotations

import hmac
import os
import uuid
from collections.abc import Mapping
from dataclasses import dataclass

from itsdangerous import BadData, URLSafeTimedSerializer
from sqlalchemy import select
from sqlalchemy.orm import Session

from purse.auth.scopes import Scope, format_scopes
from purse.db.models import AuthMode, Connection, User
from purse.db.repo import Repo, list_user_workspaces
from purse.web.errors import InvalidCredentialsError, LoginDisabledError, UnauthenticatedError

__all__ = [
    "OWNER_PASSWORD_ENV",
    "SESSION_SECRET_ENV",
    "SESSION_TTL_SECONDS",
    "WEB_CONNECTION_CLIENT_NAME",
    "OperatorIdentity",
    "OperatorProvenance",
    "SessionContext",
    "SessionManager",
    "operator_connection_id",
    "resolve_operator",
]

#: Enables login. Unset → ``/web/login`` returns ``LOGIN_DISABLED``.
OWNER_PASSWORD_ENV = "PURSE_OWNER_PASSWORD"  # noqa: S105 - env var name, not a secret
#: Signs session tokens. May be a distinct secret from ``PURSE_OAUTH_SECRET``.
SESSION_SECRET_ENV = "PURSE_SESSION_SECRET"  # noqa: S105 - env var name, not a secret

#: ~12 hours, in seconds. The signed token's ``max_age``.
SESSION_TTL_SECONDS = 12 * 60 * 60

_SIGNER_SALT = "purse.web.session"

_PERSONAL_WORKSPACE_NAME = "Personal"

#: ``client_name`` of the connection operator UI writes are attributed to.
WEB_CONNECTION_CLIENT_NAME = "Purse Web (operator)"

#: Scopes the operator provenance connection carries. The operator has full
#: reach over their own vault; these exist only so the connection row is a
#: legible grant and its writes pass the same shape every other write does.
_WEB_CONNECTION_SCOPES = (
    Scope.MEMORY_READ,
    Scope.MEMORY_WRITE,
    Scope.SKILLS_READ,
    Scope.SKILLS_WRITE,
)


@dataclass(frozen=True, slots=True)
class SessionContext:
    """What a verified session token resolves to: the operator and their workspace."""

    user_id: uuid.UUID
    workspace_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class OperatorIdentity:
    """The resolved operator: who they are and where their vault lives."""

    user_id: uuid.UUID
    workspace_id: uuid.UUID
    email: str
    workspace_name: str


@dataclass(frozen=True, slots=True)
class OperatorProvenance:
    """Provenance for an operator write.

    Satisfies :class:`purse.memory.context.WriteContext` and
    :class:`purse.skills.context.SkillContext` structurally, so the memory and
    skills services take it with no import in either direction.
    """

    connection_id: uuid.UUID
    workspace_id: uuid.UUID
    agent_id: str | None = None


def resolve_operator(session: Session) -> OperatorIdentity:
    """The single operator: first user, their ``Personal`` workspace.

    The same resolution the OAuth provider uses
    (``purse.auth.oauth.provider._default_resolve_workspace_id``) — one user,
    one vault — but returning the identity the session response needs, not only
    the workspace id.

    Raises :class:`~purse.web.errors.LoginDisabledError` when no operator vault
    has been provisioned yet: there is nothing to log into.
    """
    user = session.scalars(select(User).order_by(User.created_at, User.id)).first()
    if user is None:
        raise LoginDisabledError("no operator vault has been provisioned")
    workspaces = list_user_workspaces(session, user.id)
    if not workspaces:
        raise LoginDisabledError("the operator has no workspace")
    workspace = next(
        (ws for ws in workspaces if ws.name == _PERSONAL_WORKSPACE_NAME),
        workspaces[0],
    )
    return OperatorIdentity(
        user_id=user.id,
        workspace_id=workspace.id,
        email=user.email,
        workspace_name=workspace.name,
    )


def operator_connection_id(repo: Repo) -> uuid.UUID:
    """Get-or-create the dedicated provenance connection for operator UI writes.

    A PAT-mode row with a null ``token_hash`` — real provenance, but it can
    never authenticate as a token. Reused across writes; recreated only if a
    previous one was revoked.
    """
    for connection in repo.list_connections():
        if connection.client_name == WEB_CONNECTION_CLIENT_NAME and connection.revoked_at is None:
            return connection.id
    created: Connection = repo.add_connection(
        client_name=WEB_CONNECTION_CLIENT_NAME,
        auth_mode=AuthMode.PAT,
        scopes=format_scopes(_WEB_CONNECTION_SCOPES),
        writes_enabled=True,
        token_hash=None,
    )
    return created.id


class SessionManager:
    """Signs and verifies session tokens, and checks the operator password.

    Constructed with the resolved config (secret + password), so the routes take
    an instance and the app factory owns reading the environment. When either is
    absent login is disabled — every path that needs one raises a clear error
    rather than crashing.
    """

    def __init__(
        self,
        *,
        secret: str | None,
        password: str | None,
        ttl_seconds: int = SESSION_TTL_SECONDS,
    ) -> None:
        self._secret = (secret or "").strip() or None
        self._password = password if password else None
        self._ttl = ttl_seconds
        self._serializer = (
            URLSafeTimedSerializer(self._secret, salt=_SIGNER_SALT)
            if self._secret is not None
            else None
        )

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> SessionManager:
        """Read ``PURSE_OWNER_PASSWORD`` + ``PURSE_SESSION_SECRET`` from the env."""
        source = os.environ if env is None else env
        return cls(
            secret=source.get(SESSION_SECRET_ENV),
            password=source.get(OWNER_PASSWORD_ENV),
        )

    @property
    def login_enabled(self) -> bool:
        """True when both a password and a signing secret are configured."""
        return self._password is not None and self._serializer is not None

    def _require_login_configured(self) -> None:
        if self._password is None:
            raise LoginDisabledError("operator password not configured")
        if self._serializer is None:
            raise LoginDisabledError("operator session secret not configured")

    def verify_password(self, provided: str) -> None:
        """Check *provided* against the operator password, constant-time.

        Raises :class:`~purse.web.errors.LoginDisabledError` when login is not
        configured, and :class:`~purse.web.errors.InvalidCredentialsError` on a
        wrong password. The comparison is always
        :func:`hmac.compare_digest` — no length short-circuit — so a wrong
        password of any length takes the same path.
        """
        self._require_login_configured()
        # _require_login_configured guarantees a non-None password; bind it locally
        # so mypy narrows without an `assert` (which ruff's S101 forbids here).
        configured = self._password or ""
        if not hmac.compare_digest(provided.encode("utf-8"), configured.encode("utf-8")):
            raise InvalidCredentialsError("wrong operator password")

    def issue_token(self, *, user_id: uuid.UUID, workspace_id: uuid.UUID) -> str:
        """Sign a session token for the operator + workspace.

        Callers gate on a successful :meth:`verify_password` first; this raises
        :class:`~purse.web.errors.LoginDisabledError` if the secret is missing.
        """
        if self._serializer is None:
            raise LoginDisabledError("operator session secret not configured")
        token: str = self._serializer.dumps(
            {"user_id": str(user_id), "workspace_id": str(workspace_id)}
        )
        return token

    def verify_token(self, token: str) -> SessionContext:
        """Verify a token's signature + age and rebuild the session context.

        Any bad, tampered, or expired token — and any instance with no secret
        configured — raises :class:`~purse.web.errors.UnauthenticatedError`. The
        :mod:`itsdangerous` failure modes are collapsed into one so the caller
        cannot build an oracle out of them.
        """
        if self._serializer is None:
            raise UnauthenticatedError("session verification is not configured")
        try:
            payload = self._serializer.loads(token, max_age=self._ttl)
        except BadData as exc:
            raise UnauthenticatedError("invalid or expired session") from exc
        if not isinstance(payload, dict):
            raise UnauthenticatedError("malformed session")
        try:
            return SessionContext(
                user_id=uuid.UUID(str(payload["user_id"])),
                workspace_id=uuid.UUID(str(payload["workspace_id"])),
            )
        except (KeyError, ValueError) as exc:
            raise UnauthenticatedError("malformed session") from exc

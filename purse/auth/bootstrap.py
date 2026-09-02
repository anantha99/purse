"""First-boot bootstrap: a user, a workspace, and a PAT printed once (§6, §7.1).

The self-host packaging bar is "one command, boots in seconds, **credentials
printed on first boot**". This module is that promise at library level; C9.1
wires it into the production compose entrypoint.

Run it directly::

    python -m purse.auth.bootstrap

Idempotent where identity is concerned — the user and the ``Personal``
workspace are created only if missing — but **not** where the token is. Every
run mints a fresh PAT and prints it. It cannot do otherwise: the old token was
never stored in a readable form, so "reprint the existing one" is not an
operation the database can serve. Re-running is therefore a supported recovery
path for a lost credential, and the old connections stay live until revoked
(see :func:`purse.auth.provisioning.revoke_connection`).
"""

from __future__ import annotations

import os
import sys
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from purse.auth.provisioning import mint_pat
from purse.auth.scopes import ONBOARDING_SCOPES
from purse.auth.tokens import RawToken
from purse.db.config import DatabaseUrlError
from purse.db.models import User, Workspace
from purse.db.repo import create_user, create_workspace, list_user_workspaces
from purse.db.session import create_db_engine, session_scope
from purse.skills import seed_default_skills

__all__ = [
    "DEFAULT_USER_EMAIL",
    "ONBOARDING_CLIENT_NAME",
    "PERSONAL_WORKSPACE_NAME",
    "USER_EMAIL_ENV",
    "BootstrapResult",
    "bootstrap",
    "ensure_user",
    "ensure_workspace",
    "format_credentials",
    "main",
    "owner_email",
]

#: Overrides the owner's email on first boot. Anything later is a UI concern.
USER_EMAIL_ENV = "PURSE_USER_EMAIL"

#: Self-host default. Not a deliverable address — it identifies the single
#: local owner of a vault nobody else can reach.
DEFAULT_USER_EMAIL = "owner@localhost"

#: The workspace every vault starts with (PRD §7.1).
PERSONAL_WORKSPACE_NAME = "Personal"

#: ``client_name`` of the connection minted on first boot.
ONBOARDING_CLIENT_NAME = "onboarding"


@dataclass(frozen=True, slots=True)
class BootstrapResult:
    """What first boot produced. ``token`` is the only copy of the credential."""

    user_id: uuid.UUID
    email: str
    workspace_id: uuid.UUID
    workspace_name: str
    connection_id: uuid.UUID
    token: RawToken
    user_created: bool
    workspace_created: bool


def owner_email(env: dict[str, str] | None = None) -> str:
    """The configured owner email, or the self-host default."""
    source = os.environ if env is None else env
    return (source.get(USER_EMAIL_ENV) or "").strip() or DEFAULT_USER_EMAIL


def ensure_user(session: Session, *, email: str) -> tuple[User, bool]:
    """Get-or-create the vault owner. Returns ``(user, created)``.

    Own query rather than a ``Repo`` call: ``Repo`` is workspace-scoped and has
    no by-email lookup (identity predates any workspace).
    """
    existing = session.scalars(select(User).where(User.email == email)).one_or_none()
    if existing is not None:
        return existing, False
    return create_user(session, email=email), True


def ensure_workspace(
    session: Session, *, user_id: uuid.UUID, name: str = PERSONAL_WORKSPACE_NAME
) -> tuple[Workspace, bool]:
    """Get-or-create a named workspace in a user's vault. Returns ``(workspace, created)``.

    ``(user_id, name)`` is unique in the schema, so matching on the name is the
    same identity the database enforces.
    """
    for workspace in list_user_workspaces(session, user_id):
        if workspace.name == name:
            return workspace, False
    return create_workspace(session, user_id=user_id, name=name), True


def bootstrap(
    session: Session,
    *,
    email: str | None = None,
    workspace_name: str = PERSONAL_WORKSPACE_NAME,
    client_name: str = ONBOARDING_CLIENT_NAME,
) -> BootstrapResult:
    """Ensure owner + workspace exist, then mint a fresh onboarding PAT.

    The onboarding connection gets :data:`~purse.auth.scopes.ONBOARDING_SCOPES`
    and ``writes_enabled=True`` — PRD §7.1 makes the first connection the one
    that can write, with the "writes on" badge and a one-tap revoke, so the
    guided "save that I prefer TypeScript" moment works without a settings
    detour. Later connections default to read-only.
    """
    resolved_email = email if email is not None else owner_email()
    user, user_created = ensure_user(session, email=resolved_email)
    workspace, workspace_created = ensure_workspace(session, user_id=user.id, name=workspace_name)
    connection, token = mint_pat(
        session,
        workspace_id=workspace.id,
        client_name=client_name,
        scopes=ONBOARDING_SCOPES,
        writes_enabled=True,
    )
    # Preload the bundled skills (the save-policy that tells agents what's worth
    # remembering). Idempotent across reboots, so re-running bootstrap is safe.
    # A server-side seed, not a scoped client write — attributed to the onboarding
    # connection for provenance.
    seed_default_skills(session, workspace_id=workspace.id, connection_id=connection.id)
    return BootstrapResult(
        user_id=user.id,
        email=user.email,
        workspace_id=workspace.id,
        workspace_name=workspace.name,
        connection_id=connection.id,
        token=token,
        user_created=user_created,
        workspace_created=workspace_created,
    )


def format_credentials(result: BootstrapResult) -> str:
    """The credentials block printed on first boot.

    This is the *only* place in Purse that renders a raw token, and the call to
    ``reveal()`` below is the only one outside tests. It goes to stdout, never
    to the logger — a log file is a copy of the credential nobody chose to keep.
    """
    return "\n".join(
        [
            "",
            "  Purse is ready.",
            "",
            f"  Owner           {result.email}",
            f"  Workspace       {result.workspace_name}  ({result.workspace_id})",
            f"  Connection      {result.connection_id}  ({ONBOARDING_CLIENT_NAME})",
            f"  Scopes          {', '.join(sorted(s.value for s in ONBOARDING_SCOPES))}",
            "  Writes          enabled",
            "",
            "  Personal access token — shown once, store it now:",
            "",
            f"      {result.token.reveal()}",
            "",
            "  It is stored only as a hash; there is no way to print it again.",
            "  Lost it? Re-run this command for a new one, then revoke the old",
            "  connection from the connections screen.",
            "",
        ]
    )


def main() -> int:
    """``python -m purse.auth.bootstrap`` — bootstrap against ``DATABASE_URL``."""
    try:
        engine = create_db_engine()
    except DatabaseUrlError as exc:
        print(f"purse bootstrap: {exc}", file=sys.stderr)
        return 2
    try:
        with session_scope(engine) as session:
            result = bootstrap(session)
            credentials = format_credentials(result)
    finally:
        engine.dispose()
    print(credentials)
    return 0


if __name__ == "__main__":  # pragma: no cover - process entrypoint
    raise SystemExit(main())

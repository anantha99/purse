"""C2.1 against a real database: mint, authenticate, revoke, and stay in your workspace.

Marked ``db``: these prove behaviour the schema enforces (the partial unique
index on ``token_hash``, the workspace foreign key), which a fake would only
prove about the fake.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from purse.auth.context import require_scope
from purse.auth.errors import (
    AUTHENTICATION_FAILED_MESSAGE,
    AuthenticationError,
    ErrorCode,
    ScopeError,
)
from purse.auth.pat import authenticate_pat
from purse.auth.provisioning import ProvisioningError, mint_pat, revoke_connection
from purse.auth.scopes import (
    DEFAULT_CONNECTION_SCOPES,
    ONBOARDING_SCOPES,
    Scope,
    UnknownScopeError,
)
from purse.auth.tokens import TOKEN_PREFIX, generate_token, hash_token
from purse.db.models import AuthMode, Connection, Workspace
from purse.db.repo import NotFoundError

from .conftest import TwoWorkspaces

pytestmark = pytest.mark.db


# -- minting -----------------------------------------------------------------


def test_mint_stores_a_hash_and_never_the_token(session: Session, workspace: Workspace) -> None:
    connection, token = mint_pat(
        session,
        workspace_id=workspace.id,
        client_name="codex",
        scopes=ONBOARDING_SCOPES,
        writes_enabled=True,
    )
    assert connection.auth_mode is AuthMode.PAT
    assert connection.token_hash == hash_token(token.reveal())
    assert connection.token_hash is not None
    assert token.reveal() not in connection.token_hash
    assert connection.revoked_at is None
    assert sorted(connection.scopes) == ["memory:read", "memory:write", "skills:read"]
    assert token.reveal().startswith(TOKEN_PREFIX)


def test_mint_defaults_to_writes_off(session: Session, workspace: Workspace) -> None:
    """Later connections are read-only until the user opts in (PRD §7.1)."""
    connection, _ = mint_pat(
        session,
        workspace_id=workspace.id,
        client_name="openclaw",
        scopes=DEFAULT_CONNECTION_SCOPES,
    )
    assert connection.writes_enabled is False


def test_mint_accepts_scope_strings_as_well_as_enum(session: Session, workspace: Workspace) -> None:
    connection, _ = mint_pat(
        session,
        workspace_id=workspace.id,
        client_name="cursor",
        scopes=["skills:read", Scope.MEMORY_READ],
    )
    assert sorted(connection.scopes) == ["memory:read", "skills:read"]


def test_mint_rejects_unknown_scopes(session: Session, workspace: Workspace) -> None:
    with pytest.raises(UnknownScopeError):
        mint_pat(
            session,
            workspace_id=workspace.id,
            client_name="sneaky",
            scopes=["memory:read", "memory:admin"],
        )


@pytest.mark.parametrize("client_name", ["", "   ", "\t\n"])
def test_mint_rejects_an_empty_client_name(
    session: Session, workspace: Workspace, client_name: str
) -> None:
    with pytest.raises(ProvisioningError):
        mint_pat(
            session, workspace_id=workspace.id, client_name=client_name, scopes=[Scope.MEMORY_READ]
        )


def test_two_mints_produce_different_tokens(session: Session, workspace: Workspace) -> None:
    _, first = mint_pat(
        session, workspace_id=workspace.id, client_name="a", scopes=[Scope.MEMORY_READ]
    )
    _, second = mint_pat(
        session, workspace_id=workspace.id, client_name="b", scopes=[Scope.MEMORY_READ]
    )
    assert first.reveal() != second.reveal()
    assert hash_token(first.reveal()) != hash_token(second.reveal())


def test_token_hash_is_unique_across_connections(session: Session, workspace: Workspace) -> None:
    """The partial unique index (C1.2) makes a duplicated hash a database error."""
    connection, _ = mint_pat(
        session, workspace_id=workspace.id, client_name="a", scopes=[Scope.MEMORY_READ]
    )
    with pytest.raises(IntegrityError), session.begin_nested():
        session.add(
            Connection(
                workspace_id=workspace.id,
                client_name="collider",
                auth_mode=AuthMode.PAT,
                scopes=["memory:read"],
                writes_enabled=False,
                token_hash=connection.token_hash,
            )
        )
        session.flush()


def test_null_token_hashes_do_not_collide(session: Session, workspace: Workspace) -> None:
    """The index is partial: OAuth connections all have NULL and must coexist."""
    for name in ("claude-code", "claude-desktop", "chatgpt"):
        session.add(
            Connection(
                workspace_id=workspace.id,
                client_name=name,
                auth_mode=AuthMode.OAUTH_DCR,
                scopes=["memory:read"],
                writes_enabled=False,
                token_hash=None,
            )
        )
    session.flush()


# -- authentication ----------------------------------------------------------


def test_mint_then_authenticate_round_trip(session: Session, workspace: Workspace) -> None:
    connection, token = mint_pat(
        session,
        workspace_id=workspace.id,
        client_name="codex",
        scopes=ONBOARDING_SCOPES,
        writes_enabled=True,
    )
    ctx = authenticate_pat(session, token.reveal())
    assert ctx.connection_id == connection.id
    assert ctx.workspace_id == workspace.id
    assert ctx.scopes == ONBOARDING_SCOPES
    assert ctx.writes_enabled is True
    assert ctx.client_name == "codex"
    require_scope(ctx, Scope.MEMORY_WRITE)


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "not-a-token",
        TOKEN_PREFIX,
        TOKEN_PREFIX + "short",
        "PURSE_PAT_" + "a" * 43,
    ],
)
def test_malformed_tokens_are_refused(session: Session, workspace: Workspace, bad: str) -> None:
    mint_pat(session, workspace_id=workspace.id, client_name="codex", scopes=[Scope.MEMORY_READ])
    with pytest.raises(AuthenticationError) as caught:
        authenticate_pat(session, bad)
    assert str(caught.value) == AUTHENTICATION_FAILED_MESSAGE


def test_a_well_formed_but_unknown_token_is_refused(session: Session, workspace: Workspace) -> None:
    mint_pat(session, workspace_id=workspace.id, client_name="codex", scopes=[Scope.MEMORY_READ])
    with pytest.raises(AuthenticationError):
        authenticate_pat(session, generate_token().reveal())


def test_a_near_miss_token_is_refused(session: Session, workspace: Workspace) -> None:
    """One character off is as wrong as anything else — no prefix matching."""
    _, token = mint_pat(
        session, workspace_id=workspace.id, client_name="codex", scopes=[Scope.MEMORY_READ]
    )
    raw = token.reveal()
    tampered = raw[:-1] + ("a" if raw[-1] != "a" else "b")
    with pytest.raises(AuthenticationError):
        authenticate_pat(session, tampered)


def test_a_pat_hash_on_a_non_pat_connection_does_not_authenticate(
    session: Session, workspace: Workspace
) -> None:
    """auth_mode is part of the lookup: an OAuth row cannot be used as a PAT."""
    token = generate_token()
    session.add(
        Connection(
            workspace_id=workspace.id,
            client_name="oauth-client",
            auth_mode=AuthMode.OAUTH_STATIC,
            scopes=["memory:read"],
            writes_enabled=True,
            token_hash=token.digest(),
        )
    )
    session.flush()
    with pytest.raises(AuthenticationError):
        authenticate_pat(session, token.reveal())


# -- revocation --------------------------------------------------------------


def test_revoked_token_fails_identically_to_an_unknown_one(
    session: Session, workspace: Workspace
) -> None:
    """No oracle: "revoked" and "never existed" must be indistinguishable."""
    connection, token = mint_pat(
        session, workspace_id=workspace.id, client_name="codex", scopes=[Scope.MEMORY_READ]
    )
    assert authenticate_pat(session, token.reveal()).connection_id == connection.id

    assert revoke_connection(session, connection.id) is True

    with pytest.raises(AuthenticationError) as revoked:
        authenticate_pat(session, token.reveal())
    with pytest.raises(AuthenticationError) as unknown:
        authenticate_pat(session, generate_token().reveal())

    assert type(revoked.value) is type(unknown.value)
    assert str(revoked.value) == str(unknown.value) == AUTHENTICATION_FAILED_MESSAGE
    assert revoked.value.code is unknown.value.code is ErrorCode.UNAUTHENTICATED


def test_revocation_is_idempotent(session: Session, workspace: Workspace) -> None:
    connection, _ = mint_pat(
        session, workspace_id=workspace.id, client_name="codex", scopes=[Scope.MEMORY_READ]
    )
    assert revoke_connection(session, connection.id) is True
    first_revoked_at = session.get(Connection, connection.id).revoked_at  # type: ignore[union-attr]
    assert revoke_connection(session, connection.id) is False
    assert session.get(Connection, connection.id).revoked_at == first_revoked_at  # type: ignore[union-attr]


def test_revoking_an_unknown_connection_is_a_no_op(session: Session) -> None:
    assert revoke_connection(session, uuid.uuid4()) is False


def test_revoking_one_connection_leaves_the_others_alone(
    session: Session, workspace: Workspace
) -> None:
    _, keeper = mint_pat(
        session, workspace_id=workspace.id, client_name="keeper", scopes=[Scope.MEMORY_READ]
    )
    doomed_connection, doomed = mint_pat(
        session, workspace_id=workspace.id, client_name="doomed", scopes=[Scope.MEMORY_READ]
    )
    revoke_connection(session, doomed_connection.id)
    assert authenticate_pat(session, keeper.reveal()).client_name == "keeper"
    with pytest.raises(AuthenticationError):
        authenticate_pat(session, doomed.reveal())


# -- writes toggle -----------------------------------------------------------


def test_writes_disabled_blocks_a_granted_write_scope_end_to_end(
    session: Session, workspace: Workspace
) -> None:
    """The badge is off, the grant still says memory:write, the write is refused."""
    _, token = mint_pat(
        session,
        workspace_id=workspace.id,
        client_name="read-only-agent",
        scopes=[Scope.MEMORY_READ, Scope.MEMORY_WRITE],
        writes_enabled=False,
    )
    ctx = authenticate_pat(session, token.reveal())
    assert ctx.has(Scope.MEMORY_WRITE)
    require_scope(ctx, Scope.MEMORY_READ)
    with pytest.raises(ScopeError) as caught:
        require_scope(ctx, Scope.MEMORY_WRITE)
    assert caught.value.code is ErrorCode.UNAUTHORIZED_SCOPE


# -- workspace binding -------------------------------------------------------


def test_a_token_is_bound_to_exactly_one_workspace(
    session: Session, two_workspaces: TwoWorkspaces
) -> None:
    alpha, beta = two_workspaces.alpha, two_workspaces.beta
    _, alpha_token = mint_pat(
        session, workspace_id=alpha.id, client_name="alpha-agent", scopes=[Scope.MEMORY_READ]
    )
    _, beta_token = mint_pat(
        session, workspace_id=beta.id, client_name="beta-agent", scopes=[Scope.MEMORY_READ]
    )

    alpha_ctx = authenticate_pat(session, alpha_token.reveal())
    beta_ctx = authenticate_pat(session, beta_token.reveal())

    assert alpha_ctx.workspace_id == alpha.id
    assert beta_ctx.workspace_id == beta.id
    assert alpha_ctx.workspace_id != beta_ctx.workspace_id
    assert alpha_ctx.connection_id != beta_ctx.connection_id


def test_revoking_in_one_workspace_does_not_touch_the_other(
    session: Session, two_workspaces: TwoWorkspaces
) -> None:
    alpha_connection, alpha_token = mint_pat(
        session,
        workspace_id=two_workspaces.alpha.id,
        client_name="alpha-agent",
        scopes=[Scope.MEMORY_READ],
    )
    _, beta_token = mint_pat(
        session,
        workspace_id=two_workspaces.beta.id,
        client_name="beta-agent",
        scopes=[Scope.MEMORY_READ],
    )
    revoke_connection(session, alpha_connection.id)
    with pytest.raises(AuthenticationError):
        authenticate_pat(session, alpha_token.reveal())
    assert authenticate_pat(session, beta_token.reveal()).workspace_id == two_workspaces.beta.id


def test_minting_into_a_nonexistent_workspace_fails(session: Session) -> None:
    with pytest.raises(NotFoundError):
        mint_pat(
            session,
            workspace_id=uuid.uuid4(),
            client_name="ghost",
            scopes=[Scope.MEMORY_READ],
        )

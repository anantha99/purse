"""Workspace-scoped data access (C1.8).

Every query in Purse goes through a :class:`Repo`, and a ``Repo`` is bound to
exactly one ``workspace_id`` at construction. No method on it takes a
``workspace_id`` argument, so there is no call site where the wrong workspace
can be passed by mistake, and no "just this once" escape hatch that a code
review has to catch.

Isolation is enforced twice, deliberately:

* every statement this module emits carries ``workspace_id = <bound id>``;
* the schema itself refuses cross-workspace links — a memory can only supersede
  a memory in the same workspace, because the foreign key is composite
  (see the C1.3 migration).

The three functions that are *not* workspace-scoped are the ones that create
identity: :func:`create_user`, :func:`create_workspace`, and the lookups a
vault-wide export needs. A workspace-scoped repository cannot create the
workspace it is scoped to.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from purse.db.models import (
    Api,
    AuditLogEntry,
    AuthMode,
    Connection,
    InitiatedBy,
    Memory,
    MemoryCurrent,
    MemoryKind,
    Skill,
    SkillHead,
    User,
    Workspace,
)

__all__ = [
    "NotFoundError",
    "Repo",
    "WorkspaceScopeError",
    "content_hash",
    "create_user",
    "create_workspace",
    "get_workspace",
    "list_user_workspaces",
]


class WorkspaceScopeError(RuntimeError):
    """Raised when an operation would cross a workspace boundary."""


class NotFoundError(LookupError):
    """Raised when a referenced row does not exist in this workspace."""


def content_hash(content: str) -> str:
    """sha256 hex digest of *content* — the content address of a skill version."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Vault-level helpers (not workspace-scoped, by necessity)
# ---------------------------------------------------------------------------


def create_user(session: Session, *, email: str, auth: Mapping[str, Any] | None = None) -> User:
    """Create a user. ``auth`` holds hashes and provider subjects, never secrets."""
    user = User(email=email, auth=dict(auth or {}))
    session.add(user)
    session.flush()
    return user


def create_workspace(session: Session, *, user_id: uuid.UUID, name: str) -> Workspace:
    """Create a workspace owned by *user_id*."""
    workspace = Workspace(user_id=user_id, name=name)
    session.add(workspace)
    session.flush()
    return workspace


def list_user_workspaces(session: Session, user_id: uuid.UUID) -> list[Workspace]:
    """Every workspace in a user's vault, oldest first."""
    stmt = select(Workspace).where(Workspace.user_id == user_id).order_by(Workspace.created_at)
    return list(session.scalars(stmt))


def get_workspace(session: Session, workspace_id: uuid.UUID) -> Workspace | None:
    return session.get(Workspace, workspace_id)


# ---------------------------------------------------------------------------
# The repository
# ---------------------------------------------------------------------------


class Repo:
    """All reads and writes for one workspace."""

    def __init__(self, session: Session, workspace_id: uuid.UUID) -> None:
        self._session = session
        self._workspace_id = workspace_id

    @classmethod
    def open(cls, session: Session, workspace_id: uuid.UUID) -> Repo:
        """Construct a repo, failing loudly if the workspace does not exist."""
        if get_workspace(session, workspace_id) is None:
            raise NotFoundError(f"workspace {workspace_id} does not exist")
        return cls(session, workspace_id)

    @property
    def workspace_id(self) -> uuid.UUID:
        return self._workspace_id

    def flush(self) -> None:
        self._session.flush()

    # -- connections --------------------------------------------------------

    def add_connection(
        self,
        *,
        client_name: str,
        auth_mode: AuthMode,
        scopes: Sequence[str] = (),
        writes_enabled: bool = False,
        token_hash: str | None = None,
    ) -> Connection:
        connection = Connection(
            workspace_id=self._workspace_id,
            client_name=client_name,
            auth_mode=auth_mode,
            scopes=list(scopes),
            writes_enabled=writes_enabled,
            token_hash=token_hash,
        )
        self._session.add(connection)
        self._session.flush()
        return connection

    def get_connection(self, connection_id: uuid.UUID) -> Connection | None:
        stmt = select(Connection).where(
            Connection.id == connection_id,
            Connection.workspace_id == self._workspace_id,
        )
        return self._session.scalars(stmt).one_or_none()

    def list_connections(self) -> list[Connection]:
        stmt = (
            select(Connection)
            .where(Connection.workspace_id == self._workspace_id)
            .order_by(Connection.created_at, Connection.id)
        )
        return list(self._session.scalars(stmt))

    def revoke_connection(self, connection_id: uuid.UUID) -> bool:
        """Mark a connection revoked. Returns False if it is not in this workspace."""
        stmt = (
            update(Connection)
            .where(
                Connection.id == connection_id,
                Connection.workspace_id == self._workspace_id,
                Connection.revoked_at.is_(None),
            )
            .values(revoked_at=func.now())
            .returning(Connection.id)
            .execution_options(synchronize_session=False)
        )
        changed = self._session.execute(stmt).scalar_one_or_none() is not None
        if changed:
            self._session.expire_all()
        return changed

    def _require_connection(self, connection_id: uuid.UUID) -> None:
        if self.get_connection(connection_id) is None:
            raise WorkspaceScopeError(
                f"connection {connection_id} does not belong to workspace {self._workspace_id}"
            )

    # -- memories -----------------------------------------------------------

    def add_memory(
        self,
        *,
        content: str,
        kind: MemoryKind,
        connection_id: uuid.UUID,
        initiated_by: InitiatedBy,
        embedding: Sequence[float] | None = None,
        agent_id: str | None = None,
        supersedes: uuid.UUID | None = None,
    ) -> Memory:
        """Append a memory. This is the only way a memory is ever created."""
        self._require_connection(connection_id)
        if supersedes is not None and self.get_memory(supersedes) is None:
            raise WorkspaceScopeError(
                f"memory {supersedes} does not belong to workspace {self._workspace_id}"
            )
        memory = Memory(
            workspace_id=self._workspace_id,
            content=content,
            kind=kind,
            connection_id=connection_id,
            initiated_by=initiated_by,
            embedding=list(embedding) if embedding is not None else None,
            agent_id=agent_id,
            supersedes=supersedes,
        )
        self._session.add(memory)
        self._session.flush()
        return memory

    def supersede_memory(
        self,
        memory_id: uuid.UUID,
        *,
        content: str,
        connection_id: uuid.UUID,
        initiated_by: InitiatedBy,
        kind: MemoryKind | None = None,
        embedding: Sequence[float] | None = None,
        agent_id: str | None = None,
    ) -> Memory:
        """Replace a memory by appending its successor (PRD §8.2).

        The old row is untouched — it is the history. ``kind`` defaults to the
        superseded memory's kind.
        """
        previous = self.get_memory(memory_id)
        if previous is None:
            raise NotFoundError(f"memory {memory_id} not found in workspace {self._workspace_id}")
        return self.add_memory(
            content=content,
            kind=kind if kind is not None else previous.kind,
            connection_id=connection_id,
            initiated_by=initiated_by,
            embedding=embedding,
            agent_id=agent_id,
            supersedes=previous.id,
        )

    def tombstone_memory(self, memory_id: uuid.UUID) -> bool:
        """Mark a memory deleted. The row survives; only the flag flips.

        Returns False if the memory is not in this workspace or is already
        tombstoned. Un-tombstoning is impossible — the database refuses.
        """
        stmt = (
            update(Memory)
            .where(
                Memory.id == memory_id,
                Memory.workspace_id == self._workspace_id,
                Memory.tombstone.is_(False),
            )
            .values(tombstone=True)
            .returning(Memory.id)
            .execution_options(synchronize_session=False)
        )
        changed = self._session.execute(stmt).scalar_one_or_none() is not None
        if changed:
            self._session.expire_all()
        return changed

    def set_embedding(self, memory_id: uuid.UUID, embedding: Sequence[float]) -> bool:
        """Backfill or rebuild the derived embedding for a memory (C3.6).

        Permitted by the append-only triggers precisely because an embedding is
        derived from ``content`` and carries no information of its own.
        """
        stmt = (
            update(Memory)
            .where(Memory.id == memory_id, Memory.workspace_id == self._workspace_id)
            .values(embedding=list(embedding))
            .returning(Memory.id)
            .execution_options(synchronize_session=False)
        )
        changed = self._session.execute(stmt).scalar_one_or_none() is not None
        if changed:
            self._session.expire_all()
        return changed

    def get_memory(self, memory_id: uuid.UUID) -> Memory | None:
        stmt = select(Memory).where(
            Memory.id == memory_id, Memory.workspace_id == self._workspace_id
        )
        return self._session.scalars(stmt).one_or_none()

    def list_memories(self, *, limit: int | None = None, offset: int = 0) -> list[Memory]:
        """The full canonical log for this workspace — superseded and tombstoned included."""
        stmt = (
            select(Memory)
            .where(Memory.workspace_id == self._workspace_id)
            .order_by(Memory.created_at, Memory.id)
            .offset(offset or None)
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        return list(self._session.scalars(stmt))

    def current_memories(self, *, limit: int | None = None) -> list[MemoryCurrent]:
        """Live memories: not tombstoned, never superseded (C1.4)."""
        stmt = (
            select(MemoryCurrent)
            .where(MemoryCurrent.workspace_id == self._workspace_id)
            .order_by(MemoryCurrent.created_at, MemoryCurrent.id)
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        return list(self._session.scalars(stmt))

    def count_memories(self) -> int:
        stmt = (
            select(func.count())
            .select_from(Memory)
            .where(Memory.workspace_id == self._workspace_id)
        )
        return int(self._session.execute(stmt).scalar_one())

    # -- skills -------------------------------------------------------------

    def put_skill(
        self,
        *,
        name: str,
        version: str,
        content: str,
        frontmatter: Mapping[str, Any] | None = None,
        make_head: bool = True,
    ) -> Skill:
        """Store a skill version and (by default) point the head at it."""
        skill = Skill(
            workspace_id=self._workspace_id,
            name=name,
            version=version,
            content=content,
            content_hash=content_hash(content),
            frontmatter=dict(frontmatter or {}),
        )
        self._session.add(skill)
        self._session.flush()
        if make_head:
            self._set_skill_head(name=name, skill_id=skill.id)
        return skill

    def _set_skill_head(self, *, name: str, skill_id: uuid.UUID) -> None:
        stmt = (
            update(SkillHead)
            .where(SkillHead.workspace_id == self._workspace_id, SkillHead.name == name)
            .values(skill_id=skill_id, updated_at=func.now())
            .returning(SkillHead.skill_id)
            .execution_options(synchronize_session=False)
        )
        if self._session.execute(stmt).scalar_one_or_none() is None:
            self._session.add(
                SkillHead(workspace_id=self._workspace_id, name=name, skill_id=skill_id)
            )
        self._session.flush()
        self._session.expire_all()

    def get_skill(self, name: str, version: str | None = None) -> Skill | None:
        """A specific version, or the head version when *version* is None."""
        if version is not None:
            stmt = select(Skill).where(
                Skill.workspace_id == self._workspace_id,
                Skill.name == name,
                Skill.version == version,
            )
            return self._session.scalars(stmt).one_or_none()
        head_stmt = (
            select(Skill)
            .join(SkillHead, SkillHead.skill_id == Skill.id)
            .where(
                SkillHead.workspace_id == self._workspace_id,
                SkillHead.name == name,
                Skill.workspace_id == self._workspace_id,
            )
        )
        return self._session.scalars(head_stmt).one_or_none()

    def list_skills(self) -> list[Skill]:
        """The head version of every skill in this workspace."""
        stmt = (
            select(Skill)
            .join(SkillHead, SkillHead.skill_id == Skill.id)
            .where(
                SkillHead.workspace_id == self._workspace_id,
                Skill.workspace_id == self._workspace_id,
            )
            .order_by(Skill.name)
        )
        return list(self._session.scalars(stmt))

    def list_skill_versions(self, name: str | None = None) -> list[Skill]:
        """Every stored version — the history an export carries."""
        stmt = select(Skill).where(Skill.workspace_id == self._workspace_id)
        if name is not None:
            stmt = stmt.where(Skill.name == name)
        return list(self._session.scalars(stmt.order_by(Skill.name, Skill.created_at, Skill.id)))

    def list_skill_heads(self) -> list[SkillHead]:
        stmt = (
            select(SkillHead)
            .where(SkillHead.workspace_id == self._workspace_id)
            .order_by(SkillHead.name)
        )
        return list(self._session.scalars(stmt))

    # -- apis ---------------------------------------------------------------

    def add_api(
        self,
        *,
        name: str,
        provider: str,
        base_url: str,
        auth_style: str,
        allowed_hosts: Sequence[str] = (),
        key_ciphertext: bytes | None = None,
        dek_wrapped: bytes | None = None,
    ) -> Api:
        api = Api(
            workspace_id=self._workspace_id,
            name=name,
            provider=provider,
            base_url=base_url,
            auth_style=auth_style,
            allowed_hosts=list(allowed_hosts),
            key_ciphertext=key_ciphertext,
            dek_wrapped=dek_wrapped,
        )
        self._session.add(api)
        self._session.flush()
        return api

    def get_api(self, name: str) -> Api | None:
        stmt = select(Api).where(Api.workspace_id == self._workspace_id, Api.name == name)
        return self._session.scalars(stmt).one_or_none()

    def list_apis(self) -> list[Api]:
        stmt = select(Api).where(Api.workspace_id == self._workspace_id).order_by(Api.name)
        return list(self._session.scalars(stmt))

    def rotate_api_key(self, name: str, *, key_ciphertext: bytes, dek_wrapped: bytes) -> bool:
        stmt = (
            update(Api)
            .where(Api.workspace_id == self._workspace_id, Api.name == name)
            .values(
                key_ciphertext=key_ciphertext,
                dek_wrapped=dek_wrapped,
                rotated_at=func.now(),
            )
            .returning(Api.id)
            .execution_options(synchronize_session=False)
        )
        changed = self._session.execute(stmt).scalar_one_or_none() is not None
        if changed:
            self._session.expire_all()
        return changed

    def delete_api(self, name: str) -> bool:
        """Remove a stored API. Unlike memories, credentials are genuinely deletable."""
        stmt = (
            delete(Api)
            .where(Api.workspace_id == self._workspace_id, Api.name == name)
            .returning(Api.id)
            .execution_options(synchronize_session=False)
        )
        return self._session.execute(stmt).scalar_one_or_none() is not None

    # -- audit --------------------------------------------------------------

    def record_audit(
        self,
        *,
        connection_id: uuid.UUID,
        action: str,
        target_type: str,
        target_id: str,
        agent_id: str | None = None,
    ) -> AuditLogEntry:
        """Append an audit row. Names and IDs only — never values (PRD §13)."""
        self._require_connection(connection_id)
        entry = AuditLogEntry(
            workspace_id=self._workspace_id,
            connection_id=connection_id,
            agent_id=agent_id,
            action=action,
            target_type=target_type,
            target_id=target_id,
        )
        self._session.add(entry)
        self._session.flush()
        return entry

    def list_audit(self, *, limit: int = 100) -> list[AuditLogEntry]:
        """Most recent first (C7.7)."""
        stmt = (
            select(AuditLogEntry)
            .where(AuditLogEntry.workspace_id == self._workspace_id)
            .order_by(AuditLogEntry.created_at.desc(), AuditLogEntry.id.desc())
            .limit(limit)
        )
        return list(self._session.scalars(stmt))

    # -- misc ---------------------------------------------------------------

    def workspace(self) -> Workspace:
        workspace = get_workspace(self._session, self._workspace_id)
        if workspace is None:
            raise NotFoundError(f"workspace {self._workspace_id} does not exist")
        return workspace

    def __repr__(self) -> str:
        return f"Repo(workspace_id={self._workspace_id!r})"

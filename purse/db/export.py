"""Vault export (C1.9) — "exit is a feature" (PRD §8.1).

The export is a public contract, documented in ``docs/export-schema.md``. It is
ungated: no plan check, no rate limit, no "contact us". A user who wants their
data out gets all of it.

What "all of it" means, precisely:

* **memories** — every row of the canonical log, including superseded and
  tombstoned ones, each with its full provenance. The history *is* the data;
  exporting only the current view would be exporting a summary.
* **skills** — every stored version, not just the head.
* **apis** — names, providers, base URLs, auth styles, host allowlists.
  **Never** ``key_ciphertext`` or ``dek_wrapped``. An export travels through
  email attachments and cloud drives; wrapped key material must not.
* **connections** — the provenance IDs that memories point at, so a downstream
  reader can say "this came from Cursor". Never ``token_hash``.

Deliberately excluded, and why:

* ``memories.embedding`` — derived from ``content`` under whatever embedding
  model was configured at write time. It is rebuildable (C3.6), it would
  multiply the export size by an order of magnitude, and it is meaningless to
  anyone using a different model.
* ``audit_log`` — an operational record of the vault, not the user's content.
  A separate, explicitly-requested export if it is ever wanted.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy.orm import Session

from purse.db.models import Api, Connection, Memory, Skill, User
from purse.db.repo import Repo, list_user_workspaces

__all__ = [
    "API_EXPORTED_FIELDS",
    "API_NEVER_EXPORTED_FIELDS",
    "EXPORT_FORMAT",
    "EXPORT_FORMAT_VERSION",
    "export_vault",
    "export_workspace",
]

#: Identifier for the document shape. Present so a reader can tell a Purse
#: export from any other JSON blob before it starts parsing.
EXPORT_FORMAT = "purse.vault.export"

#: Bumped only for breaking changes. Additive fields do not bump it — readers
#: must ignore keys they do not recognise. See docs/export-schema.md.
EXPORT_FORMAT_VERSION = "1.0"

#: Columns of ``apis`` that the export carries.
API_EXPORTED_FIELDS = frozenset(
    {
        "id",
        "workspace_id",
        "name",
        "provider",
        "base_url",
        "auth_style",
        "allowed_hosts",
        "created_at",
        "rotated_at",
    }
)

#: Columns of ``apis`` that the export must never carry. These hold key
#: material (C6.1).
API_NEVER_EXPORTED_FIELDS = frozenset({"key_ciphertext", "dek_wrapped"})

#: Columns of ``connections`` the export must never carry.
CONNECTION_NEVER_EXPORTED_FIELDS = frozenset({"token_hash"})


def _assert_every_column_is_classified() -> None:
    """Fail at import time if a new column dodged the export decision.

    Adding a column to ``apis`` or ``connections`` without saying whether it is
    exportable is exactly how key material leaks into an export three releases
    from now. This makes that a startup error instead.
    """
    api_columns = set(Api.__table__.columns.keys())
    classified = API_EXPORTED_FIELDS | API_NEVER_EXPORTED_FIELDS
    if api_columns != classified:
        raise RuntimeError(
            "purse.db.export is out of date with the apis table: "
            f"unclassified={sorted(api_columns - classified)} "
            f"stale={sorted(classified - api_columns)}. "
            "Add each new column to API_EXPORTED_FIELDS or API_NEVER_EXPORTED_FIELDS."
        )
    connection_columns = set(Connection.__table__.columns.keys())
    if not connection_columns >= CONNECTION_NEVER_EXPORTED_FIELDS:
        raise RuntimeError(
            "purse.db.export names connection columns that no longer exist: "
            f"{sorted(CONNECTION_NEVER_EXPORTED_FIELDS - connection_columns)}"
        )


_assert_every_column_is_classified()


# ---------------------------------------------------------------------------
# Scalar coercion: everything below emits JSON-native values only.
# ---------------------------------------------------------------------------
#
# This is not tidiness. Because no custom JSON encoder is involved, any `bytes`
# that ever reached the payload would make json.dumps raise TypeError rather
# than quietly base64 it into the file. The type system of the serializer is
# the last line of defence for key material.


def _iso(value: dt.datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _id(value: uuid.UUID | None) -> str | None:
    return str(value) if value is not None else None


# ---------------------------------------------------------------------------
# Row serializers
# ---------------------------------------------------------------------------


def _memory_entry(memory: Memory, *, current_ids: set[uuid.UUID]) -> dict[str, Any]:
    return {
        "id": str(memory.id),
        "workspace_id": str(memory.workspace_id),
        "content": memory.content,
        "kind": memory.kind.value,
        "supersedes": _id(memory.supersedes),
        "tombstone": memory.tombstone,
        "created_at": _iso(memory.created_at),
        "is_current": memory.id in current_ids,
        "provenance": {
            "connection_id": str(memory.connection_id),
            "agent_id": memory.agent_id,
            "initiated_by": memory.initiated_by.value,
        },
    }


def _skill_entry(skill: Skill, *, head_ids: set[uuid.UUID]) -> dict[str, Any]:
    return {
        "id": str(skill.id),
        "workspace_id": str(skill.workspace_id),
        "name": skill.name,
        "version": skill.version,
        "frontmatter": skill.frontmatter,
        "content": skill.content,
        "content_hash": skill.content_hash,
        "created_at": _iso(skill.created_at),
        "is_head": skill.id in head_ids,
    }


def _api_entry(api: Api) -> dict[str, Any]:
    """Names and references only — never key material.

    Built field by field from the allowlist rather than by iterating the row,
    so a future column is absent by default instead of present by default.
    """
    return {
        "id": str(api.id),
        "workspace_id": str(api.workspace_id),
        "name": api.name,
        "provider": api.provider,
        "base_url": api.base_url,
        "auth_style": api.auth_style,
        "allowed_hosts": list(api.allowed_hosts),
        "created_at": _iso(api.created_at),
        "rotated_at": _iso(api.rotated_at),
        "key_exported": False,
    }


def _connection_entry(connection: Connection) -> dict[str, Any]:
    return {
        "id": str(connection.id),
        "workspace_id": str(connection.workspace_id),
        "client_name": connection.client_name,
        "auth_mode": connection.auth_mode.value,
        "scopes": list(connection.scopes),
        "writes_enabled": connection.writes_enabled,
        "created_at": _iso(connection.created_at),
        "revoked_at": _iso(connection.revoked_at),
    }


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def export_workspace(repo: Repo) -> dict[str, Any]:
    """Export one workspace through its repository.

    Taking a :class:`Repo` rather than a session and a workspace id is the
    point: the export reads through the same workspace-scoped path as
    everything else, so it cannot accidentally pull another workspace's rows.
    """
    workspace = repo.workspace()
    memories = repo.list_memories()
    current_ids = {row.id for row in repo.current_memories()}
    skill_versions = repo.list_skill_versions()
    head_ids = {head.skill_id for head in repo.list_skill_heads()}

    return {
        "id": str(workspace.id),
        "name": workspace.name,
        "created_at": _iso(workspace.created_at),
        "connections": [_connection_entry(c) for c in repo.list_connections()],
        "memories": [_memory_entry(m, current_ids=current_ids) for m in memories],
        "skills": [_skill_entry(s, head_ids=head_ids) for s in skill_versions],
        "apis": [_api_entry(a) for a in repo.list_apis()],
    }


def export_vault(session: Session, user_id: uuid.UUID) -> dict[str, Any]:
    """Export a whole vault: the user, and every workspace they own.

    The returned structure contains only JSON-native values, so
    ``json.dumps(export_vault(...))`` works with no custom encoder.
    """
    user = session.get(User, user_id)
    if user is None:
        raise LookupError(f"user {user_id} does not exist")

    workspaces = list_user_workspaces(session, user_id)
    return {
        "format": EXPORT_FORMAT,
        "format_version": EXPORT_FORMAT_VERSION,
        "exported_at": dt.datetime.now(dt.UTC).isoformat(),
        "user": {
            "id": str(user.id),
            "email": user.email,
            "created_at": _iso(user.created_at),
        },
        "workspaces": [export_workspace(Repo(session, ws.id)) for ws in workspaces],
    }

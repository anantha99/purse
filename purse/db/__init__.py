"""Database (C1): schema, migrations, and workspace-scoped queries over Postgres + pgvector.

The public surface of this package is deliberately narrow:

``Repo``
    The only way application code reads or writes vault data. Bound to one
    workspace at construction (C1.8).
``create_user`` / ``create_workspace``
    Identity bootstrap — the only writers that are not workspace-scoped,
    because they create the scope.
``export_vault``
    The documented, ungated JSON export (C1.9, ``docs/export-schema.md``).
``upgrade``
    Apply migrations.

Models live in :mod:`purse.db.models`; import them from there when you need the
table definitions themselves.
"""

from purse.db.config import DatabaseUrlError, database_url, normalize_database_url
from purse.db.export import EXPORT_FORMAT, EXPORT_FORMAT_VERSION, export_vault, export_workspace
from purse.db.migrate import upgrade
from purse.db.models import EMBEDDING_DIM, AuthMode, InitiatedBy, MemoryKind, OAuthClientKind
from purse.db.repo import (
    NotFoundError,
    Repo,
    WorkspaceScopeError,
    create_user,
    create_workspace,
    list_user_workspaces,
)
from purse.db.session import create_db_engine, session_scope

__all__ = [
    "EMBEDDING_DIM",
    "EXPORT_FORMAT",
    "EXPORT_FORMAT_VERSION",
    "AuthMode",
    "DatabaseUrlError",
    "InitiatedBy",
    "MemoryKind",
    "NotFoundError",
    "OAuthClientKind",
    "Repo",
    "WorkspaceScopeError",
    "create_db_engine",
    "create_user",
    "create_workspace",
    "database_url",
    "export_vault",
    "export_workspace",
    "list_user_workspaces",
    "normalize_database_url",
    "session_scope",
    "upgrade",
]

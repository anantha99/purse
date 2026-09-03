"""The ``/web`` operator dashboard surface (C7, ``docs/web-api-contract.md``).

A session-authenticated FastAPI app — mounted at ``/web`` on the one Purse ASGI
app (:mod:`purse.gateway.asgi`) alongside ``/mcp``, ``/v1``, and the OAuth AS.
Every endpoint except ``/web/login`` and ``/web/logout`` requires a valid session
token and resolves to the operator's workspace; all reads and writes go through
the existing services and the workspace-scoped :class:`Repo`, so the isolation
boundary is the same one every other surface uses.

This module deliberately reimplements no business logic: memory writes go through
:mod:`purse.memory.service`, skills through :mod:`purse.skills.service`, PAT
minting and revocation through :mod:`purse.auth.provisioning` and ``Repo``, and
the export through :mod:`purse.db.export`. What lives here is the session gate,
the display-shape projections (:mod:`purse.web.memories`), and the one error
envelope — mirroring :mod:`purse.gateway.rest`.
"""

# NOTE: no `from __future__ import annotations` here, and it is load-bearing —
# exactly as in purse.gateway.rest. FastAPI resolves the `Annotated[..., Depends]`
# markers on the nested endpoints against this module's globals; PEP 563 would
# turn the closure-local dependency references into unresolvable strings and every
# request would fail at import. Everything annotated here is valid at runtime on
# 3.12, so nothing is lost by evaluating annotations eagerly.

import json
import uuid
from collections.abc import Callable, Iterator
from typing import Annotated, Any, cast

from fastapi import Depends, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from purse.auth.provisioning import ProvisioningError, mint_pat
from purse.auth.scopes import UnknownScopeError
from purse.db.export import export_vault
from purse.db.repo import NotFoundError as DbNotFoundError
from purse.db.repo import Repo
from purse.memory import service as memory_service
from purse.memory.engine import MemoryEngine
from purse.memory.errors import MemoryError_
from purse.memory.records import MemoryRecord
from purse.skills import service as skills_service
from purse.skills.errors import SkillError
from purse.web import memories as web_memories
from purse.web.errors import NotFoundError, UnauthenticatedError, WebError
from purse.web.session import (
    OperatorProvenance,
    SessionContext,
    SessionManager,
    operator_connection_id,
    resolve_operator,
)

__all__ = ["create_web_app"]

_STATUS_BY_CODE = {
    "NOT_FOUND": 404,
    "PAYLOAD_TOO_LARGE": 413,
    "VALIDATION": 422,
}


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------


class _StrictModel(BaseModel):
    # An unknown field is a client bug; forbidding extras turns a typo into a
    # 422 rather than a silently ignored value.
    model_config = ConfigDict(extra="forbid")


class LoginRequest(_StrictModel):
    password: str


class AddMemoryRequest(_StrictModel):
    content: str
    kind: str = "fact"
    # The operator is a human acting directly, so a UI add defaults to `user`.
    initiated_by: str = "user"


class UpdateMemoryRequest(_StrictModel):
    content: str


class UpsertSkillRequest(_StrictModel):
    content: str


class MintTokenRequest(_StrictModel):
    client_name: str
    scopes: list[str] = []
    writes_enabled: bool = False


# ---------------------------------------------------------------------------
# Error envelope
# ---------------------------------------------------------------------------


def _error_response(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message}})


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_web_app(
    session_factory: Callable[[], Session],
    engine: MemoryEngine,
    *,
    sessions: SessionManager,
    title: str = "Purse Web",
) -> FastAPI:
    """Build the ``/web`` dashboard app.

    :param session_factory: Called once per request; the session is committed on
        success and rolled back on any exception (so a failed audit write can
        never orphan a memory row).
    :param engine: The shared memory engine, passed to the memory service for
        ingest/search exactly as the REST and MCP surfaces do.
    :param sessions: The configured :class:`SessionManager` (password + signing
        secret). Login is disabled cleanly when either is unset.
    """
    app = FastAPI(title=title, version="0.0.1")

    # -- request plumbing ---------------------------------------------------

    def db_session() -> Iterator[Session]:
        session = session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def require_session(request: Request) -> SessionContext:
        """Verify the ``Authorization: Bearer <session-token>`` header."""
        return sessions.verify_token(_bearer_token(request))

    Db = Annotated[Session, Depends(db_session)]
    Ctx = Annotated[SessionContext, Depends(require_session)]

    # -- error handling -----------------------------------------------------

    async def on_web_error(request: Request, exc: Exception) -> JSONResponse:
        error = cast(WebError, exc)
        response = _error_response(error.status, error.code, error.message)
        if error.status == 401:
            response.headers["WWW-Authenticate"] = "Bearer"
        return response

    async def on_memory_error(request: Request, exc: Exception) -> JSONResponse:
        error = cast(MemoryError_, exc)
        return _error_response(_STATUS_BY_CODE.get(error.code, 400), error.code, error.message)

    async def on_skill_error(request: Request, exc: Exception) -> JSONResponse:
        error = cast(SkillError, exc)
        return _error_response(_STATUS_BY_CODE.get(error.code, 400), error.code, error.message)

    async def on_provisioning_error(request: Request, exc: Exception) -> JSONResponse:
        # An empty client_name or an unknown scope is a bad request, not a fault.
        return _error_response(422, "VALIDATION", str(exc))

    async def on_db_not_found(request: Request, exc: Exception) -> JSONResponse:
        return _error_response(404, "NOT_FOUND", str(exc))

    async def on_request_validation_error(request: Request, exc: Exception) -> JSONResponse:
        error = cast(RequestValidationError, exc)
        detail = "; ".join(
            f"{'.'.join(str(part) for part in item['loc'][1:])}: {item['msg']}"
            for item in error.errors()
        )
        return _error_response(422, "VALIDATION", detail or "invalid request")

    app.add_exception_handler(WebError, on_web_error)
    app.add_exception_handler(MemoryError_, on_memory_error)
    app.add_exception_handler(SkillError, on_skill_error)
    app.add_exception_handler(ProvisioningError, on_provisioning_error)
    app.add_exception_handler(UnknownScopeError, on_provisioning_error)
    app.add_exception_handler(DbNotFoundError, on_db_not_found)
    app.add_exception_handler(RequestValidationError, on_request_validation_error)

    # -- session ------------------------------------------------------------

    @app.post("/login")
    def login(body: LoginRequest, session: Db) -> dict[str, Any]:
        sessions.verify_password(body.password)
        operator = resolve_operator(session)
        token = sessions.issue_token(user_id=operator.user_id, workspace_id=operator.workspace_id)
        return {
            "user": {"email": operator.email},
            "workspace": {"id": str(operator.workspace_id), "name": operator.workspace_name},
            "session_token": token,
        }

    @app.post("/logout", status_code=204)
    def logout() -> Response:
        # Tokens are stateless; the BFF clears its cookie. Nothing to do server-side.
        return Response(status_code=204)

    @app.get("/session")
    def session_info(ctx: Ctx, session: Db) -> dict[str, Any]:
        workspace = _require_workspace(session, ctx)
        user = _require_user(session, ctx)
        return {
            "user": {"email": user_email(user)},
            "workspace": {"id": str(workspace.id), "name": workspace.name},
            "writes_enabled_default": False,
        }

    # -- memories -----------------------------------------------------------

    @app.get("/memories")
    def list_memories(
        ctx: Ctx,
        session: Db,
        cursor: Annotated[str | None, Query()] = None,
        limit: Annotated[int | None, Query()] = None,
        kind: Annotated[str | None, Query()] = None,
        initiated_by: Annotated[str | None, Query()] = None,
    ) -> dict[str, Any]:
        repo = Repo.open(session, ctx.workspace_id)
        page = web_memories.list_current(
            session,
            ctx.workspace_id,
            cursor=cursor,
            limit=limit,
            kind=kind,
            initiated_by=initiated_by,
        )
        names = _client_names(repo)
        items = [_memory_item(MemoryRecord.from_model(row), repo, names) for row in page.rows]
        return {"items": items, "next_cursor": page.next_cursor}

    @app.get("/memories/search")
    def search_memories(
        ctx: Ctx,
        session: Db,
        q: Annotated[str, Query()] = "",
        limit: Annotated[int, Query()] = memory_service.DEFAULT_SEARCH_LIMIT,
    ) -> dict[str, Any]:
        repo = Repo.open(session, ctx.workspace_id)
        prov = _read_context(ctx)
        hits = memory_service.search_memory(session, prov, engine, query=q, limit=limit)
        names = _client_names(repo)
        results = []
        for hit in hits:
            row = repo.get_memory(hit.id)
            supersedes = row.supersedes if row is not None else None
            item = web_memories.item_dict(
                MemoryRecord(
                    id=hit.id,
                    content=hit.content,
                    kind=hit.kind,
                    created_at=hit.created_at,
                    provenance=hit.provenance,
                    supersedes=supersedes,
                ),
                client_name=names.get(hit.provenance.connection_id),
                superseded=web_memories.superseded_count(repo, supersedes),
            )
            item["score"] = hit.score
            results.append(item)
        return {"results": results}

    @app.get("/memories/{memory_id}/history")
    def memory_history(memory_id: uuid.UUID, ctx: Ctx, session: Db) -> dict[str, Any]:
        repo = Repo.open(session, ctx.workspace_id)
        chain = web_memories.history_chain(repo, memory_id)
        if chain is None:
            raise NotFoundError(f"memory {memory_id} not found in this workspace")
        names = _client_names(repo)
        versions = [
            web_memories.history_entry(row, client_name=names.get(row.connection_id))
            for row in chain
        ]
        return {"versions": versions}

    @app.post("/memories", status_code=201)
    def add_memory(body: AddMemoryRequest, ctx: Ctx, session: Db) -> dict[str, Any]:
        repo = Repo.open(session, ctx.workspace_id)
        prov = _write_context(repo, ctx)
        record = memory_service.add_memory(
            session,
            prov,
            engine,
            content=body.content,
            kind=body.kind,
            initiated_by=body.initiated_by,
        )
        return _memory_item(record, repo, _client_names(repo))

    @app.patch("/memories/{memory_id}")
    def update_memory(
        memory_id: uuid.UUID, body: UpdateMemoryRequest, ctx: Ctx, session: Db
    ) -> dict[str, Any]:
        repo = Repo.open(session, ctx.workspace_id)
        prov = _write_context(repo, ctx)
        record = memory_service.update_memory(
            session, prov, engine, memory_id=memory_id, content=body.content
        )
        return _memory_item(record, repo, _client_names(repo))

    @app.delete("/memories/{memory_id}")
    def delete_memory(memory_id: uuid.UUID, ctx: Ctx, session: Db) -> dict[str, Any]:
        repo = Repo.open(session, ctx.workspace_id)
        prov = _write_context(repo, ctx)
        memory_service.delete_memory(session, prov, memory_id=memory_id, engine=engine)
        return {"id": str(memory_id), "deleted": True}

    # -- skills -------------------------------------------------------------

    @app.get("/skills")
    def list_skills(ctx: Ctx, session: Db) -> dict[str, Any]:
        summaries = skills_service.list_skills(session, _read_context(ctx))
        return {"skills": [summary.as_dict() for summary in summaries]}

    @app.get("/skills/{name}")
    def get_skill(
        name: str, ctx: Ctx, session: Db, version: Annotated[str | None, Query()] = None
    ) -> dict[str, Any]:
        prov = _read_context(ctx)
        record = skills_service.get_skill(session, prov, name=name, version=version)
        repo = Repo.open(session, ctx.workspace_id)
        result = record.as_dict()
        result["versions"] = [
            {
                "version": skill.version,
                "content_hash": skill.content_hash,
                "created_at": skill.created_at.isoformat(),
            }
            for skill in repo.list_skill_versions(name)
        ]
        return result

    @app.put("/skills/{name}")
    def upsert_skill(name: str, body: UpsertSkillRequest, ctx: Ctx, session: Db) -> dict[str, Any]:
        repo = Repo.open(session, ctx.workspace_id)
        prov = _write_context(repo, ctx)
        record = skills_service.upsert_skill(session, prov, name=name, content=body.content)
        return {"name": record.name, "version": record.version}

    # -- connections --------------------------------------------------------

    @app.get("/connections")
    def list_connections(ctx: Ctx, session: Db) -> dict[str, Any]:
        repo = Repo.open(session, ctx.workspace_id)
        return {"connections": [_connection_dict(c) for c in repo.list_connections()]}

    @app.delete("/connections/{connection_id}")
    def revoke_connection(connection_id: uuid.UUID, ctx: Ctx, session: Db) -> dict[str, Any]:
        repo = Repo.open(session, ctx.workspace_id)
        if repo.get_connection(connection_id) is None:
            raise NotFoundError(f"connection {connection_id} not found in this workspace")
        repo.revoke_connection(connection_id)  # idempotent: no-op if already revoked
        return {"id": str(connection_id), "revoked": True}

    # -- tokens -------------------------------------------------------------

    @app.post("/tokens", status_code=201)
    def create_token(body: MintTokenRequest, ctx: Ctx, session: Db) -> dict[str, Any]:
        connection, token = mint_pat(
            session,
            workspace_id=ctx.workspace_id,
            client_name=body.client_name,
            scopes=body.scopes,
            writes_enabled=body.writes_enabled,
        )
        return {
            "connection": {
                "id": str(connection.id),
                "client_name": connection.client_name,
                "scopes": list(connection.scopes),
                "writes_enabled": connection.writes_enabled,
            },
            "token": token.reveal(),
        }

    # -- audit --------------------------------------------------------------

    @app.get("/audit")
    def list_audit(ctx: Ctx, session: Db, limit: Annotated[int, Query()] = 100) -> dict[str, Any]:
        repo = Repo.open(session, ctx.workspace_id)
        names = _client_names(repo)
        entries = [
            {
                "action": entry.action,
                "target_type": entry.target_type,
                "target_id": entry.target_id,
                "client_name": names.get(entry.connection_id),
                "agent_id": entry.agent_id,
                "created_at": entry.created_at.isoformat(),
            }
            for entry in repo.list_audit(limit=limit)
        ]
        return {"entries": entries}

    # -- export -------------------------------------------------------------

    @app.get("/export")
    def export(ctx: Ctx, session: Db) -> Response:
        payload = export_vault(session, ctx.user_id)
        body = json.dumps(payload, indent=2)
        return Response(
            content=body,
            media_type="application/json",
            headers={"Content-Disposition": 'attachment; filename="purse-vault-export.json"'},
        )

    # -- workspace ----------------------------------------------------------

    @app.get("/workspace")
    def workspace_counts(ctx: Ctx, session: Db) -> dict[str, Any]:
        repo = Repo.open(session, ctx.workspace_id)
        active = [c for c in repo.list_connections() if c.revoked_at is None]
        return {
            "memories": len(repo.current_memories()),
            "skills": len(repo.list_skills()),
            "apis": len(repo.list_apis()),
            "connections": len(active),
        }

    return app


# ---------------------------------------------------------------------------
# Helpers used by the endpoints above
# ---------------------------------------------------------------------------


def _bearer_token(request: Request) -> str:
    header = request.headers.get("Authorization")
    if not header:
        raise UnauthenticatedError("missing Authorization header")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise UnauthenticatedError("Authorization header must be 'Bearer <token>'")
    return token.strip()


def _read_context(ctx: SessionContext) -> OperatorProvenance:
    """A provenance object for reads: only ``workspace_id`` is used by the services.

    The ``connection_id`` here is a placeholder the read paths never touch (they
    open the workspace repo and query the view); a write path must use
    :func:`_write_context`, which resolves the real operator connection.
    """
    return OperatorProvenance(connection_id=ctx.workspace_id, workspace_id=ctx.workspace_id)


def _write_context(repo: Repo, ctx: SessionContext) -> OperatorProvenance:
    return OperatorProvenance(
        connection_id=operator_connection_id(repo),
        workspace_id=ctx.workspace_id,
    )


def _client_names(repo: Repo) -> dict[uuid.UUID, str]:
    return {connection.id: connection.client_name for connection in repo.list_connections()}


def _memory_item(record: MemoryRecord, repo: Repo, names: dict[uuid.UUID, str]) -> dict[str, Any]:
    return web_memories.item_dict(
        record,
        client_name=names.get(record.provenance.connection_id),
        superseded=web_memories.superseded_count(repo, record.supersedes),
    )


def _connection_dict(connection: Any) -> dict[str, Any]:
    return {
        "id": str(connection.id),
        "client_name": connection.client_name,
        "auth_mode": connection.auth_mode.value,
        "scopes": list(connection.scopes),
        "writes_enabled": connection.writes_enabled,
        "created_at": connection.created_at.isoformat(),
        "revoked_at": connection.revoked_at.isoformat() if connection.revoked_at else None,
    }


def _require_workspace(session: Session, ctx: SessionContext) -> Any:
    workspace = Repo.open(session, ctx.workspace_id).workspace()
    return workspace


def _require_user(session: Session, ctx: SessionContext) -> Any:
    from purse.db.models import User

    user = session.get(User, ctx.user_id)
    if user is None:
        raise NotFoundError("operator not found")
    return user


def user_email(user: Any) -> str:
    return str(user.email)

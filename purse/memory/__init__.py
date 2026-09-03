"""Memory (C3): the canonical append-only store, the MemoryEngine interface, and the Mem0 adapter.

The split PRD §8.2 insists on, in module form:

:mod:`purse.memory.service`
    The canonical path. Every write goes through here and lands in Postgres
    synchronously; every read is answered from Postgres. This is the source of
    truth and what an export contains.
:mod:`purse.memory.engine`
    The derived index behind one swappable interface. :class:`NullEngine` is the
    M1 default; the Mem0 adapter lands in C3.4. **An engine failure never fails a
    canonical write.**
:mod:`purse.memory.context`
    The structural contract for "who is calling" — satisfied by ``purse.auth``'s
    real context without either package importing the other.
:mod:`purse.memory.errors`
    Stable error codes, shared by the REST gateway now and the MCP tools in C4.2.
"""

from purse.memory.context import WriteContext
from purse.memory.engine import EngineHit, MemoryEngine, NullEngine
from purse.memory.errors import MemoryError_ as MemoryServiceError
from purse.memory.errors import NotFoundError, PayloadTooLargeError, ValidationError
from purse.memory.mem0_engine import (
    EmbeddingConfig,
    Mem0Engine,
    build_memory_engine_from_env,
)
from purse.memory.records import MemoryRecord, Provenance, SearchHit
from purse.memory.service import (
    DEFAULT_LIST_LIMIT,
    DEFAULT_SEARCH_LIMIT,
    MAX_CONTENT_BYTES,
    MAX_LIMIT,
    MemoryPage,
    add_memory,
    delete_memory,
    list_memories,
    search_memory,
    update_memory,
)

__all__ = [
    "DEFAULT_LIST_LIMIT",
    "DEFAULT_SEARCH_LIMIT",
    "MAX_CONTENT_BYTES",
    "MAX_LIMIT",
    "EmbeddingConfig",
    "EngineHit",
    "Mem0Engine",
    "MemoryEngine",
    "MemoryPage",
    "MemoryRecord",
    "MemoryServiceError",
    "NotFoundError",
    "NullEngine",
    "PayloadTooLargeError",
    "Provenance",
    "SearchHit",
    "ValidationError",
    "WriteContext",
    "add_memory",
    "build_memory_engine_from_env",
    "delete_memory",
    "list_memories",
    "search_memory",
    "update_memory",
]

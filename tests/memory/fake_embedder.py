"""A deterministic, network-free embedder for the Mem0 adapter's db tests.

The adapter's db-marked tests need a *real* pgvector store (that is the point —
they prove ranking, isolation, supersession and rebuild against the actual SQL),
but they must not need an embedding provider or a network. This module supplies a
fake embedder that Mem0 will use in place of OpenAI.

.. rubric:: The extension point

Mem0 builds the embedder through ``EmbedderFactory.create(provider, config, ...)``,
which looks ``provider`` up in the class-level dict
``EmbedderFactory.provider_to_class`` and ``load_class`` es it. Registering a
*new* provider name there is not enough: ``mem0ai==2.0.19`` validates the provider
against a **hardcoded pydantic allowlist** (``mem0/embeddings/configs.py``:
``openai``, ``ollama``, … ``fastembed``) inside ``MemoryConfig(**dict)``, which runs
*before* the factory is ever consulted — an unknown name raises
``ValidationError`` at ``Memory.from_config``. So the injection instead **rebinds
an allowlisted provider** (``openai``) to the fake class for the duration of a test
and constructs the engine with ``provider="openai"``. Validation passes (the name
is on the allowlist), and the factory loads the fake class. Production never calls
:func:`register_fake_embedder`, so its real ``openai`` embedder is untouched.

.. rubric:: The embedding

Bag-of-words hashing: each lowercased alphanumeric token is hashed (stably, via
BLAKE2b — Python's ``hash`` is per-process salted and would not be reproducible)
into one of ``embedding_dims`` buckets and the bucket incremented; the vector is
then L2-normalised. Cosine similarity between two such vectors therefore rises
with shared vocabulary, which is all the tests need: a query that shares words
with the relevant memory ranks it above an unrelated one, deterministically and
identically on every machine.
"""

from __future__ import annotations

import hashlib
import math
import re
from typing import Literal

from mem0.configs.embeddings.base import BaseEmbedderConfig
from mem0.embeddings.base import EmbeddingBase
from mem0.utils.factory import EmbedderFactory

__all__ = [
    "FAKE_PROVIDER",
    "DeterministicFakeEmbedder",
    "register_fake_embedder",
    "unregister_fake_embedder",
]

#: An allowlisted Mem0 provider name (so ``MemoryConfig`` validation passes); tests
#: rebind it to the fake class via :func:`register_fake_embedder`.
FAKE_PROVIDER = "openai"

#: The real class path we displace, restored by :func:`unregister_fake_embedder`.
_ORIGINAL: str | None = None
_PATCHED = False

_TOKEN = re.compile(r"[a-z0-9]+")


def _bucket(token: str, dims: int) -> int:
    digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big") % dims


class DeterministicFakeEmbedder(EmbeddingBase):
    """A reproducible bag-of-words embedder. No network, no key, no state."""

    def __init__(self, config: BaseEmbedderConfig | None = None) -> None:
        super().__init__(config)
        self._dims = self.config.embedding_dims or 1536

    def embed(
        self,
        text: str,
        memory_action: Literal["add", "search", "update"] | None = None,
    ) -> list[float]:
        vector = [0.0] * self._dims
        for token in _TOKEN.findall((text or "").lower()):
            vector[_bucket(token, self._dims)] += 1.0
        norm = math.sqrt(sum(component * component for component in vector))
        if norm == 0.0:
            # An all-non-alphanumeric string: hand back a fixed unit vector rather
            # than zeros, so cosine distance is defined and the row is insertable.
            vector[0] = 1.0
            return vector
        return [component / norm for component in vector]


def register_fake_embedder() -> None:
    """Rebind the ``openai`` embedder to the fake class. Idempotent."""
    global _ORIGINAL, _PATCHED
    if _PATCHED:
        return
    _ORIGINAL = EmbedderFactory.provider_to_class.get(FAKE_PROVIDER)
    EmbedderFactory.provider_to_class[FAKE_PROVIDER] = (
        "tests.memory.fake_embedder.DeterministicFakeEmbedder"
    )
    _PATCHED = True


def unregister_fake_embedder() -> None:
    """Restore the real ``openai`` embedder class. Idempotent."""
    global _ORIGINAL, _PATCHED
    if not _PATCHED:
        return
    if _ORIGINAL is None:
        EmbedderFactory.provider_to_class.pop(FAKE_PROVIDER, None)
    else:
        EmbedderFactory.provider_to_class[FAKE_PROVIDER] = _ORIGINAL
    _ORIGINAL = None
    _PATCHED = False

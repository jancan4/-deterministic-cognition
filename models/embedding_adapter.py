"""
Embedding adapter interface and deterministic stub.

EmbeddingAdapter is the ABC for all embedding adapters. It is separate from
LocalModelAdapter (text generation) because embedding is a different capability:
    text -> List[float]

Replay contract
---------------
Historical embedding artifacts are replayed from recorded event_embeddings rows.
Regeneration is explicit and provenance-preserving but may produce different float
values depending on model, runtime, or hardware. The canonical truth is the
recorded artifact plus its provenance metadata, not regenerated vector identity.

Adapter contract
----------------
  - embed() must return exactly self.dimensions floats
  - embed() must not write to any database
  - embed() must not make network calls
  - embed() must not mutate the input

Continuity
----------
event_embeddings rows are local derived artifacts. They are excluded from
continuity bundles by governance policy. Future portability can be considered
explicitly, not silently.
"""
import hashlib
import struct
from abc import ABC, abstractmethod
from typing import List, Optional


class EmbeddingAdapter(ABC):
    """Abstract base for all embedding adapters."""

    @property
    @abstractmethod
    def adapter_name(self) -> str:
        """Stable adapter identifier."""

    @property
    @abstractmethod
    def adapter_version(self) -> str:
        """Semver adapter version."""

    @property
    @abstractmethod
    def model_name(self) -> str:
        """Embedding model name."""

    @property
    @abstractmethod
    def model_version(self) -> str:
        """Embedding model version string."""

    @property
    @abstractmethod
    def model_digest(self) -> Optional[str]:
        """Content-addressable model hash, or None if unavailable."""

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Provider identifier (e.g. 'ollama', 'stub')."""

    @property
    @abstractmethod
    def dimensions(self) -> int:
        """Output vector length."""

    @abstractmethod
    def embed(self, text: str) -> List[float]:
        """
        Produce a dense float vector for the given text.

        Must return exactly self.dimensions floats.
        Must not write to any database or make network calls.
        """

    @property
    def producer_version(self) -> str:
        """
        Composite version key: adapter_version:model_version.

        Changes whenever adapter code or model weights change. A new
        producer_version triggers the supersede path, not invalidation.
        Subclasses with a real model digest should override to include it.
        """
        return f"{self.adapter_version}:{self.model_version}"

    def get_provenance(self) -> dict:
        """Return all provenance fields for an event_embeddings row."""
        return {
            'adapter_name': self.adapter_name,
            'adapter_version': self.adapter_version,
            'model_name': self.model_name,
            'model_version': self.model_version,
            'model_digest': self.model_digest,
            'provider_name': self.provider_name,
            'dimensions': self.dimensions,
            'producer_version': self.producer_version,
        }


class StubEmbeddingAdapter(EmbeddingAdapter):
    """
    Deterministic test stub for EmbeddingAdapter.

    Generates a fixed-length vector derived from the SHA-256 hash of the input.
    Identical input always produces the same vector. No inference is performed.

    model_digest is None. producer_version uses an explicit ':stub-no-model-digest'
    suffix rather than a simulated hash, to prevent confusion with real model digests.

    Configurable dimensions allow tests to exercise dimension validation.
    """

    NAME = 'stub_embedding'
    VERSION = '1.0.0'
    MODEL_NAME = 'stub-model'
    MODEL_VERSION = '1.0.0'
    PROVIDER = 'stub'

    def __init__(self, dimensions: int = 4):
        self._dimensions = dimensions

    @property
    def adapter_name(self) -> str:
        return self.NAME

    @property
    def adapter_version(self) -> str:
        return self.VERSION

    @property
    def model_name(self) -> str:
        return self.MODEL_NAME

    @property
    def model_version(self) -> str:
        return self.MODEL_VERSION

    @property
    def model_digest(self) -> Optional[str]:
        return None

    @property
    def provider_name(self) -> str:
        return self.PROVIDER

    @property
    def dimensions(self) -> int:
        return self._dimensions

    @property
    def producer_version(self) -> str:
        # Explicit ':stub-no-model-digest' suffix signals this is a test stub,
        # not a real model content hash.
        return f"{self.VERSION}:{self.MODEL_VERSION}:stub-no-model-digest"

    def embed(self, text: str) -> List[float]:
        """
        Derive a deterministic float vector from the SHA-256 hash of the input.

        Each float is derived from 2 bytes of the 32-byte hash (cyclic). Values
        are in [0.0, 1.0). Same input always produces the same vector.
        """
        raw = hashlib.sha256(text.encode('utf-8')).digest()  # 32 bytes
        floats: List[float] = []
        for i in range(self._dimensions):
            byte_idx = (i * 2) % len(raw)
            value = struct.unpack_from('>H', raw, byte_idx)[0] / 65535.0
            floats.append(value)
        return floats

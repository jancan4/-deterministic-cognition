"""
Ollama embedding adapter for the governed embedding substrate.

OllamaEmbeddingAdapter wraps Ollama's /api/embeddings endpoint as an
EmbeddingAdapter. It is SEPARATE from OllamaAdapter (text generation):
the embedding endpoint does not share semantics or configuration with
/api/generate and must not be conflated with it.

Import safety
-------------
This module is safe to import without 'requests' installed. The import is
deferred to the first embed() or _load_digest() call, following the same
pattern as ollama_adapter.py.

Model digest
------------
model_digest is loaded lazily via /api/show on the first embed() call.
It is included in producer_version (adapter_version:digest[:12]) so that
embedding rows carry content-addressable model provenance.

Replay contract
---------------
Ollama embeddings are NOT bitwise deterministic across model versions or
hardware. Canonical truth is the recorded artifact row, not a re-embedding.
Float non-determinism across regeneration attempts is not a replay failure.

TODO (Phase 3A): Unify embedding generation with workflow lineage before
embedding-aware retrieval becomes canonical. All embed_event() calls in
production workflows should record workflow_id and step provenance.
"""
from typing import List, Optional

from memory.artifact_governance import EMBEDDING_VISIBLE_FIELDS_VERSION
from models.embedding_adapter import EmbeddingAdapter

VERSION = '1.0.0'

OLLAMA_EMBEDDING_ADAPTER_NAME = 'ollama-embedding'
OLLAMA_EMBEDDING_DEFAULT_BASE_URL = 'http://localhost:11434'


class OllamaEmbeddingAdapter(EmbeddingAdapter):
    """
    EmbeddingAdapter backed by Ollama's /api/embeddings endpoint.

    Parameters
    ----------
    model_name : str
        Ollama model identifier (e.g. 'nomic-embed-text').
    expected_dimensions : int
        Expected output vector length. Validated after each embed() call.
    base_url : str
        Ollama server base URL (default: http://localhost:11434).
    """

    def __init__(
        self,
        model_name: str,
        *,
        expected_dimensions: int,
        base_url: str = OLLAMA_EMBEDDING_DEFAULT_BASE_URL,
    ) -> None:
        self._model_name = model_name
        self._expected_dimensions = expected_dimensions
        self._base_url = base_url.rstrip('/')
        self._model_digest: Optional[str] = None
        self._digest_loaded: bool = False

    # --- EmbeddingAdapter interface ---

    @property
    def adapter_name(self) -> str:
        return OLLAMA_EMBEDDING_ADAPTER_NAME

    @property
    def adapter_version(self) -> str:
        return VERSION

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def model_version(self) -> str:
        return self._model_name

    @property
    def model_digest(self) -> Optional[str]:
        return self._model_digest

    @property
    def provider_name(self) -> str:
        return 'ollama'

    @property
    def dimensions(self) -> int:
        return self._expected_dimensions

    @property
    def producer_version(self) -> str:
        digest = self._model_digest
        digest_part = digest[:12] if digest else 'no-digest'
        return f"{VERSION}:{digest_part}"

    def embed(self, text: str) -> List[float]:
        """
        Call Ollama /api/embeddings and return the dense float vector.

        Lazily loads model_digest via /api/show before the first embedding.
        Validates that the returned vector length matches expected_dimensions.
        """
        import requests
        self._load_digest()
        resp = requests.post(
            f"{self._base_url}/api/embeddings",
            json={'model': self._model_name, 'prompt': text},
            timeout=60,
        )
        resp.raise_for_status()
        vector: List[float] = resp.json()['embedding']
        if len(vector) != self._expected_dimensions:
            raise ValueError(
                f"Ollama returned {len(vector)} dimensions; "
                f"expected {self._expected_dimensions} for model {self._model_name!r}"
            )
        return vector

    def get_provenance(self) -> dict:
        """Return embedding provenance including governance version token."""
        return {
            'adapter_name': self.adapter_name,
            'adapter_version': self.adapter_version,
            'model_name': self.model_name,
            'model_version': self.model_version,
            'model_digest': self._model_digest,
            'provider_name': self.provider_name,
            'dimensions': self.dimensions,
            'producer_version': self.producer_version,
            'embedding_visible_fields_version': EMBEDDING_VISIBLE_FIELDS_VERSION,
        }

    # --- Internal helpers ---

    def _load_digest(self) -> None:
        """Lazily fetch model digest from Ollama /api/show. Idempotent."""
        if self._digest_loaded:
            return
        import requests
        resp = requests.post(
            f"{self._base_url}/api/show",
            json={'name': self._model_name},
            timeout=30,
        )
        resp.raise_for_status()
        self._model_digest = resp.json().get('digest')
        self._digest_loaded = True

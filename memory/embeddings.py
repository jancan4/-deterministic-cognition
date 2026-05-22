"""
Embedding artifact persistence for the governed memory substrate.

Embedding rows (event_embeddings) are Tier 3 derived artifacts. They are
computed from canonical memory events but are never canonical themselves.

Governance invariants
---------------------
  - Only EMBEDDING_VISIBLE_FIELDS (title, summary) contribute to content_hash.
  - embed_event() never mutates memory_events.
  - embed_event() never promotes memory or approves embeddings.
  - All generated rows start as status='candidate'. Promotion is Phase 2C scope.
  - invalidate_stale_embeddings() uses mark_invalidated() from artifact_governance.

Continuity
----------
event_embeddings rows are local derived artifacts. They are excluded from
continuity bundles by governance policy. Future portability can be considered
explicitly, not silently.

Replay contract
---------------
Historical embedding artifacts are replayed from recorded event_embeddings rows.
Regeneration is explicit and provenance-preserving but may produce different float
values depending on model, runtime, or hardware. The canonical truth is the
recorded artifact plus its provenance metadata, not regenerated vector identity.
"""
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, List, Optional

from .artifact_governance import (
    EMBEDDING_VISIBLE_FIELDS_VERSION,
    ArtifactStatus,
    GovernanceInvalidationError,
    GovernancePinError,
    compute_content_hash,
    compute_pin_identity,
    mark_active,
    mark_invalidated,
    mark_superseded,
)
from .embedding_pins import PinRecord, get_active_pin
from .models import MemoryEvent

# Live statuses: rows eligible for idempotency checks and invalidation.
_LIVE_STATUSES = (ArtifactStatus.CANDIDATE, ArtifactStatus.ACTIVE)

_REQUIRED_COLUMNS = [
    'id', 'memory_event_id', 'content_hash', 'vector_json', 'dimensions',
    'model_name', 'model_version', 'model_digest', 'provider_name',
    'adapter_name', 'adapter_version', 'producer_version',
    'status', 'generated_at', 'invalidated_at', 'invalidated_reason',
    'provenance_json',
]
_REQUIRED_INDICES = [
    'idx_embeddings_event_id',
    'idx_embeddings_content_hash',
    'idx_embeddings_status',
    'idx_embeddings_producer_version',
]


@dataclass
class EmbeddingRow:
    id: int
    memory_event_id: int
    content_hash: str
    vector_json: str
    dimensions: int
    model_name: str
    model_version: str
    model_digest: Optional[str]
    provider_name: str
    adapter_name: str
    adapter_version: str
    producer_version: str
    status: str
    generated_at: str
    invalidated_at: Optional[str]
    invalidated_reason: Optional[str]
    provenance_json: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> 'EmbeddingRow':
        return cls(
            id=row['id'],
            memory_event_id=row['memory_event_id'],
            content_hash=row['content_hash'],
            vector_json=row['vector_json'],
            dimensions=row['dimensions'],
            model_name=row['model_name'],
            model_version=row['model_version'],
            model_digest=row['model_digest'],
            provider_name=row['provider_name'],
            adapter_name=row['adapter_name'],
            adapter_version=row['adapter_version'],
            producer_version=row['producer_version'],
            status=row['status'],
            generated_at=row['generated_at'],
            invalidated_at=row['invalidated_at'],
            invalidated_reason=row['invalidated_reason'],
            provenance_json=row['provenance_json'],
        )

    def to_dict(self) -> dict:
        return {
            'id': self.id,
            'memory_event_id': self.memory_event_id,
            'content_hash': self.content_hash,
            'vector_json': self.vector_json,
            'dimensions': self.dimensions,
            'model_name': self.model_name,
            'model_version': self.model_version,
            'model_digest': self.model_digest,
            'provider_name': self.provider_name,
            'adapter_name': self.adapter_name,
            'adapter_version': self.adapter_version,
            'producer_version': self.producer_version,
            'status': self.status,
            'generated_at': self.generated_at,
            'invalidated_at': self.invalidated_at,
            'invalidated_reason': self.invalidated_reason,
            'provenance_json': self.provenance_json,
        }

    @property
    def vector(self) -> List[float]:
        """Deserialize the stored vector."""
        return json.loads(self.vector_json)


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _open(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA foreign_keys=ON')
    return conn


def embed_event(
    db_path: str,
    event: MemoryEvent,
    adapter: Any,
    *,
    pin_scope: str = 'global',
    actor: str = 'operator',
) -> int:
    """
    Persist a candidate embedding for a memory event.

    Materializes title + summary (EMBEDDING_VISIBLE_FIELDS) and calls
    adapter.embed(). Validates that the returned vector length matches
    adapter.dimensions. Checks for an existing live (candidate or active) row
    with the same (memory_event_id, content_hash, producer_version) — returns
    that row's id without inserting if one exists.

    Pin provenance is recorded softly: the current active pin for pin_scope is
    looked up and its id is stored in provenance_json. Generation can proceed
    even when no active pin exists (pin_id=None, generation_validated=False).
    Hard pin enforcement happens at promotion time in promote_embedding().

    Does NOT promote the row to active. Does NOT mutate memory_events.

    Returns the event_embeddings row id (existing or newly inserted).
    """
    content_hash = compute_content_hash(event.title, event.summary)
    materialized = f"{event.title}\n{event.summary}"
    vector = adapter.embed(materialized)

    dims = adapter.dimensions
    if len(vector) != dims:
        raise ValueError(
            f"Adapter {adapter.adapter_name!r} declared {dims} dimensions "
            f"but embed() returned {len(vector)}"
        )

    vector_json = json.dumps(vector, ensure_ascii=True)
    prov = adapter.get_provenance()
    producer_version = adapter.producer_version
    now = _now_utc()

    # Soft pin provenance: record which pin (if any) was active at generation time.
    active_pin = get_active_pin(db_path, pin_scope=pin_scope)
    pin_id = active_pin.id if active_pin is not None else None
    generation_validated = active_pin is not None

    enriched_prov = dict(prov)
    enriched_prov['embedding_visible_fields_version'] = EMBEDDING_VISIBLE_FIELDS_VERSION
    enriched_prov['pin_scope'] = pin_scope
    enriched_prov['pin_id'] = pin_id
    enriched_prov['generation_validated'] = generation_validated
    enriched_prov['actor'] = actor
    provenance_json = json.dumps(enriched_prov, sort_keys=True, ensure_ascii=True)

    with _open(db_path) as conn:
        existing = conn.execute(
            "SELECT id FROM event_embeddings "
            "WHERE memory_event_id = ? AND content_hash = ? AND producer_version = ? "
            "AND status IN ('candidate', 'active')",
            (event.id, content_hash, producer_version),
        ).fetchone()
        if existing is not None:
            return existing['id']

        cur = conn.execute(
            "INSERT INTO event_embeddings "
            "(memory_event_id, content_hash, vector_json, dimensions, "
            " model_name, model_version, model_digest, provider_name, "
            " adapter_name, adapter_version, producer_version, "
            " status, generated_at, provenance_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                event.id,
                content_hash,
                vector_json,
                dims,
                prov['model_name'],
                prov['model_version'],
                prov.get('model_digest'),
                prov['provider_name'],
                prov['adapter_name'],
                prov['adapter_version'],
                producer_version,
                ArtifactStatus.CANDIDATE,
                now,
                provenance_json,
            ),
        )
        return cur.lastrowid


def get_embeddings(
    db_path: str,
    memory_event_id: int,
    *,
    status: Optional[str] = None,
) -> List[EmbeddingRow]:
    """Return embedding rows for a memory event, optionally filtered by status."""
    conn = _open(db_path)
    try:
        if status is not None:
            rows = conn.execute(
                "SELECT * FROM event_embeddings "
                "WHERE memory_event_id = ? AND status = ? ORDER BY id ASC",
                (memory_event_id, status),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM event_embeddings "
                "WHERE memory_event_id = ? ORDER BY id ASC",
                (memory_event_id,),
            ).fetchall()
        return [EmbeddingRow.from_row(r) for r in rows]
    finally:
        conn.close()


def get_active_embedding(
    db_path: str,
    memory_event_id: int,
) -> Optional[EmbeddingRow]:
    """
    Return the active embedding for a memory event, or None.

    In Phase 2B all generated embeddings remain 'candidate'. This returns None
    unless a row has been manually promoted to 'active' (e.g. in tests).
    Promotion flows are Phase 2C scope.
    """
    conn = _open(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM event_embeddings "
            "WHERE memory_event_id = ? AND status = 'active' "
            "ORDER BY id DESC LIMIT 1",
            (memory_event_id,),
        ).fetchone()
        return EmbeddingRow.from_row(row) if row is not None else None
    finally:
        conn.close()


def _validate_candidate_against_pin(
    candidate: 'EmbeddingRow',
    pin: PinRecord,
) -> bool:
    """
    Return True iff the candidate's embedding space matches the given pin.

    Recomputes pin_identity from the candidate's provenance fields and compares
    to pin.pin_identity. Uses the pin's embedding_visible_fields_version for
    the comparison, ensuring the identity encodes the same field-set version.
    """
    candidate_identity = compute_pin_identity(
        adapter_name=candidate.adapter_name,
        adapter_version=candidate.adapter_version,
        model_name=candidate.model_name,
        model_digest=candidate.model_digest,
        dimensions=candidate.dimensions,
        evfv=pin.embedding_visible_fields_version,
    )
    return candidate_identity == pin.pin_identity


def promote_embedding(
    db_path: str,
    embedding_id: int,
    *,
    reason: str,
    operator: str,
) -> 'EmbeddingRow':
    """
    Promote a candidate embedding to active.

    Enforces the at-most-one-active invariant: any existing active rows for the
    same memory_event_id are superseded via mark_superseded() before the candidate
    is promoted via mark_active(). All mutations happen atomically in one connection.

    Pin validation (hard gate): reads pin_scope from the candidate's provenance_json,
    fetches the CURRENT active pin for that scope, and validates that the candidate's
    embedding space matches the pin via compute_pin_identity(). Raises GovernancePinError
    if no active pin exists for the scope or if the identity does not match.
    Mismatched candidates remain in 'candidate' status (not auto-invalidated).

    Persists promotion audit metadata into provenance_json as a 'promotion' sub-key,
    preserving all existing generation provenance fields.

    Does NOT mutate memory_events. Does NOT approve memory. Does NOT trigger
    retrieval or semantic activation.

    Raises ValueError for empty reason or operator.
    Raises GovernanceInvalidationError if the row is not in 'candidate' status,
    or if the row does not exist.
    Raises GovernancePinError if no active pin exists for the candidate's scope,
    or if the candidate's embedding space does not match the active pin.
    """
    if not reason or not reason.strip():
        raise ValueError("'reason' must not be empty")
    if not operator or not operator.strip():
        raise ValueError("'operator' must not be empty")

    now = _now_utc()

    with _open(db_path) as conn:
        # Fetch and validate candidate before any mutations.
        row = conn.execute(
            "SELECT * FROM event_embeddings WHERE id = ?", (embedding_id,)
        ).fetchone()
        if row is None:
            raise GovernanceInvalidationError(
                f"Embedding id={embedding_id} not found"
            )
        candidate = EmbeddingRow.from_row(row)

        if candidate.status != ArtifactStatus.CANDIDATE:
            raise GovernanceInvalidationError(
                f"Cannot promote embedding id={embedding_id}: "
                f"current status={candidate.status!r}. "
                f"Only 'candidate' embeddings may be promoted to 'active'."
            )

        # Pin validation: hard gate at promotion time.
        candidate_prov = json.loads(candidate.provenance_json)
        pin_scope = candidate_prov.get('pin_scope', 'global')
        active_pin = get_active_pin(db_path, pin_scope=pin_scope)
        if active_pin is None:
            raise GovernancePinError(
                f"Cannot promote embedding id={embedding_id}: "
                f"no active pin for scope={pin_scope!r}. "
                f"Create a pin via 'pin-embedding-model' before promoting."
            )
        if not _validate_candidate_against_pin(candidate, active_pin):
            expected_identity = compute_pin_identity(
                adapter_name=candidate.adapter_name,
                adapter_version=candidate.adapter_version,
                model_name=candidate.model_name,
                model_digest=candidate.model_digest,
                dimensions=candidate.dimensions,
                evfv=active_pin.embedding_visible_fields_version,
            )
            raise GovernancePinError(
                f"Cannot promote embedding id={embedding_id}: "
                f"embedding space identity {expected_identity!r} does not match "
                f"active pin identity {active_pin.pin_identity!r} for scope={pin_scope!r}. "
                f"Candidate remains in 'candidate' status."
            )

        # Supersede all existing active rows for this memory_event_id.
        active_rows = conn.execute(
            "SELECT id FROM event_embeddings "
            "WHERE memory_event_id = ? AND status = 'active'",
            (candidate.memory_event_id,),
        ).fetchall()
        previous_active_ids = [r['id'] for r in active_rows]
        for prev_id in previous_active_ids:
            mark_superseded(
                conn, 'event_embeddings', prev_id,
                f"superseded by promotion of embedding_id={embedding_id}",
                now,
            )

        # Promote candidate to active (governance helper owns the status UPDATE).
        mark_active(conn, 'event_embeddings', embedding_id)

        # Persist promotion audit into provenance_json, preserving generation fields.
        existing_prov = json.loads(candidate.provenance_json)
        existing_prov['promotion'] = {
            'governance_action': 'promote_embedding',
            'operator': operator,
            'reason': reason,
            'promoted_at': now,
            'promoted_embedding_id': embedding_id,
            'memory_event_id': candidate.memory_event_id,
            'previous_active_embedding_ids': previous_active_ids,
            'pin_id': active_pin.id,
            'pin_scope': pin_scope,
            'pin_identity': active_pin.pin_identity,
        }
        updated_prov_json = json.dumps(existing_prov, sort_keys=True, ensure_ascii=True)
        conn.execute(
            "UPDATE event_embeddings SET provenance_json = ? WHERE id = ?",
            (updated_prov_json, embedding_id),
        )

        refreshed = conn.execute(
            "SELECT * FROM event_embeddings WHERE id = ?", (embedding_id,)
        ).fetchone()
        return EmbeddingRow.from_row(refreshed)


def invalidate_stale_embeddings(
    db_path: str,
    event: MemoryEvent,
) -> int:
    """
    Invalidate live embeddings whose content_hash no longer matches the event.

    Computes current content_hash from event.title + event.summary. Any
    candidate or active row with a different content_hash is invalidated via
    mark_invalidated(). Terminal rows (superseded, invalidated) are unchanged.

    Returns the count of rows invalidated.
    """
    current_hash = compute_content_hash(event.title, event.summary)
    now = _now_utc()
    count = 0

    with _open(db_path) as conn:
        stale = conn.execute(
            "SELECT id, content_hash FROM event_embeddings "
            "WHERE memory_event_id = ? AND status IN ('candidate', 'active') "
            "AND content_hash != ?",
            (event.id, current_hash),
        ).fetchall()
        for row in stale:
            reason = f"content_hash changed: {row['content_hash']} → {current_hash}"
            mark_invalidated(conn, 'event_embeddings', row['id'], reason, now)
            count += 1

    return count

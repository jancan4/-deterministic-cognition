"""
Derived artifact governance substrate.

This module is the canonical governance layer for all Tier 3 derived artifact
tables. Derived artifacts are computed from canonical memory or source data;
they are never canonical themselves and never enter continuity bundles without
operator promotion through the governed pipeline.

Governance contract
-------------------
Every derived artifact table MUST provide the semantic equivalents of:
  - producer_version  TEXT NOT NULL  — algorithm/model version that produced this
  - source_hash       TEXT NOT NULL  — fingerprint of the input (sha256[:16])
  - status            TEXT NOT NULL  — one of VALID_ARTIFACT_STATUSES
  - generated_at      TEXT NOT NULL  — ISO-8601 UTC wall-clock time

Invalidatable artifact tables (those that can be superseded or invalidated)
additionally require:
  - invalidated_at     TEXT          — NULL until superseded/invalidated
  - invalidated_reason TEXT          — NULL until superseded/invalidated

Embedding materialization invariant
-------------------------------------
EMBEDDING_VISIBLE_FIELDS defines exactly which memory_events fields participate
in content_hash (the embedding anchor). This set MUST NOT be expanded without
governance review: expanding it silently changes which memory_event revisions
trigger embedding invalidation and alters replay semantics. Any expansion
requires a producer_version bump in the embedding substrate.

Status state machine
---------------------
Valid transitions:
  candidate  -->  active        (promotion; not handled by mark_* helpers)
  candidate  -->  invalidated   (pre-activation rejection)
  active     -->  superseded    (same anchor, newer computation)
  active     -->  invalidated   (anchor changed: source or model changed)

Invalid transitions (raise GovernanceInvalidationError):
  superseded --> *  (terminal)
  invalidated --> * (terminal)
  active      --> candidate

Governed artifact table allowlist
-----------------------------------
_GOVERNED_ARTIFACT_TABLES is the allowlist for mark_invalidated() and
mark_superseded(). Only tables in this set may be mutated by those helpers.
Phase 2B registered 'event_embeddings' in this set.
"""
import hashlib
import sqlite3
from typing import FrozenSet, Iterable, Optional

DERIVED_ARTIFACT_GOVERNANCE_VERSION = '1.0.0'


class ArtifactStatus:
    CANDIDATE   = 'candidate'
    ACTIVE      = 'active'
    SUPERSEDED  = 'superseded'
    INVALIDATED = 'invalidated'


VALID_ARTIFACT_STATUSES: FrozenSet[str] = frozenset({
    ArtifactStatus.CANDIDATE,
    ArtifactStatus.ACTIVE,
    ArtifactStatus.SUPERSEDED,
    ArtifactStatus.INVALIDATED,
})

# Canonical set of memory_events fields that participate in embedding
# materialization. content_hash = sha256(title + NUL + summary)[:16].
#
# GOVERNANCE INVARIANT: Do NOT expand this set without governance review.
# Expanding embedding-visible fields silently changes which memory_event
# revisions trigger embedding invalidation and alters replay semantics.
# Any expansion requires a producer_version bump in the embedding substrate.
EMBEDDING_VISIBLE_FIELDS: FrozenSet[str] = frozenset({'title', 'summary'})

# Allowlist of governed derived artifact table names for invalidation helpers.
# mark_invalidated() and mark_superseded() refuse to operate on unlisted tables.
# Tables in this set MUST have: status, invalidated_at, invalidated_reason columns.
_GOVERNED_ARTIFACT_TABLES: FrozenSet[str] = frozenset({
    'event_embeddings',
})

# Valid source statuses for mark_invalidated (candidate or active).
_INVALIDATABLE_FROM: FrozenSet[str] = frozenset({
    ArtifactStatus.CANDIDATE,
    ArtifactStatus.ACTIVE,
})


class GovernanceSchemaError(Exception):
    """Raised when a derived artifact table fails governance schema validation,
    or when a mutation helper is called with a table not in _GOVERNED_ARTIFACT_TABLES."""


class GovernanceInvalidationError(ValueError):
    """Raised when an invalidation attempt violates the append-only state machine."""


def compute_content_hash(title: str, summary: str) -> str:
    """
    Canonical embedding anchor: sha256(title + NUL + summary)[:16].

    Only fields in EMBEDDING_VISIBLE_FIELDS (title, summary) participate.
    Status, tags, confidence, governance metadata, workflow lineage, and link
    relationships do NOT participate — they must not affect embedding invalidation.

    The NUL byte separator prevents prefix-collision:
      compute_content_hash('ab', '') != compute_content_hash('a', 'b')
    """
    raw = f"{title}\x00{summary}".encode('utf-8')
    return hashlib.sha256(raw).hexdigest()[:16]


def validate_artifact_table_schema(
    conn: sqlite3.Connection,
    table_name: str,
    required_columns: Iterable[str],
    required_indices: Optional[Iterable[str]] = None,
) -> None:
    """
    Raise GovernanceSchemaError if the table is missing required columns or indices.

    Uses PRAGMA table_info and PRAGMA index_list — read-only, no writes.
    Column type and nullability are NOT validated; presence is sufficient for
    Phase 2A. Safe to call from tests against tmp_path DBs.
    """
    existing_columns = {row[1] for row in conn.execute(f'PRAGMA table_info({table_name})')}
    missing_cols = sorted(set(required_columns) - existing_columns)
    if missing_cols:
        raise GovernanceSchemaError(
            f"Table '{table_name}' is missing required governance columns: {missing_cols}"
        )

    if required_indices:
        existing_indices = {
            row[1] for row in conn.execute(f'PRAGMA index_list({table_name})')
        }
        missing_idx = sorted(set(required_indices) - existing_indices)
        if missing_idx:
            raise GovernanceSchemaError(
                f"Table '{table_name}' is missing required governance indices: {missing_idx}"
            )


def _require_governed_table(table_name: str) -> None:
    if table_name not in _GOVERNED_ARTIFACT_TABLES:
        raise GovernanceSchemaError(
            f"Table '{table_name}' is not in the governed artifact table allowlist. "
            f"Known invalidatable tables: {sorted(_GOVERNED_ARTIFACT_TABLES)}."
        )


def mark_invalidated(
    conn: sqlite3.Connection,
    table_name: str,
    artifact_id: int,
    reason: str,
    now: str,
) -> None:
    """
    Append-only transition: set status='invalidated', record when and why.

    Sets invalidated_at=now, invalidated_reason=reason on the artifact row.

    Valid from: status='active' or status='candidate'.
    Invalid from: status='superseded' or status='invalidated' — terminal states.
    Raises GovernanceSchemaError if table_name not in _GOVERNED_ARTIFACT_TABLES.
    Raises GovernanceInvalidationError on invalid transition or missing row.
    """
    _require_governed_table(table_name)
    row = conn.execute(
        f'SELECT status FROM {table_name} WHERE id = ?', (artifact_id,)
    ).fetchone()
    if row is None:
        raise GovernanceInvalidationError(
            f"Artifact id={artifact_id} not found in '{table_name}'"
        )
    current = row[0]
    if current not in _INVALIDATABLE_FROM:
        raise GovernanceInvalidationError(
            f"Cannot invalidate artifact id={artifact_id} in '{table_name}': "
            f"current status={current!r} is terminal. "
            f"Only {sorted(_INVALIDATABLE_FROM)} may be invalidated."
        )
    conn.execute(
        f"UPDATE {table_name} "
        f"SET status = ?, invalidated_at = ?, invalidated_reason = ? "
        f"WHERE id = ?",
        (ArtifactStatus.INVALIDATED, now, reason, artifact_id),
    )


def mark_superseded(
    conn: sqlite3.Connection,
    table_name: str,
    artifact_id: int,
    reason: str,
    now: str,
) -> None:
    """
    Append-only transition: set status='superseded', record when and why.

    Sets invalidated_at=now, invalidated_reason=reason on the artifact row.
    Supersession represents replacement by a newer computation for the same anchor
    (e.g. embedding model upgrade). It does not imply the anchor changed.

    Valid from: status='active' only.
    Invalid from: status='candidate', 'superseded', or 'invalidated'.
    Raises GovernanceSchemaError if table_name not in _GOVERNED_ARTIFACT_TABLES.
    Raises GovernanceInvalidationError on invalid transition or missing row.
    """
    _require_governed_table(table_name)
    row = conn.execute(
        f'SELECT status FROM {table_name} WHERE id = ?', (artifact_id,)
    ).fetchone()
    if row is None:
        raise GovernanceInvalidationError(
            f"Artifact id={artifact_id} not found in '{table_name}'"
        )
    current = row[0]
    if current != ArtifactStatus.ACTIVE:
        raise GovernanceInvalidationError(
            f"Cannot supersede artifact id={artifact_id} in '{table_name}': "
            f"current status={current!r}. Only 'active' artifacts may be superseded."
        )
    conn.execute(
        f"UPDATE {table_name} "
        f"SET status = ?, invalidated_at = ?, invalidated_reason = ? "
        f"WHERE id = ?",
        (ArtifactStatus.SUPERSEDED, now, reason, artifact_id),
    )

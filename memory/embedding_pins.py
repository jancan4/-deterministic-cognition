"""
Model pinning governance for the embedding substrate.

embedding_model_pins records define the governed embedding space. An active
pin declares which adapter/model combination is the current operator-approved
embedding space for a given scope.

Pin governance
--------------
- At most one pin may be 'active' per scope at any time.
- create_pin() atomically supersedes the current active pin (if any),
  then inserts a new active pin, within a single transaction.
- Supersession is append-only: superseded_at and superseded_reason are set
  and never cleared.
- get_active_pin() returns the active pin for a scope, or None.
- list_pins() returns all pins for a scope, newest first.
- supersede_pin() explicitly supersedes a specific pin by id; used internally
  by create_pin() and available for operator workflows.

Pin identity
------------
pin_identity is a deterministic hash from compute_pin_identity() in
artifact_governance. Two embedding spaces are equivalent iff their
pin_identity matches. The identity encodes: adapter_name, adapter_version,
model_name, model_digest (None → 'null'), dimensions,
embedding_visible_fields_version.

TODO (Phase 3A): Unify embedding generation with workflow lineage before
embedding-aware retrieval becomes canonical. Pin records should carry
workflow_scope information for lineage tracking.
"""
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional

from .artifact_governance import (
    EMBEDDING_VISIBLE_FIELDS_VERSION,
    compute_pin_identity,
)


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _open(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA foreign_keys=ON')
    return conn


@dataclass
class PinRecord:
    id: int
    pin_scope: str
    adapter_name: str
    adapter_version: str
    model_name: str
    model_digest: Optional[str]
    dimensions: int
    embedding_visible_fields_version: str
    pin_identity: str
    provider_name: str
    status: str
    pinned_at: str
    pinned_by: str
    superseded_at: Optional[str]
    superseded_reason: Optional[str]
    notes: Optional[str]

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> 'PinRecord':
        return cls(
            id=row['id'],
            pin_scope=row['pin_scope'],
            adapter_name=row['adapter_name'],
            adapter_version=row['adapter_version'],
            model_name=row['model_name'],
            model_digest=row['model_digest'],
            dimensions=row['dimensions'],
            embedding_visible_fields_version=row['embedding_visible_fields_version'],
            pin_identity=row['pin_identity'],
            provider_name=row['provider_name'],
            status=row['status'],
            pinned_at=row['pinned_at'],
            pinned_by=row['pinned_by'],
            superseded_at=row['superseded_at'],
            superseded_reason=row['superseded_reason'],
            notes=row['notes'],
        )

    def to_dict(self) -> dict:
        return {
            'id': self.id,
            'pin_scope': self.pin_scope,
            'adapter_name': self.adapter_name,
            'adapter_version': self.adapter_version,
            'model_name': self.model_name,
            'model_digest': self.model_digest,
            'dimensions': self.dimensions,
            'embedding_visible_fields_version': self.embedding_visible_fields_version,
            'pin_identity': self.pin_identity,
            'provider_name': self.provider_name,
            'status': self.status,
            'pinned_at': self.pinned_at,
            'pinned_by': self.pinned_by,
            'superseded_at': self.superseded_at,
            'superseded_reason': self.superseded_reason,
            'notes': self.notes,
        }


def create_pin(
    db_path: str,
    *,
    adapter_name: str,
    adapter_version: str,
    model_name: str,
    model_digest: Optional[str],
    dimensions: int,
    provider_name: str,
    pinned_by: str,
    pin_scope: str = 'global',
    notes: Optional[str] = None,
) -> PinRecord:
    """
    Create a new active pin for the given scope, atomically superseding the
    current active pin (if any).

    pin_identity is computed deterministically from the adapter/model fields
    and the current EMBEDDING_VISIBLE_FIELDS_VERSION. It is stored in the row
    and used for promotion-time validation in promote_embedding().

    Returns the newly inserted PinRecord.
    """
    now = _now_utc()
    identity = compute_pin_identity(
        adapter_name=adapter_name,
        adapter_version=adapter_version,
        model_name=model_name,
        model_digest=model_digest,
        dimensions=dimensions,
        evfv=EMBEDDING_VISIBLE_FIELDS_VERSION,
    )

    with _open(db_path) as conn:
        # Supersede the current active pin for this scope (if any).
        active_rows = conn.execute(
            "SELECT id FROM embedding_model_pins WHERE pin_scope = ? AND status = 'active'",
            (pin_scope,),
        ).fetchall()
        for row in active_rows:
            conn.execute(
                "UPDATE embedding_model_pins "
                "SET status = 'superseded', superseded_at = ?, superseded_reason = ? "
                "WHERE id = ?",
                (now, f"superseded by new pin created by {pinned_by!r}", row['id']),
            )

        cur = conn.execute(
            "INSERT INTO embedding_model_pins "
            "(pin_scope, adapter_name, adapter_version, model_name, model_digest, "
            " dimensions, embedding_visible_fields_version, pin_identity, provider_name, "
            " status, pinned_at, pinned_by, notes) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                pin_scope,
                adapter_name,
                adapter_version,
                model_name,
                model_digest,
                dimensions,
                EMBEDDING_VISIBLE_FIELDS_VERSION,
                identity,
                provider_name,
                'active',
                now,
                pinned_by,
                notes,
            ),
        )
        row = conn.execute(
            "SELECT * FROM embedding_model_pins WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
        return PinRecord.from_row(row)


def get_active_pin(
    db_path: str,
    *,
    pin_scope: str = 'global',
) -> Optional[PinRecord]:
    """Return the current active pin for the given scope, or None."""
    with _open(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM embedding_model_pins "
            "WHERE pin_scope = ? AND status = 'active' "
            "ORDER BY id DESC LIMIT 1",
            (pin_scope,),
        ).fetchone()
        return PinRecord.from_row(row) if row is not None else None


def supersede_pin(
    db_path: str,
    pin_id: int,
    *,
    reason: str,
) -> PinRecord:
    """
    Explicitly supersede a pin by id. Append-only: sets status='superseded',
    superseded_at, superseded_reason.

    Raises ValueError if pin_id does not exist or is not in 'active' status.
    """
    now = _now_utc()
    with _open(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM embedding_model_pins WHERE id = ?", (pin_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"Pin id={pin_id} not found")
        if row['status'] != 'active':
            raise ValueError(
                f"Cannot supersede pin id={pin_id}: "
                f"current status={row['status']!r}. Only 'active' pins may be superseded."
            )
        conn.execute(
            "UPDATE embedding_model_pins "
            "SET status = 'superseded', superseded_at = ?, superseded_reason = ? "
            "WHERE id = ?",
            (now, reason, pin_id),
        )
        refreshed = conn.execute(
            "SELECT * FROM embedding_model_pins WHERE id = ?", (pin_id,)
        ).fetchone()
        return PinRecord.from_row(refreshed)


def list_pins(
    db_path: str,
    *,
    pin_scope: str = 'global',
) -> List[PinRecord]:
    """Return all pins for the given scope, newest first."""
    with _open(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM embedding_model_pins WHERE pin_scope = ? ORDER BY id DESC",
            (pin_scope,),
        ).fetchall()
        return [PinRecord.from_row(r) for r in rows]

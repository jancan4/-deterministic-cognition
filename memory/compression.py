"""
Governed compression artifact lifecycle.

Phase 6A: create, retrieve, list, promote, invalidate compression artifacts.
No automatic summarization, no model calls, no autonomous promotion.
All mutations are explicit, provenance-preserving, and auditable.

Status state machine (from artifact_governance.py):
  candidate  -->  active       (promote_compression_artifact, requires operator)
  candidate  -->  invalidated  (invalidate_compression_artifact)
  active     -->  invalidated  (invalidate_compression_artifact)
  superseded, invalidated: terminal — no further transitions

Promotion to canonical memory is Phase 6B scope. No promoted_memory_id column exists.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

from .artifact_governance import ArtifactStatus, mark_active, mark_invalidated


_TABLE = 'compression_artifacts'


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    return conn


@dataclass
class CompressionArtifact:
    id: int
    source_assembly_id: int
    source_assembly_hash: str
    cognition_session_id: Optional[int]
    compression_method: str
    producer_version: str
    artifact_text: str
    artifact_char_count: int
    source_memory_event_ids: List[int]
    source_contradiction_link_ids: List[int]
    confidence_snapshot: Dict
    excluded_event_ids: List[int]
    unresolved_issue_count: int
    compression_confidence: Optional[int]
    status: str
    generated_at: str
    invalidated_at: Optional[str]
    invalidated_reason: Optional[str]
    promoted_by: Optional[str]
    promoted_at: Optional[str]
    promotion_notes: Optional[str]
    provenance: Dict

    def to_dict(self) -> dict:
        return {
            'id': self.id,
            'source_assembly_id': self.source_assembly_id,
            'source_assembly_hash': self.source_assembly_hash,
            'cognition_session_id': self.cognition_session_id,
            'compression_method': self.compression_method,
            'producer_version': self.producer_version,
            'artifact_text': self.artifact_text,
            'artifact_char_count': self.artifact_char_count,
            'source_memory_event_ids': self.source_memory_event_ids,
            'source_contradiction_link_ids': self.source_contradiction_link_ids,
            'confidence_snapshot': self.confidence_snapshot,
            'excluded_event_ids': self.excluded_event_ids,
            'unresolved_issue_count': self.unresolved_issue_count,
            'compression_confidence': self.compression_confidence,
            'status': self.status,
            'generated_at': self.generated_at,
            'invalidated_at': self.invalidated_at,
            'invalidated_reason': self.invalidated_reason,
            'promoted_by': self.promoted_by,
            'promoted_at': self.promoted_at,
            'promotion_notes': self.promotion_notes,
            'provenance': self.provenance,
        }


def _row_to_artifact(row: sqlite3.Row) -> CompressionArtifact:
    return CompressionArtifact(
        id=row['id'],
        source_assembly_id=row['source_assembly_id'],
        source_assembly_hash=row['source_assembly_hash'],
        cognition_session_id=row['cognition_session_id'],
        compression_method=row['compression_method'],
        producer_version=row['producer_version'],
        artifact_text=row['artifact_text'],
        artifact_char_count=row['artifact_char_count'],
        source_memory_event_ids=json.loads(row['source_memory_event_ids_json']),
        source_contradiction_link_ids=json.loads(row['source_contradiction_link_ids_json']),
        confidence_snapshot=json.loads(row['confidence_snapshot_json']),
        excluded_event_ids=json.loads(row['excluded_event_ids_json']),
        unresolved_issue_count=row['unresolved_issue_count'],
        compression_confidence=row['compression_confidence'],
        status=row['status'],
        generated_at=row['generated_at'],
        invalidated_at=row['invalidated_at'],
        invalidated_reason=row['invalidated_reason'],
        promoted_by=row['promoted_by'],
        promoted_at=row['promoted_at'],
        promotion_notes=row['promotion_notes'],
        provenance=json.loads(row['provenance_json']),
    )


def _extract_provenance_from_snapshot(snapshot: dict) -> tuple:
    """Extract (event_ids, contradiction_link_ids, confidence_snapshot) from assembly_snapshot_json."""
    memory_sections = (
        snapshot.get('governance_context', []),
        snapshot.get('unresolved_items', []),
        snapshot.get('active_investigations', []),
        snapshot.get('relevant_memory', []),
    )
    event_ids = sorted({
        item['memory_id']
        for section in memory_sections
        for item in section
        if 'memory_id' in item
    })
    confidence_snap = {
        str(item['memory_id']): item.get('confidence')
        for section in memory_sections
        for item in section
        if 'memory_id' in item
    }
    contradiction_link_ids = sorted({
        pair['link_id']
        for pair in snapshot.get('conflicting_pairs', [])
        if 'link_id' in pair
    })
    return event_ids, contradiction_link_ids, confidence_snap


def create_compression_artifact(
    db_path: str,
    source_assembly_id: int,
    compression_method: str,
    producer_version: str,
    artifact_text: str,
    created_by: str,
    cognition_session_id: Optional[int] = None,
    compression_confidence: Optional[int] = None,
    excluded_event_ids: Optional[List[int]] = None,
    unresolved_issue_count: int = 0,
    provenance: Optional[Dict] = None,
) -> CompressionArtifact:
    """
    Persist a new compression artifact in status='candidate'.

    Provenance snapshot is extracted deterministically from the source assembly's
    assembly_snapshot_json. excluded_event_ids and unresolved_issue_count are
    caller-supplied (they are not stored in the assembly snapshot).

    Raises ValueError on invalid inputs or missing source assembly.
    """
    if not compression_method or not compression_method.strip():
        raise ValueError("compression_method must not be empty")
    if not producer_version or not producer_version.strip():
        raise ValueError("producer_version must not be empty")
    if not artifact_text or not artifact_text.strip():
        raise ValueError("artifact_text must not be empty")
    if not created_by or not created_by.strip():
        raise ValueError("created_by must not be empty")
    if compression_confidence is not None and not (1 <= compression_confidence <= 5):
        raise ValueError("compression_confidence must be 1–5 or None")

    excluded_event_ids = excluded_event_ids or []
    provenance = provenance or {}
    now = _now_utc()

    conn = _connect(db_path)
    try:
        asm_row = conn.execute(
            "SELECT id, assembly_hash, assembly_snapshot_json FROM context_assembly_log WHERE id = ?",
            (source_assembly_id,),
        ).fetchone()
        if asm_row is None:
            raise ValueError(f"source_assembly_id={source_assembly_id} not found in context_assembly_log")

        source_hash = asm_row['assembly_hash']
        snapshot = json.loads(asm_row['assembly_snapshot_json'])
        event_ids, contradiction_link_ids, confidence_snap = _extract_provenance_from_snapshot(snapshot)

        full_provenance = dict(provenance)
        full_provenance['created_by'] = created_by
        full_provenance['created_at'] = now

        cur = conn.execute(
            """
            INSERT INTO compression_artifacts (
                source_assembly_id, source_assembly_hash, cognition_session_id,
                compression_method, producer_version,
                artifact_text, artifact_char_count,
                source_memory_event_ids_json, source_contradiction_link_ids_json,
                confidence_snapshot_json, excluded_event_ids_json, unresolved_issue_count,
                compression_confidence,
                status, generated_at, provenance_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'candidate', ?, ?)
            """,
            (
                source_assembly_id,
                source_hash,
                cognition_session_id,
                compression_method,
                producer_version,
                artifact_text,
                len(artifact_text),
                json.dumps(event_ids),
                json.dumps(contradiction_link_ids),
                json.dumps(confidence_snap),
                json.dumps(excluded_event_ids),
                unresolved_issue_count,
                compression_confidence,
                now,
                json.dumps(full_provenance),
            ),
        )
        artifact_id = cur.lastrowid
        conn.commit()
        row = conn.execute(
            "SELECT * FROM compression_artifacts WHERE id = ?", (artifact_id,)
        ).fetchone()
        return _row_to_artifact(row)
    finally:
        conn.close()


def get_compression_artifact(db_path: str, artifact_id: int) -> CompressionArtifact:
    """Return a single compression artifact by id. Raises ValueError if not found."""
    conn = _connect(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM compression_artifacts WHERE id = ?", (artifact_id,)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise ValueError(f"compression artifact id={artifact_id} not found")
    return _row_to_artifact(row)


def list_compression_artifacts(
    db_path: str,
    status: Optional[str] = None,
    compression_method: Optional[str] = None,
    source_assembly_id: Optional[int] = None,
    limit: int = 50,
) -> List[CompressionArtifact]:
    """List compression artifacts with optional filters. Ordered by generated_at DESC."""
    query = "SELECT * FROM compression_artifacts WHERE 1=1"
    params: list = []
    if status is not None:
        query += " AND status = ?"
        params.append(status)
    if compression_method is not None:
        query += " AND compression_method = ?"
        params.append(compression_method)
    if source_assembly_id is not None:
        query += " AND source_assembly_id = ?"
        params.append(source_assembly_id)
    query += " ORDER BY generated_at DESC LIMIT ?"
    params.append(limit)

    conn = _connect(db_path)
    try:
        rows = conn.execute(query, params).fetchall()
    finally:
        conn.close()
    return [_row_to_artifact(r) for r in rows]


def promote_compression_artifact(
    db_path: str,
    artifact_id: int,
    promoted_by: str,
    promotion_notes: str,
) -> CompressionArtifact:
    """
    Promote a candidate compression artifact to active.

    Sets status='active', records promoted_by, promoted_at, promotion_notes.
    Both promoted_by and promotion_notes must be non-empty (operator accountability).
    Raises ValueError on empty inputs. Raises GovernanceInvalidationError on invalid transition.
    """
    if not promoted_by or not promoted_by.strip():
        raise ValueError("promoted_by must not be empty")
    if not promotion_notes or not promotion_notes.strip():
        raise ValueError("promotion_notes must not be empty")

    now = _now_utc()
    conn = _connect(db_path)
    try:
        mark_active(conn, _TABLE, artifact_id)
        conn.execute(
            """
            UPDATE compression_artifacts
            SET promoted_by = ?, promoted_at = ?, promotion_notes = ?
            WHERE id = ?
            """,
            (promoted_by, now, promotion_notes, artifact_id),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM compression_artifacts WHERE id = ?", (artifact_id,)
        ).fetchone()
        return _row_to_artifact(row)
    finally:
        conn.close()


def invalidate_compression_artifact(
    db_path: str,
    artifact_id: int,
    reason: str,
    invalidated_by: str,
) -> CompressionArtifact:
    """
    Invalidate a candidate or active compression artifact.

    Records invalidated_at, invalidated_reason in the artifact row.
    Both reason and invalidated_by must be non-empty (operator accountability).
    Raises ValueError on empty inputs. Raises GovernanceInvalidationError on invalid transition.

    NOTE: Invalidation is NOT rejection. It means the source or artifact is
    no longer valid (e.g., source assembly was superseded, artifact is stale).
    There is no 'rejected' status in Phase 6A.
    """
    if not reason or not reason.strip():
        raise ValueError("reason must not be empty")
    if not invalidated_by or not invalidated_by.strip():
        raise ValueError("invalidated_by must not be empty")

    now = _now_utc()
    conn = _connect(db_path)
    try:
        mark_invalidated(conn, _TABLE, artifact_id, reason, now)
        conn.commit()
        row = conn.execute(
            "SELECT * FROM compression_artifacts WHERE id = ?", (artifact_id,)
        ).fetchone()
        return _row_to_artifact(row)
    finally:
        conn.close()

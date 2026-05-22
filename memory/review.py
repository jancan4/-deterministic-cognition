import sqlite3
from dataclasses import dataclass, field
from typing import List

from .governance import (
    GovernanceIssue,
    LOW_CONFIDENCE_ACTIVE_THRESHOLD,
    STALE_WARNING_DAYS,
    UNRESOLVED_WARNING_DAYS,
    _connect,
    _cutoff,
    detect_conflicts,
    detect_deprecated_linked,
)
from .models import MemoryEvent


@dataclass
class ReviewQueue:
    unresolved: List[MemoryEvent] = field(default_factory=list)
    stale: List[MemoryEvent] = field(default_factory=list)
    low_confidence_active: List[MemoryEvent] = field(default_factory=list)
    deprecated_linked: List[MemoryEvent] = field(default_factory=list)
    conflicts: List[GovernanceIssue] = field(default_factory=list)

    @property
    def total(self) -> int:
        seen = set()
        for ev in (
            self.unresolved
            + self.stale
            + self.low_confidence_active
            + self.deprecated_linked
        ):
            seen.add(ev.id)
        return len(seen) + len(self.conflicts)

    def is_empty(self) -> bool:
        return (
            not self.unresolved
            and not self.stale
            and not self.low_confidence_active
            and not self.deprecated_linked
            and not self.conflicts
        )

    def to_dict(self) -> dict:
        return {
            'total': self.total,
            'unresolved': [e.to_dict() for e in self.unresolved],
            'stale': [e.to_dict() for e in self.stale],
            'low_confidence_active': [e.to_dict() for e in self.low_confidence_active],
            'deprecated_linked': [e.to_dict() for e in self.deprecated_linked],
            'conflicts': [i.to_dict() for i in self.conflicts],
        }


def review_unresolved(
    db_path: str,
    aging_days: int = UNRESOLVED_WARNING_DAYS,
    limit: int = 50,
) -> List[MemoryEvent]:
    """Unresolved events older than aging_days, ordered oldest-first for prioritised triage."""
    cutoff = _cutoff(aging_days)
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM memory_events WHERE status = 'unresolved' AND created_at < ?"
            " ORDER BY created_at ASC LIMIT ?",
            (cutoff, limit),
        ).fetchall()
        return [MemoryEvent.from_row(r) for r in rows]
    finally:
        conn.close()


def review_stale(
    db_path: str,
    stale_days: int = STALE_WARNING_DAYS,
    limit: int = 50,
) -> List[MemoryEvent]:
    """Active/proposed events not updated within stale_days, ordered least-recently-updated first."""
    cutoff = _cutoff(stale_days)
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM memory_events WHERE status IN ('active', 'proposed') AND updated_at < ?"
            " ORDER BY updated_at ASC LIMIT ?",
            (cutoff, limit),
        ).fetchall()
        return [MemoryEvent.from_row(r) for r in rows]
    finally:
        conn.close()


def review_conflicts(db_path: str) -> List[GovernanceIssue]:
    """Conflict issues ordered by memory_id ascending for deterministic human review."""
    return sorted(detect_conflicts(db_path), key=lambda i: i.memory_id)


def review_low_confidence_active(
    db_path: str,
    max_confidence: int = LOW_CONFIDENCE_ACTIVE_THRESHOLD,
    limit: int = 50,
) -> List[MemoryEvent]:
    """Active/accepted events with confidence at or below max_confidence.
    Ordered by confidence ascending (lowest first), then id ascending.
    """
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM memory_events WHERE status IN ('active', 'accepted') AND confidence <= ?"
            " ORDER BY confidence ASC, id ASC LIMIT ?",
            (max_confidence, limit),
        ).fetchall()
        return [MemoryEvent.from_row(r) for r in rows]
    finally:
        conn.close()


def review_deprecated_linked(
    db_path: str,
    limit: int = 50,
) -> List[MemoryEvent]:
    """Deprecated events still linked from active/accepted/proposed events, ordered by id ascending."""
    conn = _connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT DISTINCT e.*
            FROM memory_events e
            JOIN memory_links ml ON ml.target_id = e.id
            JOIN memory_events src ON src.id = ml.source_id
            WHERE e.status = 'deprecated'
              AND src.status IN ('active', 'accepted', 'proposed')
            ORDER BY e.id ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [MemoryEvent.from_row(r) for r in rows]
    finally:
        conn.close()


def get_review_queue(
    db_path: str,
    stale_days: int = STALE_WARNING_DAYS,
    unresolved_aging_days: int = UNRESOLVED_WARNING_DAYS,
    max_confidence: int = LOW_CONFIDENCE_ACTIVE_THRESHOLD,
    limit: int = 50,
) -> ReviewQueue:
    """Build a combined deterministic review queue from all active review categories."""
    return ReviewQueue(
        unresolved=review_unresolved(db_path, aging_days=unresolved_aging_days, limit=limit),
        stale=review_stale(db_path, stale_days=stale_days, limit=limit),
        low_confidence_active=review_low_confidence_active(
            db_path, max_confidence=max_confidence, limit=limit
        ),
        deprecated_linked=review_deprecated_linked(db_path, limit=limit),
        conflicts=review_conflicts(db_path),
    )

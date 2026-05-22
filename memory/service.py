import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple

from .models import (
    CONFIDENCE_MAX, CONFIDENCE_MIN,
    REVIEW_STATUSES,
    VALID_EVENT_TYPES, VALID_RELATIONSHIPS, VALID_STATUSES,
    MemoryEvent, MemoryLink, MemoryRevision,
)

_SCHEMA = Path(__file__).parent / 'schema.sql'
_MEMORY_SCHEMA_VERSION = 6


class ValidationError(ValueError):
    pass


class NotFoundError(KeyError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA foreign_keys=ON')
    return conn


def _validate_confidence(confidence: int) -> None:
    if isinstance(confidence, bool) or not isinstance(confidence, int):
        raise ValidationError(
            f"confidence must be an integer, got {type(confidence).__name__}"
        )
    if not CONFIDENCE_MIN <= confidence <= CONFIDENCE_MAX:
        raise ValidationError(
            f"confidence must be {CONFIDENCE_MIN}–{CONFIDENCE_MAX}, got {confidence}"
        )


def _migrate_to_v6(conn: sqlite3.Connection) -> None:
    # Add semantic_mode and semantic_provenance_json to retrieval_log if absent.
    # For fresh DBs the columns already exist via schema.sql; the guards make
    # this function safe to call in both fresh and upgrade paths.
    existing_cols = {row[1] for row in conn.execute('PRAGMA table_info(retrieval_log)')}
    if 'semantic_mode' not in existing_cols:
        conn.execute(
            "ALTER TABLE retrieval_log ADD COLUMN semantic_mode TEXT NOT NULL DEFAULT 'none'"
        )
    if 'semantic_provenance_json' not in existing_cols:
        conn.execute(
            "ALTER TABLE retrieval_log ADD COLUMN semantic_provenance_json TEXT"
        )


def _migrate_to_v5(conn: sqlite3.Connection) -> None:
    # embedding_model_pins was added to schema.sql in v5. For all DBs (fresh and
    # upgraded), executescript() runs before this function and creates the table
    # via CREATE TABLE IF NOT EXISTS. This function idempotently ensures all three
    # governance indices exist, mirroring the _migrate_to_v3/_migrate_to_v4 pattern.
    # Indices are created here (not in schema.sql) so they are never attempted
    # before the table exists on pre-v5 DBs.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pins_scope_status "
        "ON embedding_model_pins(pin_scope, status)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pins_identity "
        "ON embedding_model_pins(pin_identity)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pins_pinned_at "
        "ON embedding_model_pins(pinned_at)"
    )


def _migrate_to_v4(conn: sqlite3.Connection) -> None:
    # event_embeddings was added to schema.sql in v4. For all DBs (fresh and
    # upgraded), executescript() runs before this function and creates the table
    # and indices via CREATE TABLE/INDEX IF NOT EXISTS. This function idempotently
    # ensures all four governance indices exist in case they were created outside
    # the normal init_db path.
    for idx, col in (
        ('idx_embeddings_event_id',         'memory_event_id'),
        ('idx_embeddings_content_hash',     'content_hash'),
        ('idx_embeddings_status',           'status'),
        ('idx_embeddings_producer_version', 'producer_version'),
    ):
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS {idx} ON event_embeddings({col})"
        )


def _migrate_to_v3(conn: sqlite3.Connection) -> None:
    # Add status column to retrieval_log if absent (v2 → v3 upgrade path).
    # For fresh DBs the column already exists via schema.sql; the guard makes
    # this function safe to call in both cases.
    existing_cols = {row[1] for row in conn.execute('PRAGMA table_info(retrieval_log)')}
    if 'status' not in existing_cols:
        conn.execute(
            "ALTER TABLE retrieval_log ADD COLUMN status TEXT NOT NULL DEFAULT 'active'"
        )
    # Create the status index here rather than in schema.sql so it is never
    # attempted before the column exists on a v2 DB.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_retrieval_log_status ON retrieval_log(status)"
    )


def init_db(db_path: str) -> None:
    with _connect(db_path) as conn:
        conn.executescript(_SCHEMA.read_text())
        row = conn.execute('SELECT version FROM memory_schema_version').fetchone()
        if row is None:
            # Fresh DB: schema.sql created retrieval_log with status, but the
            # status index is not in schema.sql (see comment there). Run all
            # migrations idempotently to ensure all indices and columns exist.
            _migrate_to_v3(conn)
            _migrate_to_v4(conn)
            _migrate_to_v5(conn)
            _migrate_to_v6(conn)
            conn.execute(
                'INSERT INTO memory_schema_version (version) VALUES (?)',
                (_MEMORY_SCHEMA_VERSION,)
            )
        elif row['version'] < _MEMORY_SCHEMA_VERSION:
            if row['version'] < 3:
                _migrate_to_v3(conn)
            if row['version'] < 4:
                _migrate_to_v4(conn)
            if row['version'] < 5:
                _migrate_to_v5(conn)
            if row['version'] < 6:
                _migrate_to_v6(conn)
            conn.execute(
                'UPDATE memory_schema_version SET version = ?',
                (_MEMORY_SCHEMA_VERSION,)
            )


def add_memory_event(
    db_path: str,
    event_type: str,
    title: str,
    summary: str,
    source: str,
    confidence: int,
    status: str,
    created_by: str,
    evidence: Optional[str] = None,
    tags: Optional[List[str]] = None,
    related_ids: Optional[List[int]] = None,
) -> MemoryEvent:
    if event_type not in VALID_EVENT_TYPES:
        raise ValidationError(f"Invalid event_type '{event_type}'. Valid: {VALID_EVENT_TYPES}")
    if status not in VALID_STATUSES:
        raise ValidationError(f"Invalid status '{status}'. Valid: {VALID_STATUSES}")
    _validate_confidence(confidence)
    for field, val in (('title', title), ('summary', summary), ('source', source), ('created_by', created_by)):
        if not val or not val.strip():
            raise ValidationError(f"'{field}' must not be empty")

    now = _now()
    tags_json = json.dumps(sorted(tags or []))
    related_ids_json = json.dumps(sorted(related_ids or []))

    with _connect(db_path) as conn:
        cur = conn.execute(
            'INSERT INTO memory_events'
            ' (event_type, title, summary, evidence, source, confidence, status,'
            '  tags_json, related_ids_json, created_by, created_at, updated_at, version)'
            ' VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1)',
            (event_type, title, summary, evidence, source, confidence, status,
             tags_json, related_ids_json, created_by, now, now),
        )
        row = conn.execute('SELECT * FROM memory_events WHERE id = ?', (cur.lastrowid,)).fetchone()
        return MemoryEvent.from_row(row)


def list_memory_events(
    db_path: str,
    event_type: Optional[str] = None,
    status: Optional[str] = None,
    tag: Optional[str] = None,
    limit: int = 50,
) -> List[MemoryEvent]:
    if event_type and event_type not in VALID_EVENT_TYPES:
        raise ValidationError(f"Invalid event_type '{event_type}'")
    if status and status not in VALID_STATUSES:
        raise ValidationError(f"Invalid status '{status}'")

    clauses: List[str] = []
    params: list = []

    if event_type:
        clauses.append('event_type = ?')
        params.append(event_type)
    if status:
        clauses.append('status = ?')
        params.append(status)
    if tag:
        clauses.append("EXISTS (SELECT 1 FROM json_each(tags_json) WHERE value = ?)")
        params.append(tag)

    where = f'WHERE {" AND ".join(clauses)}' if clauses else ''
    params.append(limit)

    with _connect(db_path) as conn:
        rows = conn.execute(
            f'SELECT * FROM memory_events {where} ORDER BY id DESC LIMIT ?',
            params,
        ).fetchall()
        return [MemoryEvent.from_row(r) for r in rows]


def search_memory_events(
    db_path: str,
    query: Optional[str] = None,
    tag: Optional[str] = None,
) -> List[MemoryEvent]:
    if not query and not tag:
        raise ValidationError("At least one of query or tag must be provided")

    clauses: List[str] = []
    params: list = []

    if query:
        pattern = f'%{query}%'
        clauses.append(
            '(title LIKE ? OR summary LIKE ? OR evidence LIKE ? OR source LIKE ? OR tags_json LIKE ?)'
        )
        params.extend([pattern, pattern, pattern, pattern, pattern])
    if tag:
        clauses.append("EXISTS (SELECT 1 FROM json_each(tags_json) WHERE value = ?)")
        params.append(tag)

    where = f'WHERE {" AND ".join(clauses)}'

    with _connect(db_path) as conn:
        rows = conn.execute(
            f'SELECT * FROM memory_events {where} ORDER BY id DESC',
            params,
        ).fetchall()
        return [MemoryEvent.from_row(r) for r in rows]


def get_memory_event(
    db_path: str, memory_id: int
) -> Tuple[MemoryEvent, List[MemoryRevision], List[MemoryLink]]:
    with _connect(db_path) as conn:
        row = conn.execute('SELECT * FROM memory_events WHERE id = ?', (memory_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"Memory event {memory_id} not found")
        event = MemoryEvent.from_row(row)

        revisions = [
            MemoryRevision.from_row(r)
            for r in conn.execute(
                'SELECT * FROM memory_revisions WHERE memory_id = ? ORDER BY id',
                (memory_id,),
            ).fetchall()
        ]
        links = [
            MemoryLink.from_row(r)
            for r in conn.execute(
                'SELECT * FROM memory_links WHERE source_id = ? OR target_id = ? ORDER BY id',
                (memory_id, memory_id),
            ).fetchall()
        ]
        return event, revisions, links


def update_status(
    db_path: str,
    memory_id: int,
    new_status: str,
    reason: str,
    created_by: str,
) -> MemoryEvent:
    if new_status not in VALID_STATUSES:
        raise ValidationError(f"Invalid status '{new_status}'. Valid: {VALID_STATUSES}")
    if not reason or not reason.strip():
        raise ValidationError("'reason' must not be empty")
    if not created_by or not created_by.strip():
        raise ValidationError("'created_by' must not be empty")

    now = _now()
    with _connect(db_path) as conn:
        row = conn.execute('SELECT * FROM memory_events WHERE id = ?', (memory_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"Memory event {memory_id} not found")

        old_status = row['status']
        old_version = row['version']
        new_version = old_version + 1

        old_val = json.dumps({'status': old_status, 'version': old_version})
        new_val = json.dumps({'status': new_status, 'version': new_version})

        conn.execute(
            'UPDATE memory_events SET status = ?, version = ?, updated_at = ? WHERE id = ?',
            (new_status, new_version, now, memory_id),
        )
        conn.execute(
            'INSERT INTO memory_revisions'
            ' (memory_id, old_value_json, new_value_json, reason, created_at, created_by)'
            ' VALUES (?,?,?,?,?,?)',
            (memory_id, old_val, new_val, reason, now, created_by),
        )
        row = conn.execute('SELECT * FROM memory_events WHERE id = ?', (memory_id,)).fetchone()
        return MemoryEvent.from_row(row)


def link_memory_events(
    db_path: str,
    source_id: int,
    target_id: int,
    relationship: str,
) -> MemoryLink:
    if relationship not in VALID_RELATIONSHIPS:
        raise ValidationError(f"Invalid relationship '{relationship}'. Valid: {VALID_RELATIONSHIPS}")

    now = _now()
    with _connect(db_path) as conn:
        for mid in (source_id, target_id):
            if conn.execute('SELECT id FROM memory_events WHERE id = ?', (mid,)).fetchone() is None:
                raise NotFoundError(f"Memory event {mid} not found")

        try:
            cur = conn.execute(
                'INSERT INTO memory_links (source_id, target_id, relationship, created_at)'
                ' VALUES (?,?,?,?)',
                (source_id, target_id, relationship, now),
            )
        except sqlite3.IntegrityError:
            raise ValidationError(
                f"Link ({source_id} -> {target_id} [{relationship}]) already exists"
            )

        row = conn.execute('SELECT * FROM memory_links WHERE id = ?', (cur.lastrowid,)).fetchone()
        return MemoryLink.from_row(row)


def export_memory(db_path: str) -> dict:
    # Governance: retrieval_log and event_embeddings are local derived artifacts.
    # They are excluded from continuity bundles by governance policy.
    # Future portability can be considered explicitly, not silently.
    with _connect(db_path) as conn:
        events = [
            MemoryEvent.from_row(r)
            for r in conn.execute('SELECT * FROM memory_events ORDER BY id').fetchall()
        ]
        revisions = [
            MemoryRevision.from_row(r)
            for r in conn.execute('SELECT * FROM memory_revisions ORDER BY id').fetchall()
        ]
        links = [
            MemoryLink.from_row(r)
            for r in conn.execute('SELECT * FROM memory_links ORDER BY id').fetchall()
        ]
    return {
        'schema_version': 1,
        'memory_events': [e.to_dict() for e in events],
        'memory_revisions': [r.to_dict() for r in revisions],
        'memory_links': [lnk.to_dict() for lnk in links],
    }


def review_memory(
    db_path: str,
    status: Optional[str] = None,
    event_type: Optional[str] = None,
) -> List[MemoryEvent]:
    if status and status not in VALID_STATUSES:
        raise ValidationError(f"Invalid status '{status}'")
    if event_type and event_type not in VALID_EVENT_TYPES:
        raise ValidationError(f"Invalid event_type '{event_type}'")

    clauses: List[str] = []
    params: list = []

    if status:
        clauses.append('status = ?')
        params.append(status)
    else:
        placeholders = ','.join('?' * len(REVIEW_STATUSES))
        clauses.append(f'status IN ({placeholders})')
        params.extend(REVIEW_STATUSES)

    if event_type:
        clauses.append('event_type = ?')
        params.append(event_type)

    where = f'WHERE {" AND ".join(clauses)}'

    with _connect(db_path) as conn:
        rows = conn.execute(
            f'SELECT * FROM memory_events {where} ORDER BY id DESC',
            params,
        ).fetchall()
        return [MemoryEvent.from_row(r) for r in rows]

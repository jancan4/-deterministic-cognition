"""
Ingestion run ledger: persistent provenance for every ingest-file execution.

Each time ingest-file runs, one IngestionRun record is written to the
`ingestion_runs` table. The record links:

  source_documents.source_id
       ↓
  ingestion_runs.run_id   (this module)
       ↓
  memory_events.id × N   (committed_memory_ids_json)

This makes the full provenance chain inspectable without querying three tables:
  "which file produced which memory events, and exactly what state was the file
   in at the time?"

Run lifecycle:
  candidate_generated  — candidates extracted; --commit not passed
  committed            — candidates committed to memory_events
  failed               — an exception occurred during ingestion

run_id derivation:
  sha256( source_id + '\x00' + source_checksum + '\x00' + started_at )[:16]

  Deterministic per (source version, wall-clock second). Two runs on the same
  source within the same second would collide; in practice this is a non-issue
  for a CLI tool. Sub-second precision could be added if needed.

No write is issued against any table other than ingestion_runs from this module.
"""
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional

VALID_RUN_STATUSES = ('candidate_generated', 'committed', 'failed')

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ingestion_runs (
    id                        INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id                    TEXT    NOT NULL UNIQUE,
    source_id                 TEXT    NOT NULL,
    source_checksum_sha256    TEXT    NOT NULL,
    source_version            INTEGER NOT NULL,
    parser_version            TEXT    NOT NULL,
    extractor_version         TEXT    NOT NULL,
    chunk_count               INTEGER NOT NULL DEFAULT 0,
    candidate_count           INTEGER NOT NULL DEFAULT 0,
    committed_count           INTEGER NOT NULL DEFAULT 0,
    committed_memory_ids_json TEXT    NOT NULL DEFAULT '[]',
    status                    TEXT    NOT NULL CHECK (status IN (
                                  'candidate_generated','committed','failed'
                              )),
    started_at                TEXT    NOT NULL,
    completed_at              TEXT,
    metadata_json             TEXT    NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_runs_source_id
    ON ingestion_runs(source_id);
CREATE INDEX IF NOT EXISTS idx_runs_status
    ON ingestion_runs(status);
CREATE INDEX IF NOT EXISTS idx_runs_started_at
    ON ingestion_runs(started_at);
"""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA foreign_keys=ON')
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)


def _derive_run_id(source_id: str, source_checksum: str, started_at: str) -> str:
    """Deterministic run_id: sha256(source_id + NUL + checksum + NUL + started_at)[:16]."""
    raw = f"{source_id}\x00{source_checksum}\x00{started_at}".encode('utf-8')
    return hashlib.sha256(raw).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Public data model
# ---------------------------------------------------------------------------

@dataclass
class IngestionRun:
    run_id: str
    source_id: str
    source_checksum_sha256: str
    source_version: int
    parser_version: str
    extractor_version: str
    chunk_count: int
    candidate_count: int
    committed_count: int
    committed_memory_ids: List[int]
    status: str
    started_at: str
    completed_at: Optional[str]
    metadata: dict

    def to_dict(self) -> dict:
        return {
            'run_id': self.run_id,
            'source_id': self.source_id,
            'source_checksum_sha256': self.source_checksum_sha256,
            'source_version': self.source_version,
            'parser_version': self.parser_version,
            'extractor_version': self.extractor_version,
            'chunk_count': self.chunk_count,
            'candidate_count': self.candidate_count,
            'committed_count': self.committed_count,
            'committed_memory_ids': list(self.committed_memory_ids),
            'status': self.status,
            'started_at': self.started_at,
            'completed_at': self.completed_at,
            'metadata': dict(self.metadata),
        }

    @classmethod
    def from_row(cls, row) -> 'IngestionRun':
        return cls(
            run_id=row['run_id'],
            source_id=row['source_id'],
            source_checksum_sha256=row['source_checksum_sha256'],
            source_version=row['source_version'],
            parser_version=row['parser_version'],
            extractor_version=row['extractor_version'],
            chunk_count=row['chunk_count'],
            candidate_count=row['candidate_count'],
            committed_count=row['committed_count'],
            committed_memory_ids=json.loads(row['committed_memory_ids_json'] or '[]'),
            status=row['status'],
            started_at=row['started_at'],
            completed_at=row['completed_at'],
            metadata=json.loads(row['metadata_json'] or '{}'),
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def init_run_ledger(db_path: str) -> None:
    """
    Create ingestion_runs table and indexes if they do not exist.
    Safe to call multiple times (idempotent).
    """
    with _connect(db_path) as conn:
        _ensure_schema(conn)


def record_run(
    db_path: str,
    source_id: str,
    source_checksum: str,
    source_version: int,
    parser_version: str,
    extractor_version: str,
    chunk_count: int,
    candidate_count: int,
    committed_count: int,
    committed_memory_ids: List[int],
    status: str,
    started_at: str,
    completed_at: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> IngestionRun:
    """
    Write one ingestion run record to the ledger.

    run_id is derived deterministically from (source_id, source_checksum,
    started_at). All other fields are stored as-is.

    Returns the persisted IngestionRun.
    """
    if status not in VALID_RUN_STATUSES:
        raise ValueError(
            f"Invalid run status {status!r}. Must be one of: {VALID_RUN_STATUSES}"
        )

    run_id = _derive_run_id(source_id, source_checksum, started_at)
    committed_ids_json = json.dumps(sorted(committed_memory_ids))
    metadata_json = json.dumps(metadata or {}, sort_keys=True)

    with _connect(db_path) as conn:
        _ensure_schema(conn)
        conn.execute(
            """
            INSERT OR REPLACE INTO ingestion_runs (
                run_id, source_id, source_checksum_sha256, source_version,
                parser_version, extractor_version,
                chunk_count, candidate_count, committed_count,
                committed_memory_ids_json, status,
                started_at, completed_at, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id, source_id, source_checksum, source_version,
                parser_version, extractor_version,
                chunk_count, candidate_count, committed_count,
                committed_ids_json, status,
                started_at, completed_at, metadata_json,
            ),
        )
        row = conn.execute(
            "SELECT * FROM ingestion_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        return IngestionRun.from_row(row)


def get_run(db_path: str, run_id: str) -> Optional[IngestionRun]:
    """Return the IngestionRun with the given run_id, or None."""
    with _connect(db_path) as conn:
        _ensure_schema(conn)
        row = conn.execute(
            "SELECT * FROM ingestion_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        return IngestionRun.from_row(row) if row else None


def list_runs(
    db_path: str,
    source_id: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 100,
) -> List[IngestionRun]:
    """
    List ingestion runs ordered by started_at DESC.

    Optionally filter by source_id and/or status.
    """
    if status is not None and status not in VALID_RUN_STATUSES:
        raise ValueError(
            f"Invalid run status {status!r}. Must be one of: {VALID_RUN_STATUSES}"
        )

    clauses: List[str] = []
    params: list = []

    if source_id:
        clauses.append("source_id = ?")
        params.append(source_id)
    if status:
        clauses.append("status = ?")
        params.append(status)

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)

    with _connect(db_path) as conn:
        _ensure_schema(conn)
        rows = conn.execute(
            f"SELECT * FROM ingestion_runs {where} ORDER BY started_at DESC LIMIT ?",
            params,
        ).fetchall()
        return [IngestionRun.from_row(r) for r in rows]


def make_started_at() -> str:
    """Return the current UTC timestamp for use as started_at."""
    return _now()

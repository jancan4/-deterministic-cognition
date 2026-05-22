"""
Source document registry: persistent provenance store for ingested files.

All registry state lives in a `source_documents` table inside the operator's
memory SQLite database (same file, separate table namespace). The table is
created on first use — no explicit migration step is required.

Registration is idempotent:
  - Same path + same checksum → return the existing active record unchanged.
  - Same path + different checksum → supersede the previous active record and
    create a new version (version = prev.version + 1).
  - New path → create version 1 record.

source_id derivation:
  sha256( abs_path + '\x00' + checksum_sha256 )[:16]

  Deterministic: same (path, content) pair always produces the same source_id.
  Changes when file content changes, preserving full version lineage.

All writes use PRAGMA journal_mode=WAL and PRAGMA foreign_keys=ON.
"""
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from .checksums import compute_file_checksum
from .models import (
    VALID_AUTHORITY_TIERS,
    VALID_SOURCE_STATUSES,
    VALID_SOURCE_TYPES,
    SourceDocument,
    SourceValidationError,
    _validate_authority_tier,
    _validate_source_status,
    _validate_source_type,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS source_documents (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id       TEXT    NOT NULL UNIQUE,
    path            TEXT    NOT NULL,
    filename        TEXT    NOT NULL,
    checksum_sha256 TEXT    NOT NULL,
    size_bytes      INTEGER NOT NULL,
    modified_time   TEXT    NOT NULL,
    registered_at   TEXT    NOT NULL,
    source_type     TEXT    NOT NULL CHECK (source_type IN (
                        'doctrine','research_note','article','transcript',
                        'implementation_brief','architecture_doc',
                        'external_reference','unknown'
                    )),
    authority_tier  TEXT    NOT NULL CHECK (authority_tier IN (
                        'authoritative','high','medium','low','unknown'
                    )),
    status          TEXT    NOT NULL DEFAULT 'active' CHECK (status IN (
                        'active','superseded','deprecated','rejected','archived'
                    )),
    metadata_json   TEXT    NOT NULL DEFAULT '{}',
    version         INTEGER NOT NULL DEFAULT 1
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_src_path_checksum
    ON source_documents(path, checksum_sha256);
CREATE INDEX IF NOT EXISTS idx_src_path
    ON source_documents(path);
CREATE INDEX IF NOT EXISTS idx_src_checksum
    ON source_documents(checksum_sha256);
CREATE INDEX IF NOT EXISTS idx_src_status
    ON source_documents(status);
CREATE INDEX IF NOT EXISTS idx_src_type
    ON source_documents(source_type);
"""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _mtime_iso(path: str) -> str:
    mtime = Path(path).stat().st_mtime
    return datetime.fromtimestamp(mtime, tz=timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _compute_source_id(abs_path: str, checksum: str) -> str:
    """Deterministic source_id: sha256(abs_path + NUL + checksum)[:16]."""
    digest_input = f"{abs_path}\x00{checksum}".encode('utf-8')
    return hashlib.sha256(digest_input).hexdigest()[:16]


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA foreign_keys=ON')
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def init_registry(db_path: str) -> None:
    """
    Create source_documents table and indexes if they do not exist.

    Safe to call multiple times (idempotent). Compatible with an existing
    memory.db — only adds the source_documents table.
    """
    with _connect(db_path) as conn:
        _ensure_schema(conn)


def register_source(
    db_path: str,
    path: str,
    source_type: str = 'unknown',
    authority_tier: str = 'unknown',
    metadata: Optional[dict] = None,
) -> SourceDocument:
    """
    Register a file in the source document registry.

    Idempotency rules:
      - Same path + same checksum → return existing active record.
      - Same path + different checksum → supersede active record, create new
        version (version = prev.version + 1, status='superseded' on old).
      - New path → create version 1 record.

    Validates source_type and authority_tier before writing.
    Raises FileNotFoundError if the file does not exist.
    Raises SourceValidationError on invalid enum values.
    """
    _validate_source_type(source_type)
    _validate_authority_tier(authority_tier)

    abs_path = str(Path(path).resolve())
    checksum = compute_file_checksum(abs_path)
    size_bytes = Path(abs_path).stat().st_size
    modified_time = _mtime_iso(abs_path)
    filename = Path(abs_path).name
    source_id = _compute_source_id(abs_path, checksum)
    metadata_json = json.dumps(metadata or {}, sort_keys=True)
    now = _now()

    with _connect(db_path) as conn:
        _ensure_schema(conn)

        # --- Idempotent: same path + same checksum already registered ---
        existing = conn.execute(
            "SELECT * FROM source_documents WHERE path = ? AND checksum_sha256 = ?",
            (abs_path, checksum),
        ).fetchone()
        if existing:
            return SourceDocument.from_row(existing)

        # --- Same path, different checksum: supersede previous active record ---
        prev_version = 0
        active_rows = conn.execute(
            "SELECT * FROM source_documents WHERE path = ? AND status = 'active' ORDER BY version DESC",
            (abs_path,),
        ).fetchall()
        for row in active_rows:
            conn.execute(
                "UPDATE source_documents SET status = 'superseded' WHERE source_id = ?",
                (row['source_id'],),
            )
            prev_version = max(prev_version, row['version'])

        version = prev_version + 1

        conn.execute(
            """
            INSERT INTO source_documents (
                source_id, path, filename, checksum_sha256,
                size_bytes, modified_time, registered_at,
                source_type, authority_tier, status,
                metadata_json, version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)
            """,
            (
                source_id, abs_path, filename, checksum,
                size_bytes, modified_time, now,
                source_type, authority_tier,
                metadata_json, version,
            ),
        )
        row = conn.execute(
            "SELECT * FROM source_documents WHERE source_id = ?", (source_id,)
        ).fetchone()
        return SourceDocument.from_row(row)


def get_source_by_id(db_path: str, source_id: str) -> Optional[SourceDocument]:
    """Return the SourceDocument with the given source_id, or None."""
    with _connect(db_path) as conn:
        _ensure_schema(conn)
        row = conn.execute(
            "SELECT * FROM source_documents WHERE source_id = ?", (source_id,)
        ).fetchone()
        return SourceDocument.from_row(row) if row else None


def get_sources_by_path(db_path: str, path: str) -> List[SourceDocument]:
    """
    Return all SourceDocument records for the given path, ordered by version ASC.

    Returns all versions (active and superseded). Use the last entry (highest
    version) to get the current state.
    """
    abs_path = str(Path(path).resolve())
    with _connect(db_path) as conn:
        _ensure_schema(conn)
        rows = conn.execute(
            "SELECT * FROM source_documents WHERE path = ? ORDER BY version ASC",
            (abs_path,),
        ).fetchall()
        return [SourceDocument.from_row(r) for r in rows]


def get_sources_by_checksum(db_path: str, checksum: str) -> List[SourceDocument]:
    """Return all SourceDocument records with the given SHA-256 checksum."""
    with _connect(db_path) as conn:
        _ensure_schema(conn)
        rows = conn.execute(
            "SELECT * FROM source_documents WHERE checksum_sha256 = ? ORDER BY registered_at ASC",
            (checksum,),
        ).fetchall()
        return [SourceDocument.from_row(r) for r in rows]


def list_sources(
    db_path: str,
    status: Optional[str] = None,
    source_type: Optional[str] = None,
    limit: int = 100,
) -> List[SourceDocument]:
    """
    List source documents, ordered by registered_at DESC.

    Optionally filter by status and/or source_type.
    """
    if status is not None:
        _validate_source_status(status)
    if source_type is not None:
        _validate_source_type(source_type)

    clauses: List[str] = []
    params: list = []

    if status:
        clauses.append("status = ?")
        params.append(status)
    if source_type:
        clauses.append("source_type = ?")
        params.append(source_type)

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)

    with _connect(db_path) as conn:
        _ensure_schema(conn)
        rows = conn.execute(
            f"SELECT * FROM source_documents {where} ORDER BY registered_at DESC LIMIT ?",
            params,
        ).fetchall()
        return [SourceDocument.from_row(r) for r in rows]

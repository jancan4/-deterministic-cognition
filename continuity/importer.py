"""
Continuity bundle importer.

import_bundle() reads a bundle dict (as produced by export_bundle() and
validated by validate_bundle()), then writes its records into the target
database — or reports what would be written in dry-run mode.

Collision semantics
-------------------
A "collision" is a record that already exists with a DIFFERENT payload.
A "skip" is a record that already exists with an IDENTICAL payload (safe to
ignore — content-addressed identity means the record is already there).

If any collision is detected the entire import is refused atomically.
No partial writes are made.

Write order (dependency-safe)
------------------------------
1. source_documents  — no foreign keys
2. memory_events     — may reference source paths (non-FK, just a string)
3. ingestion_runs    — references source_id (non-FK in schema, but logical dep)

Memory events are inserted via direct SQL with explicit id preservation,
bypassing memory.service.add_memory_event(). This is intentional:
  - The service generates new ids; import must preserve existing ids.
  - import_bundle() is the single governed entry-point for cross-system
    record migration — it is not a normal write path.

After the import, SQLite's autoincrement sequence is bumped so that future
service-generated ids never collide with imported ids.
"""
import json
import sqlite3
from typing import List, Optional, Tuple

from .manifest import BundleValidationError, validate_bundle
from .models import ImportCollision, ImportResult


# ---------------------------------------------------------------------------
# Schema initialisation
# ---------------------------------------------------------------------------

def _ensure_schemas(db_path: str) -> None:
    from memory import service as mem_service
    from sources.registry import init_registry
    from ingestion.runs import init_run_ledger

    mem_service.init_db(db_path)
    init_registry(db_path)
    init_run_ledger(db_path)


# ---------------------------------------------------------------------------
# Connection helper
# ---------------------------------------------------------------------------

def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA foreign_keys=ON')
    return conn


# ---------------------------------------------------------------------------
# Collision detection helpers
# ---------------------------------------------------------------------------

def _check_memory_events(
    conn: sqlite3.Connection,
    events: List[dict],
) -> Tuple[List[ImportCollision], List[dict], List[dict]]:
    """
    Returns (collisions, to_insert, to_skip).

    Collision: id exists but (event_type, title, source) differs.
    Skip:      id exists and payload matches.
    Insert:    id not found.
    """
    collisions: List[ImportCollision] = []
    to_insert: List[dict] = []
    to_skip: List[dict] = []

    for ev in events:
        row = conn.execute(
            "SELECT event_type, title, source FROM memory_events WHERE id = ?",
            (ev['id'],),
        ).fetchone()

        if row is None:
            to_insert.append(ev)
        elif (
            row['event_type'] == ev['event_type']
            and row['title'] == ev['title']
            and row['source'] == ev['source']
        ):
            to_skip.append(ev)
        else:
            collisions.append(ImportCollision(
                record_type='memory_event',
                identifier=str(ev['id']),
                reason=(
                    f"id={ev['id']} exists with different event_type/title/source: "
                    f"existing=({row['event_type']!r}, {row['title']!r}, {row['source']!r}) "
                    f"incoming=({ev['event_type']!r}, {ev['title']!r}, {ev['source']!r})"
                ),
            ))

    return collisions, to_insert, to_skip


def _check_source_documents(
    conn: sqlite3.Connection,
    docs: List[dict],
) -> Tuple[List[ImportCollision], List[dict], List[dict]]:
    """
    source_id is content-addressed (sha256 of path+checksum).
    Same source_id → identical content → safe skip.
    Different source_id but same (path, version) → collision.
    """
    collisions: List[ImportCollision] = []
    to_insert: List[dict] = []
    to_skip: List[dict] = []

    for doc in docs:
        # Exact match by source_id (content-addressed)
        by_id = conn.execute(
            "SELECT source_id FROM source_documents WHERE source_id = ?",
            (doc['source_id'],),
        ).fetchone()

        if by_id is not None:
            to_skip.append(doc)
            continue

        # Check for (path, version) collision with a different source_id
        conflict = conn.execute(
            "SELECT source_id FROM source_documents WHERE path = ? AND version = ?",
            (doc['path'], doc['version']),
        ).fetchone()

        if conflict is not None:
            collisions.append(ImportCollision(
                record_type='source_document',
                identifier=doc['source_id'],
                reason=(
                    f"path={doc['path']!r} version={doc['version']} already exists "
                    f"with source_id={conflict['source_id']!r} (incoming: {doc['source_id']!r})"
                ),
            ))
        else:
            to_insert.append(doc)

    return collisions, to_insert, to_skip


def _check_ingestion_runs(
    conn: sqlite3.Connection,
    runs: List[dict],
) -> Tuple[List[ImportCollision], List[dict], List[dict]]:
    """
    run_id is deterministic (sha256 of source_id+checksum+started_at).
    Same run_id → identical provenance → safe skip.
    """
    collisions: List[ImportCollision] = []
    to_insert: List[dict] = []
    to_skip: List[dict] = []

    for run in runs:
        row = conn.execute(
            "SELECT run_id, source_id FROM ingestion_runs WHERE run_id = ?",
            (run['run_id'],),
        ).fetchone()

        if row is None:
            to_insert.append(run)
        else:
            to_skip.append(run)

    return collisions, to_insert, to_skip


# ---------------------------------------------------------------------------
# Write helpers (only called after collision check passes)
# ---------------------------------------------------------------------------

def _insert_source_documents(conn: sqlite3.Connection, docs: List[dict]) -> int:
    count = 0
    for doc in docs:
        conn.execute(
            """
            INSERT INTO source_documents (
                source_id, path, filename, checksum_sha256, size_bytes,
                modified_time, registered_at, source_type, authority_tier,
                status, metadata_json, version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                doc['source_id'],
                doc['path'],
                doc['filename'],
                doc['checksum_sha256'],
                doc['size_bytes'],
                doc['modified_time'],
                doc['registered_at'],
                doc['source_type'],
                doc['authority_tier'],
                doc['status'],
                json.dumps(doc.get('metadata', {}), sort_keys=True),
                doc['version'],
            ),
        )
        count += 1
    return count


def _insert_memory_events(conn: sqlite3.Connection, events: List[dict]) -> int:
    count = 0
    for ev in events:
        conn.execute(
            """
            INSERT INTO memory_events (
                id, event_type, title, summary, evidence, source,
                confidence, status, tags_json, related_ids_json,
                created_by, created_at, updated_at, version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ev['id'],
                ev['event_type'],
                ev['title'],
                ev['summary'],
                ev.get('evidence', ''),
                ev['source'],
                ev['confidence'],
                ev['status'],
                json.dumps(ev.get('tags', []), sort_keys=False),
                json.dumps(ev.get('related_ids', []), sort_keys=False),
                ev.get('created_by', ''),
                ev.get('created_at', ''),
                ev.get('updated_at', ''),
                ev.get('version', 1),
            ),
        )
        count += 1

    # Bump sqlite_sequence so future auto-inserts don't collide with imported ids
    if events:
        max_id = max(ev['id'] for ev in events)
        rows_updated = conn.execute(
            "UPDATE sqlite_sequence SET seq = MAX(seq, ?) WHERE name = 'memory_events'",
            (max_id,),
        ).rowcount
        if rows_updated == 0:
            conn.execute(
                "INSERT OR IGNORE INTO sqlite_sequence (name, seq) VALUES ('memory_events', ?)",
                (max_id,),
            )

    return count


def _insert_ingestion_runs(conn: sqlite3.Connection, runs: List[dict]) -> int:
    count = 0
    for run in runs:
        conn.execute(
            """
            INSERT INTO ingestion_runs (
                run_id, source_id, source_checksum_sha256, source_version,
                parser_version, extractor_version,
                chunk_count, candidate_count, committed_count,
                committed_memory_ids_json, status,
                started_at, completed_at, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run['run_id'],
                run['source_id'],
                run['source_checksum_sha256'],
                run['source_version'],
                run['parser_version'],
                run['extractor_version'],
                run['chunk_count'],
                run['candidate_count'],
                run['committed_count'],
                json.dumps(run.get('committed_memory_ids', [])),
                run['status'],
                run['started_at'],
                run.get('completed_at'),
                json.dumps(run.get('metadata', {}), sort_keys=True),
            ),
        )
        count += 1
    return count


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def import_bundle(
    bundle_dict: dict,
    db_path: str,
    dry_run: bool = False,
) -> ImportResult:
    """
    Import a continuity bundle into the target database.

    Steps:
      1. Validate bundle structure and checksum (raises BundleValidationError).
      2. Ensure all required schemas exist in target db.
      3. Planning pass — classify every record as insert / skip / collision.
      4. If dry_run: return ImportResult with planned counts; no writes.
      5. If collisions: return ImportResult with collisions listed; no writes.
      6. Atomic write: source_documents → memory_events → ingestion_runs.

    Raises:
        BundleValidationError — bundle is structurally invalid or checksum fails.
    """
    validate_bundle(bundle_dict)
    _ensure_schemas(db_path)

    events = bundle_dict.get('memory_events', [])
    docs = bundle_dict.get('source_documents', [])
    runs = bundle_dict.get('ingestion_runs', [])

    with _connect(db_path) as conn:
        ev_cols, ev_insert, ev_skip = _check_memory_events(conn, events)
        doc_cols, doc_insert, doc_skip = _check_source_documents(conn, docs)
        run_cols, run_insert, run_skip = _check_ingestion_runs(conn, runs)

    all_collisions = ev_cols + doc_cols + run_cols

    if dry_run or all_collisions:
        return ImportResult(
            imported_memory_events=len(ev_insert),
            imported_source_documents=len(doc_insert),
            imported_ingestion_runs=len(run_insert),
            skipped_memory_events=len(ev_skip),
            skipped_source_documents=len(doc_skip),
            skipped_ingestion_runs=len(run_skip),
            collisions=all_collisions,
            dry_run=dry_run,
        )

    # Atomic write — all within a single connection/transaction
    with _connect(db_path) as conn:
        imported_docs = _insert_source_documents(conn, doc_insert)
        imported_events = _insert_memory_events(conn, ev_insert)
        imported_runs = _insert_ingestion_runs(conn, run_insert)

    return ImportResult(
        imported_memory_events=imported_events,
        imported_source_documents=imported_docs,
        imported_ingestion_runs=imported_runs,
        skipped_memory_events=len(ev_skip),
        skipped_source_documents=len(doc_skip),
        skipped_ingestion_runs=len(run_skip),
        collisions=[],
        dry_run=False,
    )

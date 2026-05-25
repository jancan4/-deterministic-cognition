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
1. source_documents           — no foreign keys
2. memory_events              — may reference source paths (non-FK, just a string)
3. ingestion_runs             — references source_id (non-FK in schema, but logical dep)
4. semantic_execution_runs    — references source_id (logical dep, same as ingestion_runs)
5. semantic_candidate_events  — promoted_memory_id must reference a memory_events row

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
    from semantic.ledger import init_ledger

    mem_service.init_db(db_path)
    init_registry(db_path)
    init_run_ledger(db_path)
    init_ledger(db_path)


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


def _check_semantic_runs(
    conn: sqlite3.Connection,
    runs: List[dict],
) -> Tuple[List[ImportCollision], List[dict], List[dict]]:
    """
    run_id is deterministic (content-addressed). Same run_id → safe skip.
    """
    collisions: List[ImportCollision] = []
    to_insert: List[dict] = []
    to_skip: List[dict] = []

    for run in runs:
        row = conn.execute(
            "SELECT run_id FROM semantic_execution_runs WHERE run_id = ?",
            (run['run_id'],),
        ).fetchone()

        if row is None:
            to_insert.append(run)
        else:
            to_skip.append(run)

    return collisions, to_insert, to_skip


def _check_semantic_candidates(
    conn: sqlite3.Connection,
    candidates: List[dict],
    incoming_memory_event_ids: set,
) -> Tuple[List[ImportCollision], List[dict], List[dict]]:
    """
    candidate_id is deterministic. Same candidate_id → safe skip.

    Validates that promoted_memory_id references a memory_events row that
    either already exists in the target DB or is being imported in this bundle.
    """
    collisions: List[ImportCollision] = []
    to_insert: List[dict] = []
    to_skip: List[dict] = []

    for cand in candidates:
        row = conn.execute(
            "SELECT candidate_id FROM semantic_candidate_events WHERE candidate_id = ?",
            (cand['candidate_id'],),
        ).fetchone()

        if row is not None:
            to_skip.append(cand)
            continue

        # Validate promoted_memory_id reference before inserting
        pmid = cand.get('promoted_memory_id')
        if pmid is not None:
            mem_row = conn.execute(
                "SELECT id FROM memory_events WHERE id = ?", (pmid,)
            ).fetchone()
            if mem_row is None and pmid not in incoming_memory_event_ids:
                collisions.append(ImportCollision(
                    record_type='semantic_candidate_event',
                    identifier=cand['candidate_id'],
                    reason=(
                        f"promoted_memory_id={pmid} does not reference any "
                        f"memory_event in the target database or this bundle"
                    ),
                ))
                continue

        to_insert.append(cand)

    return collisions, to_insert, to_skip


def _insert_semantic_runs(conn: sqlite3.Connection, runs: List[dict]) -> int:
    count = 0
    for run in runs:
        conn.execute(
            """
            INSERT INTO semantic_execution_runs (
                run_id, task_id, task_type, adapter_name, adapter_version,
                input_hash, input_text, source_id, source_span_json,
                execution_policy_json, model_metadata_json, raw_output_json,
                normalized_result_json, candidate_count, promoted_count,
                status, started_at, completed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run['run_id'],
                run['task_id'],
                run['task_type'],
                run['adapter_name'],
                run['adapter_version'],
                run['input_hash'],
                run['input_text'],
                run.get('source_id'),
                json.dumps(run['source_span'], sort_keys=True) if run.get('source_span') else None,
                json.dumps(run.get('execution_policy', {}), sort_keys=True),
                json.dumps(run.get('model_metadata', {}), sort_keys=True),
                json.dumps(run['raw_output'], sort_keys=True) if run.get('raw_output') is not None else None,
                json.dumps(run.get('normalized_result', {}), sort_keys=True),
                run['candidate_count'],
                run['promoted_count'],
                run['status'],
                run['started_at'],
                run['completed_at'],
            ),
        )
        count += 1
    return count


def _insert_semantic_candidates(conn: sqlite3.Connection, candidates: List[dict]) -> int:
    count = 0
    for cand in candidates:
        conn.execute(
            """
            INSERT INTO semantic_candidate_events (
                candidate_id, semantic_run_id, candidate_index,
                event_type, title, summary, evidence, source,
                confidence, source_id, source_span_json, extraction_method,
                provenance_json, tags_json, status, promoted_memory_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                cand['candidate_id'],
                cand['semantic_run_id'],
                cand['candidate_index'],
                cand['event_type'],
                cand['title'],
                cand['summary'],
                cand.get('evidence'),
                cand['source'],
                cand['confidence'],
                cand.get('source_id'),
                json.dumps(cand['source_span'], sort_keys=True) if cand.get('source_span') else None,
                cand['extraction_method'],
                json.dumps(cand.get('provenance', {}), sort_keys=True),
                json.dumps(cand.get('tags', []), sort_keys=False),
                cand['status'],
                cand.get('promoted_memory_id'),
                cand['created_at'],
            ),
        )
        count += 1
    return count


# ---------------------------------------------------------------------------
# Warning detection helpers
# ---------------------------------------------------------------------------

def _detect_import_warnings(
    bundle_dict: dict,
    conn: sqlite3.Connection,
    events_to_import: List[dict],
) -> List[str]:
    """
    Return ordered list of import warnings. Warnings do not block the import.

    Detects:
      1. Schema version mismatch between bundle and target DB.
      2. Compression-derived proposed exclusion disclosure.
      3. Dangling compression artifact provenance in events being imported.
    """
    warnings: List[str] = []
    manifest = bundle_dict.get('manifest', {})

    # 1. Schema version mismatch
    exported_schema = manifest.get('exported_db_schema_version')
    if exported_schema is not None:
        try:
            row = conn.execute(
                'SELECT version FROM memory_schema_version'
            ).fetchone()
            if row is not None and int(row[0]) != int(exported_schema):
                warnings.append(
                    f"Bundle was exported from schema v{exported_schema}; "
                    f"target is schema v{row[0]}. "
                    f"Review column additions before proceeding."
                )
        except Exception:
            pass

    # 2. Compression-derived proposed exclusion disclosure
    if (
        manifest.get('compression_derived_proposed_excluded') is True
        and manifest.get('compression_derived_proposed_excluded_count', 0) > 0
    ):
        n = manifest['compression_derived_proposed_excluded_count']
        warnings.append(
            f"Bundle excludes {n} compression-derived proposed event(s) "
            f"per Phase 6D policy. Re-export with "
            f"include_compression_derived_proposed=True to include them."
        )

    # 3. Dangling compression artifact provenance
    has_artifacts_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='compression_artifacts'"
    ).fetchone() is not None

    for ev in events_to_import:
        src = ev.get('source', '')
        if not src.startswith('compression_artifact:'):
            continue
        if not has_artifacts_table:
            warnings.append(
                f"Event id={ev['id']} source={src!r}: compression artifact table "
                f"absent in target — provenance preserved as string, "
                f"artifact not reconstructable."
            )
        else:
            try:
                artifact_id = int(src.split(':', 1)[1])
                row = conn.execute(
                    'SELECT id FROM compression_artifacts WHERE id = ?',
                    (artifact_id,),
                ).fetchone()
                if row is None:
                    warnings.append(
                        f"Event id={ev['id']} source={src!r}: compression artifact "
                        f"not present in target — provenance preserved as string, "
                        f"artifact not reconstructable."
                    )
            except (ValueError, IndexError):
                pass

    return warnings


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
    sem_runs = bundle_dict.get('semantic_execution_runs', [])
    sem_candidates = bundle_dict.get('semantic_candidate_events', [])

    with _connect(db_path) as conn:
        ev_cols, ev_insert, ev_skip = _check_memory_events(conn, events)
        doc_cols, doc_insert, doc_skip = _check_source_documents(conn, docs)
        run_cols, run_insert, run_skip = _check_ingestion_runs(conn, runs)
        sem_run_cols, sem_run_insert, sem_run_skip = _check_semantic_runs(conn, sem_runs)
        # Candidates may reference memory events being imported in this bundle
        incoming_ev_ids = {ev['id'] for ev in ev_insert}
        sem_cand_cols, sem_cand_insert, sem_cand_skip = _check_semantic_candidates(
            conn, sem_candidates, incoming_ev_ids
        )
        # Warnings: schema mismatch, compression provenance, policy disclosure
        import_warnings = _detect_import_warnings(bundle_dict, conn, ev_insert)

    all_collisions = ev_cols + doc_cols + run_cols + sem_run_cols + sem_cand_cols

    if dry_run or all_collisions:
        return ImportResult(
            imported_memory_events=len(ev_insert),
            imported_source_documents=len(doc_insert),
            imported_ingestion_runs=len(run_insert),
            imported_semantic_execution_runs=len(sem_run_insert),
            imported_semantic_candidate_events=len(sem_cand_insert),
            skipped_memory_events=len(ev_skip),
            skipped_source_documents=len(doc_skip),
            skipped_ingestion_runs=len(run_skip),
            skipped_semantic_execution_runs=len(sem_run_skip),
            skipped_semantic_candidate_events=len(sem_cand_skip),
            collisions=all_collisions,
            dry_run=dry_run,
            warnings=import_warnings,
        )

    # Atomic write — all within a single connection/transaction
    with _connect(db_path) as conn:
        imported_docs = _insert_source_documents(conn, doc_insert)
        imported_events = _insert_memory_events(conn, ev_insert)
        imported_runs = _insert_ingestion_runs(conn, run_insert)
        imported_sem_runs = _insert_semantic_runs(conn, sem_run_insert)
        imported_sem_cands = _insert_semantic_candidates(conn, sem_cand_insert)

    return ImportResult(
        imported_memory_events=imported_events,
        imported_source_documents=imported_docs,
        imported_ingestion_runs=imported_runs,
        imported_semantic_execution_runs=imported_sem_runs,
        imported_semantic_candidate_events=imported_sem_cands,
        skipped_memory_events=len(ev_skip),
        skipped_source_documents=len(doc_skip),
        skipped_ingestion_runs=len(run_skip),
        skipped_semantic_execution_runs=len(sem_run_skip),
        skipped_semantic_candidate_events=len(sem_cand_skip),
        collisions=[],
        dry_run=False,
        warnings=import_warnings,
    )

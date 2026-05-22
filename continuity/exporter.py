"""
Continuity bundle exporter.

export_bundle() reads from the memory database and assembles a portable,
deterministic bundle. It is strictly read-only — it issues no INSERT, UPDATE,
or DELETE against any database.

Ordering guarantees (required for checksum stability):
  memory_events    → ORDER BY id ASC
  source_documents → ORDER BY path ASC, version ASC
  ingestion_runs   → ORDER BY started_at ASC, run_id ASC
  workflow_references → ORDER BY execution_id ASC
"""
import json
import sqlite3
from datetime import datetime, timezone
from typing import List, Optional

from .manifest import build_manifest
from .models import BUNDLE_SCHEMA_VERSION, ExportFilter


def _now() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    return conn


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


# ---------------------------------------------------------------------------
# Section fetchers (all read-only)
# ---------------------------------------------------------------------------

def _fetch_memory_events(
    conn: sqlite3.Connection,
    export_filter: Optional[ExportFilter],
) -> List[dict]:
    if not _table_exists(conn, 'memory_events'):
        return []

    clauses: List[str] = []
    params: list = []

    if export_filter:
        if export_filter.unresolved_only:
            clauses.append("status IN ('unresolved', 'proposed')")
        for tag in export_filter.tags:
            clauses.append(
                "EXISTS (SELECT 1 FROM json_each(tags_json) WHERE value = ?)"
            )
            params.append(tag)
        if export_filter.since:
            clauses.append("created_at >= ?")
            params.append(export_filter.since)
        if export_filter.until:
            clauses.append("created_at <= ?")
            params.append(export_filter.until)
        if export_filter.source_ids:
            source_paths = _paths_for_source_ids(conn, export_filter.source_ids)
            if not source_paths:
                return []
            ph = ','.join('?' * len(source_paths))
            clauses.append(f"source IN ({ph})")
            params.extend(source_paths)

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        f"SELECT * FROM memory_events {where} ORDER BY id ASC", params
    ).fetchall()
    return [_me_row_to_dict(r) for r in rows]


def _me_row_to_dict(row) -> dict:
    return {
        'id': row['id'],
        'event_type': row['event_type'],
        'title': row['title'],
        'summary': row['summary'],
        'evidence': row['evidence'],
        'source': row['source'],
        'confidence': row['confidence'],
        'status': row['status'],
        'tags': json.loads(row['tags_json'] or '[]'),
        'related_ids': json.loads(row['related_ids_json'] or '[]'),
        'created_by': row['created_by'],
        'created_at': row['created_at'],
        'updated_at': row['updated_at'],
        'version': row['version'],
    }


def _paths_for_source_ids(conn: sqlite3.Connection, source_ids: List[str]) -> List[str]:
    if not _table_exists(conn, 'source_documents') or not source_ids:
        return []
    ph = ','.join('?' * len(source_ids))
    rows = conn.execute(
        f"SELECT DISTINCT path FROM source_documents WHERE source_id IN ({ph})",
        source_ids,
    ).fetchall()
    return [r['path'] for r in rows]


def _fetch_source_documents(
    conn: sqlite3.Connection,
    memory_events: List[dict],
    export_filter: Optional[ExportFilter],
) -> List[dict]:
    if not _table_exists(conn, 'source_documents'):
        return []

    # Collect paths referenced by memory events
    paths = list(dict.fromkeys(
        e['source'] for e in memory_events if e.get('source')
    ))

    # Paths from explicitly requested source_ids
    if export_filter and export_filter.source_ids:
        for p in _paths_for_source_ids(conn, export_filter.source_ids):
            if p not in paths:
                paths.append(p)

    if not paths:
        return []

    ph = ','.join('?' * len(paths))
    rows = conn.execute(
        f"SELECT * FROM source_documents WHERE path IN ({ph})"
        f" ORDER BY path ASC, version ASC",
        paths,
    ).fetchall()
    return [_src_row_to_dict(r) for r in rows]


def _src_row_to_dict(row) -> dict:
    return {
        'source_id': row['source_id'],
        'path': row['path'],
        'filename': row['filename'],
        'checksum_sha256': row['checksum_sha256'],
        'size_bytes': row['size_bytes'],
        'modified_time': row['modified_time'],
        'registered_at': row['registered_at'],
        'source_type': row['source_type'],
        'authority_tier': row['authority_tier'],
        'status': row['status'],
        'metadata': json.loads(row['metadata_json'] or '{}'),
        'version': row['version'],
    }


def _fetch_ingestion_runs(
    conn: sqlite3.Connection,
    source_documents: List[dict],
) -> List[dict]:
    if not _table_exists(conn, 'ingestion_runs') or not source_documents:
        return []

    source_ids = list(dict.fromkeys(d['source_id'] for d in source_documents))
    if not source_ids:
        return []

    ph = ','.join('?' * len(source_ids))
    rows = conn.execute(
        f"SELECT * FROM ingestion_runs WHERE source_id IN ({ph})"
        f" ORDER BY started_at ASC, run_id ASC",
        source_ids,
    ).fetchall()
    return [_run_row_to_dict(r) for r in rows]


def _run_row_to_dict(row) -> dict:
    return {
        'run_id': row['run_id'],
        'source_id': row['source_id'],
        'source_checksum_sha256': row['source_checksum_sha256'],
        'source_version': row['source_version'],
        'parser_version': row['parser_version'],
        'extractor_version': row['extractor_version'],
        'chunk_count': row['chunk_count'],
        'candidate_count': row['candidate_count'],
        'committed_count': row['committed_count'],
        'committed_memory_ids': json.loads(row['committed_memory_ids_json'] or '[]'),
        'status': row['status'],
        'started_at': row['started_at'],
        'completed_at': row['completed_at'],
        'metadata': json.loads(row['metadata_json'] or '{}'),
    }


def _fetch_workflow_references(workflow_db_path: str) -> List[dict]:
    """Fetch minimal workflow references from a workflow database."""
    try:
        conn = sqlite3.connect(workflow_db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT execution_id, workflow_id, plan_id, state"
            " FROM workflow_executions ORDER BY execution_id ASC LIMIT 500"
        ).fetchall()
        conn.close()
        return [
            {
                'execution_id': r['execution_id'],
                'workflow_id': r['workflow_id'],
                'plan_id': r['plan_id'],
                'status': r['state'],
            }
            for r in rows
        ]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def export_bundle(
    db_path: str,
    export_filter: Optional[ExportFilter] = None,
    workflow_db_path: Optional[str] = None,
    exported_by: str = 'fx-orchestration-system',
) -> dict:
    """
    Assemble and return a deterministic continuity bundle from the database.

    Returns the bundle as a Python dict. Serialize with:
        json.dumps(bundle, sort_keys=True, indent=2)

    Read-only: issues no writes against any database.
    Deterministic: same database state + same filter = same bundle checksum.
    """
    exported_at = _now()

    with _connect(db_path) as conn:
        memory_events = _fetch_memory_events(conn, export_filter)
        source_documents = _fetch_source_documents(conn, memory_events, export_filter)
        ingestion_runs = _fetch_ingestion_runs(conn, source_documents)

    workflow_references: List[dict] = []
    if workflow_db_path:
        workflow_references = _fetch_workflow_references(workflow_db_path)

    filter_dict = export_filter.to_dict() if export_filter else {}

    # Build content first (manifest added after checksum is known)
    bundle_content = {
        'schema_version': BUNDLE_SCHEMA_VERSION,
        'memory_events': memory_events,
        'source_documents': source_documents,
        'ingestion_runs': ingestion_runs,
        'workflow_references': workflow_references,
    }

    manifest = build_manifest(
        bundle=bundle_content,
        exported_at=exported_at,
        exported_by=exported_by,
        filters=filter_dict,
    )

    bundle_content['manifest'] = manifest
    return bundle_content

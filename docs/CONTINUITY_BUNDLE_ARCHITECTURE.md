# Continuity Bundle Architecture

## Purpose

A continuity bundle is a **deterministic, portable snapshot of governed cognition lineage**. It allows the full provenance chain of memory events — including semantic extraction records — to be transferred between instances of the fx-orchestration-system without loss of attribution or auditability.

Canonical truth remains in the **source database's lineage tables**. Bundles are transport containers — not authoritative replicas.

---

## Schema Version History

| Version | Change |
|---------|--------|
| `1.0` | Original: memory_events, source_documents, ingestion_runs, workflow_references |
| `1.1` | Added: semantic_execution_runs, semantic_candidate_events |

Both versions are accepted by `validate_bundle()`. Export always produces the latest version (`1.1`).

---

## Bundle Structure (1.1)

```json
{
  "schema_version": "1.1",
  "manifest": { ... },
  "memory_events": [ ... ],
  "source_documents": [ ... ],
  "ingestion_runs": [ ... ],
  "workflow_references": [ ... ],
  "semantic_execution_runs": [ ... ],
  "semantic_candidate_events": [ ... ]
}
```

### Sections

| Section | Source table | Content |
|---|---|---|
| `memory_events` | `memory_events` | All governed knowledge claims |
| `source_documents` | `source_documents` | Files that produced the events |
| `ingestion_runs` | `ingestion_runs` | Per-file ingestion provenance |
| `workflow_references` | `workflow_executions` (separate db) | Optional execution refs |
| `semantic_execution_runs` | `semantic_execution_runs` | One row per semantic extraction run whose candidates were promoted to memory |
| `semantic_candidate_events` | `semantic_candidate_events` | Promoted semantic candidates (status='promoted') linked to exported memory events |

### Semantic section filtering

Only `promoted` candidates whose `promoted_memory_id` is in the exported `memory_events` set are included. Unpromoted (`candidate`) or rejected candidates are not exported. The corresponding semantic run rows are pulled via the included candidates' `semantic_run_id`.

---

## Manifest (1.1)

```json
{
  "bundle_id": "a1b2c3d4e5f60708",
  "schema_version": "1.1",
  "exported_at": "2026-01-01T00:00:00Z",
  "exported_by": "fx-orchestration-system",
  "filters": { ... },
  "memory_event_count": 42,
  "source_count": 7,
  "ingestion_run_count": 9,
  "workflow_reference_count": 0,
  "semantic_execution_run_count": 5,
  "semantic_candidate_event_count": 5,
  "checksum_sha256": "abc123..."
}
```

### bundle_id derivation

```
sha256( exported_at + NUL + str(sorted(event_ids)) )[:16]
```

### Checksum

```
sha256( json.dumps(bundle_minus_checksum, sort_keys=True, ensure_ascii=True) )
```

For schema 1.1 bundles, the checksum covers all eight sections including the semantic sections. For schema 1.0 bundles, only the original six sections are covered (backward-compatible with existing 1.0 bundle files).

---

## Ordering Guarantees (Checksum Stability)

All section fetchers enforce deterministic ordering:

| Section | Order |
|---|---|
| `memory_events` | `ORDER BY id ASC` |
| `source_documents` | `ORDER BY path ASC, version ASC` |
| `ingestion_runs` | `ORDER BY started_at ASC, run_id ASC` |
| `workflow_references` | `ORDER BY execution_id ASC` |
| `semantic_candidate_events` | `ORDER BY created_at ASC, candidate_id ASC` |
| `semantic_execution_runs` | `ORDER BY started_at ASC, run_id ASC` |

Same database state + same filter = same bundle checksum.

---

## Export

```
export_bundle(db_path, export_filter=None, workflow_db_path=None) -> dict
```

- **Read-only**: issues no INSERT, UPDATE, or DELETE against any database.
- **Filter support**: `tags`, `source_ids`, `unresolved_only`, `since`, `until` (all ANDed).
- **Missing tables**: handled gracefully — returns empty sections (including empty semantic sections).
- **Source documents**: all versions for paths referenced by exported events.

### Export Filter

```python
ExportFilter(
    tags=['usd', 'fed'],         # AND semantics per tag
    source_ids=['a1b2c3d4'],     # resolved to paths
    unresolved_only=True,        # status IN ('unresolved', 'proposed')
    since='2026-01-01T00:00:00Z',
    until='2026-06-01T00:00:00Z',
)
```

Filter applies to `memory_events` first. Semantic sections follow automatically — only candidates for exported events are included.

---

## Import

```
import_bundle(bundle_dict, db_path, dry_run=False) -> ImportResult
```

### Collision Semantics

| Situation | Action |
|---|---|
| Record not found in target | **Insert** |
| Record found, same payload (content-addressed) | **Skip** (idempotent) |
| Record found, different payload | **Collision** — entire import refused |
| Candidate's `promoted_memory_id` not in target or bundle | **Collision** — dangling reference |

### Atomicity

If any collision is detected, **zero records are written**. The import is either complete or a no-op — never partial.

### Identity Preservation

Memory events are imported with their original `id` values preserved. After import, the SQLite autoincrement sequence is bumped to `max(existing, imported)` so future service-generated ids never collide.

### Write Order

Dependencies are respected:

1. `source_documents` (no foreign-key dependencies)
2. `memory_events` (references source paths as strings)
3. `ingestion_runs` (references source_id logically)
4. `semantic_execution_runs` (references source_id logically)
5. `semantic_candidate_events` (promoted_memory_id must reference an existing memory_events row)

The `promoted_memory_id` reference is validated in the planning pass before any writes. A candidate whose `promoted_memory_id` is not present in the target DB **and** not in the bundle's `memory_events` section is a collision.

### Dry-Run Mode

`dry_run=True` performs the full planning pass (collision detection) and returns counts of what would be inserted and skipped, without writing anything.

---

## Provenance Chain (1.1)

```
source_documents.source_id
        ↓
ingestion_runs.source_id + committed_memory_ids
        ↓
memory_events.id × N
        ↑
semantic_candidate_events.promoted_memory_id
        ↑
semantic_execution_runs.run_id  ←  semantic_candidate_events.semantic_run_id
```

The full semantic provenance chain — from extraction run through promoted candidate to committed memory event — is portable in schema 1.1 bundles.

---

## ImportResult fields (1.1)

```python
ImportResult(
    imported_memory_events=N,
    imported_source_documents=N,
    imported_ingestion_runs=N,
    imported_semantic_execution_runs=N,
    imported_semantic_candidate_events=N,
    skipped_memory_events=N,
    skipped_source_documents=N,
    skipped_ingestion_runs=N,
    skipped_semantic_execution_runs=N,
    skipped_semantic_candidate_events=N,
    collisions=[...],
    dry_run=False,
)
```

---

## Invariants

- `schema_version` must be `'1.0'` or `'1.1'`.
- All counts in `manifest` must match actual section lengths.
- `manifest.checksum_sha256` must match the computed checksum (version-aware).
- For schema 1.1: bundle must contain `semantic_execution_runs` and `semantic_candidate_events` top-level keys.
- All timestamps are UTC ISO-8601: `YYYY-MM-DDTHH:MM:SSZ`.
- All JSON serialization uses `sort_keys=True` for determinism.
- No live trading data, broker connections, or strategy signals in bundles.

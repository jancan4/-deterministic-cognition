# Continuity Bundle Architecture

## Purpose

A continuity bundle is a **deterministic, portable snapshot of governed cognition lineage**. It allows the full provenance chain of memory events to be transferred between instances of the fx-orchestration-system without loss of attribution or auditability.

Canonical truth remains in the **source database's lineage tables**. Bundles are transport containers — not authoritative replicas.

---

## Bundle Structure

```json
{
  "schema_version": "1.0",
  "manifest": { ... },
  "memory_events": [ ... ],
  "source_documents": [ ... ],
  "ingestion_runs": [ ... ],
  "workflow_references": [ ... ]
}
```

### Sections

| Section | Source table | Content |
|---|---|---|
| `memory_events` | `memory_events` | All governed knowledge claims |
| `source_documents` | `source_documents` | Files that produced the events |
| `ingestion_runs` | `ingestion_runs` | Per-file ingestion provenance |
| `workflow_references` | `workflow_executions` (separate db) | Optional execution refs |

---

## Manifest

The manifest is the tamper-evident header stored inside the bundle:

```json
{
  "bundle_id": "a1b2c3d4e5f60708",
  "schema_version": "1.0",
  "exported_at": "2026-01-01T00:00:00Z",
  "exported_by": "fx-orchestration-system",
  "filters": { ... },
  "memory_event_count": 42,
  "source_count": 7,
  "ingestion_run_count": 9,
  "workflow_reference_count": 0,
  "checksum_sha256": "abc123..."
}
```

### bundle_id derivation

```
sha256( exported_at + NUL + str(sorted(event_ids)) )[:16]
```

Stable for the same export of the same set of events. Two exports of the same database at different times produce different `bundle_id` values (different `exported_at`), but the same `memory_events` content.

### Checksum

```
sha256( json.dumps(bundle_minus_checksum, sort_keys=True, ensure_ascii=True) )
```

The `manifest.checksum_sha256` field is excluded from the checksum computation to avoid circularity. Any modification to any section changes the checksum.

---

## Ordering Guarantees (Checksum Stability)

All section fetchers enforce deterministic ordering:

| Section | Order |
|---|---|
| `memory_events` | `ORDER BY id ASC` |
| `source_documents` | `ORDER BY path ASC, version ASC` |
| `ingestion_runs` | `ORDER BY started_at ASC, run_id ASC` |
| `workflow_references` | `ORDER BY execution_id ASC` |

Same database state + same filter = same bundle checksum.

---

## Export

```
export_bundle(db_path, export_filter=None, workflow_db_path=None) -> dict
```

- **Read-only**: issues no INSERT, UPDATE, or DELETE against any database.
- **Filter support**: `tags`, `source_ids`, `unresolved_only`, `since`, `until` (all ANDed).
- **Missing tables**: handled gracefully — returns empty sections.
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

### Atomicity

If any collision is detected, **zero records are written**. The import is either complete or a no-op — never partial.

### Identity Preservation

Memory events are imported with their original `id` values preserved. After import, the SQLite autoincrement sequence is bumped to `max(existing, imported)` so future service-generated ids never collide.

### Write Order

Dependencies are respected:

1. `source_documents` (no foreign-key dependencies)
2. `memory_events` (references source paths as strings)
3. `ingestion_runs` (references source_id logically)

### Dry-Run Mode

`dry_run=True` performs the full planning pass (collision detection) and returns counts of what would be inserted and skipped, without writing anything.

---

## Provenance Chain

```
source_documents.source_id
        ↓
ingestion_runs.source_id + committed_memory_ids
        ↓
memory_events.id × N
```

All three links are included in every bundle, making the full chain traversable without querying multiple systems.

---

## Invariants

- `schema_version` must be `'1.0'`.
- All counts in `manifest` must match actual section lengths.
- `manifest.checksum_sha256` must match the computed checksum.
- All timestamps are UTC ISO-8601: `YYYY-MM-DDTHH:MM:SSZ`.
- All JSON serialization uses `sort_keys=True` for determinism.
- No live trading data, broker connections, or strategy signals in bundles.

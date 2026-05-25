# Continuity Bundle Architecture

**Repository commit:** 42fef07  
**Memory schema version:** 16  
**Workflow schema version:** 3  
**Continuity bundle schema version:** 1.2  
**Document date:** 2026-05-25  
**Replay compatibility:** v1.0 and v1.1 bundles are accepted by `validate_bundle()` and can be imported into any target running schema v1.2. v1.2 bundles require a target that understands the seven additional manifest fields.

---

## Purpose

A continuity bundle is a **deterministic, portable snapshot of governed cognition lineage**. It allows the full provenance chain of memory events — including semantic extraction records — to be transferred between instances of the cognition substrate without loss of attribution or auditability.

Canonical truth remains in the **source database's lineage tables**. Bundles are transport containers — not authoritative replicas.

---

## Schema Version History

| Version | Change |
|---|---|
| `1.0` | Original sections: `memory_events`, `source_documents`, `ingestion_runs`, `workflow_references` |
| `1.1` | Added sections: `semantic_execution_runs`, `semantic_candidate_events` |
| `1.2` | Added seven tamper-evident manifest fields for recovery metadata (see Manifest section) |

All three versions are accepted by `validate_bundle()`. Export always produces the latest version (`1.2`).

---

## Bundle Structure (1.2)

```json
{
  "schema_version": "1.2",
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
| `memory_events` | `memory_events` | All governed memory events included by the export filter |
| `source_documents` | `source_documents` | All versions of source files that produced the exported events |
| `ingestion_runs` | `ingestion_runs` | Per-file ingestion provenance for the exported source documents |
| `workflow_references` | `workflow_executions` (separate db) | Optional execution references; empty if no workflow db provided |
| `semantic_execution_runs` | `semantic_execution_runs` | One row per semantic extraction run whose candidates were promoted to exported memory events |
| `semantic_candidate_events` | `semantic_candidate_events` | Promoted semantic candidates (status=`promoted`) linked to exported memory events |

### Semantic section filtering

Only `promoted` candidates whose `promoted_memory_id` is in the exported `memory_events` set are included. Unpromoted (`candidate`) or rejected candidates are not exported. The corresponding semantic run rows are pulled via the included candidates' `semantic_run_id`.

### Phase 6D compression-derived proposed filter

By default, memory events whose `source` begins with `compression_artifact:` and whose `status` is `proposed` are excluded from export. This filter is applied before source document and semantic candidate fetching, so it propagates consistently through all sections. Pass `include_compression_derived_proposed=True` to override.

---

## Manifest (1.2)

```json
{
  "bundle_id": "a1b2c3d4e5f60708",
  "schema_version": "1.2",
  "exported_at": "2026-01-01T00:00:00Z",
  "exported_by": "fx-orchestration-system",
  "filters": { ... },
  "memory_event_count": 42,
  "source_count": 7,
  "ingestion_run_count": 9,
  "workflow_reference_count": 0,
  "semantic_execution_run_count": 5,
  "semantic_candidate_event_count": 5,
  "exported_db_schema_version": 16,
  "compression_derived_proposed_excluded": true,
  "compression_derived_proposed_excluded_count": 3,
  "dangling_compression_source_count": 0,
  "lineage_integrity_checked": false,
  "lineage_integrity_all_ok": null,
  "lineage_integrity_broken_count": 0,
  "checksum_sha256": "abc123..."
}
```

### Recovery metadata fields (v1.2)

| Field | Type | Description |
|---|---|---|
| `exported_db_schema_version` | integer or null | Memory schema version at export time (`memory_schema_version` table); `null` if absent |
| `compression_derived_proposed_excluded` | bool | `true` if Phase 6D filter was applied during export |
| `compression_derived_proposed_excluded_count` | int | Number of compression-derived proposed events excluded by the filter |
| `dangling_compression_source_count` | int | Count of exported events whose `source` begins with `compression_artifact:` — these reference artifact rows that are not bundled |
| `lineage_integrity_checked` | bool | `true` if `check_lineage_integrity()` was run during export |
| `lineage_integrity_all_ok` | bool or null | `true` if all FK checks passed; `null` if not checked |
| `lineage_integrity_broken_count` | int | Number of broken FK relationships found; 0 if unchecked |

All seven recovery fields are covered by the bundle checksum. Mutating any field after export will cause checksum mismatch on validation.

### bundle_id derivation

```
sha256( exported_at + NUL + str(sorted(event_ids)) )[:16]
```

The `bundle_id` is deterministic: the same events exported at the same timestamp always produce the same id.

### Checksum

```
sha256( json.dumps(bundle_minus_checksum, sort_keys=True, ensure_ascii=True) )
```

`bundle_minus_checksum` is the full bundle dict with `manifest.checksum_sha256` excluded. All manifest fields (including all seven recovery metadata fields) are covered. For schema `1.1` and `1.2` bundles the checksum covers all eight sections including the semantic sections. For schema `1.0` bundles only the original six sections are covered, preserving backward compatibility with existing `1.0` bundle files.

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
export_bundle(
    db_path,
    export_filter=None,
    workflow_db_path=None,
    exported_by='fx-orchestration-system',
    include_compression_derived_proposed=False,
    include_lineage_integrity=False,
) -> dict
```

- **Read-only**: issues no INSERT, UPDATE, or DELETE against any database.
- **Filter support**: `tags`, `source_ids`, `unresolved_only`, `since`, `until` (all ANDed).
- **Missing tables**: handled gracefully — returns empty sections.
- **Source documents**: all versions for paths referenced by exported events.

### Export flags

| Flag | Default | Effect |
|---|---|---|
| `include_compression_derived_proposed` | `False` | When `False`, excludes compression-derived proposed events (Phase 6D policy). Sets `compression_derived_proposed_excluded=True` and records the excluded count in the manifest. |
| `include_lineage_integrity` | `False` | When `True`, runs `check_lineage_integrity()` on the source DB and records all four FK check results in the manifest. |

### Export Filter

```python
ExportFilter(
    tags=['usd', 'fed'],          # AND semantics per tag
    source_ids=['a1b2c3d4'],      # resolved to paths
    unresolved_only=True,         # status IN ('unresolved', 'proposed')
    since='2026-01-01T00:00:00Z',
    until='2026-06-01T00:00:00Z',
)
```

Filter applies to `memory_events` first. Source document and semantic sections follow automatically — only records linked to exported events are included.

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
5. `semantic_candidate_events` (`promoted_memory_id` must reference an existing `memory_events` row)

The `promoted_memory_id` reference is validated in the planning pass before any writes. A candidate whose `promoted_memory_id` is not present in the target DB and not in the bundle's `memory_events` section is a collision.

### Dry-Run Mode

`dry_run=True` performs the full planning pass (collision detection, warning detection) and returns counts of what would be inserted and skipped, without writing anything. Dry-run always exits with code 0.

### Import Warnings

Warnings are non-blocking. They are attached to `ImportResult.warnings` and reported to stderr by the CLI. A successful import with warnings exits with code 2; without warnings exits with code 0.

Three warning classes are emitted:

| Warning Class | Trigger |
|---|---|
| Schema version mismatch | `exported_db_schema_version` in the manifest differs from the target database's `memory_schema_version` |
| Phase 6D policy disclosure | Bundle manifest declares `compression_derived_proposed_excluded=true` with a non-zero excluded count |
| Dangling compression artifact provenance | An imported memory event has `source` beginning with `compression_artifact:N` but artifact id `N` is absent from the target's `compression_artifacts` table |

### Exit Code Semantics

| Situation | Exit code |
|---|---|
| Dry-run (any outcome) | 0 |
| Successful import, no warnings | 0 |
| Successful import, warnings present | 2 |
| Collision detected or validation error | 1 |

---

## Bundle Inspection (Read-Only)

```
python -m cli.main bundle-inspect PATH [--db PATH] [--format text|json]
```

`bundle-inspect` is a read-only command. It validates the bundle, prints the manifest summary, and optionally cross-checks against a target database using a read-only URI connection (`sqlite3.connect(f"file:{db}?mode=ro", uri=True)`). No writes are issued under any circumstances. All database access degrades gracefully — if the target database is absent or missing expected tables, the command reports what it can and exits cleanly.

| Flag | Effect |
|---|---|
| `--db PATH` | Optional. Cross-checks manifest fields against the target database (schema version, compression artifact presence). |
| `--format text` | Default. Human-readable manifest summary. |
| `--format json` | Machine-readable manifest JSON. |

---

## Provenance Chain (1.2)

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

The full semantic provenance chain — from extraction run through promoted candidate to committed memory event — is portable in schema 1.2 bundles. Memory events with `source` beginning with `compression_artifact:N` carry a reference to a compression artifact row that is not bundled; this is recorded in `dangling_compression_source_count` and may produce an import warning.

---

## ImportResult Fields (1.2)

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
    warnings=[...],        # list of warning strings (non-blocking)
)
```

`to_dict()` includes `warning_count` (integer) and `warnings` (list of strings).

---

## Invariants

- `schema_version` must be `'1.0'`, `'1.1'`, or `'1.2'`.
- All counts in `manifest` must match actual section lengths.
- `manifest.checksum_sha256` must match the computed checksum (version-aware).
- For schema `1.1` and `1.2`: bundle must contain `semantic_execution_runs` and `semantic_candidate_events` top-level keys.
- For schema `1.2`: manifest must contain all seven recovery metadata fields.
- All timestamps are UTC ISO-8601: `YYYY-MM-DDTHH:MM:SSZ`.
- All JSON serialization uses `sort_keys=True` for determinism.
- `bundle-inspect` is permanently read-only. It does not repair, patch, or modify bundles or databases.
- No execution signals, broker references, or strategy payloads in bundles.

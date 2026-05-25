# Schema History

**Repository commit:** 42fef07  
**Memory schema version:** 16  
**Workflow schema version:** 3  
**Continuity bundle schema version:** 1.2  
**Document date:** 2026-05-25  
**Replay compatibility:** See per-schema replay compatibility rules below.

---

## Memory Schema (`memory_schema_version`)

The memory schema version is stored in the `memory_schema_version` table. `init_db()` writes the version row on first initialization and migrates forward on subsequent calls. All migrations are forward-only. No down-migrations exist or are supported.

### Version History

| Version | Primary addition |
|---|---|
| 1 | `memory_events`, `memory_revisions`, `memory_links` — base event-sourced memory layer |
| 2 | `source_documents` — source file registry |
| 3 | `ingestion_runs` — per-file ingestion provenance ledger |
| 4 | `cognition_sessions` — session open/close lifecycle tracking |
| 5 | `activation_policies` — rule definitions governing when sessions may fire decisions |
| 6 | `activation_decision_log` — per-session policy evaluation records |
| 7 | `context_assembly_log` — records of which events were assembled for each session |
| 8 | `activation_log_transitions` — state transition records for activation log entries |
| 9 | `memory_embeddings` — optional vector embedding storage (model-pinned) |
| 10 | `embedding_model_pins` — active embedding model pin record |
| 11 | `confidence_revision_requests` — governed confidence revision workflow |
| 12 | `compression_artifacts` — compressed summaries derived from memory events |
| 13 | `compression_supersessions` — supersession chain linking compression artifacts |
| 14 | `ontology_terms` — governed concept registry with aliases and lifecycle state |
| 15 | `ontology_aliases` — alias-to-canonical-term mapping |
| 16 | `semantic_execution_runs`, `semantic_candidate_events` — semantic extraction provenance |

### Migration Invariants

- `init_db()` is idempotent: `CREATE TABLE IF NOT EXISTS` for every table; safe to call on an existing database.
- `PRAGMA foreign_keys = ON` is set on every connection.
- Version is read from `memory_schema_version` at startup; if the stored version is less than `_MEMORY_SCHEMA_VERSION`, the migration path runs forward from the stored version to the current version.
- No table is dropped by a migration. All migrations are additive.
- All timestamps written by `service.py` are UTC ISO-8601 (`YYYY-MM-DDTHH:MM:SSZ`).

### Replay Compatibility

Any database at schema version N can be exported as a continuity bundle and imported into a target at schema version M, provided:
- Both the source and target understand the bundle schema version used.
- If M < N, an import warning is emitted (schema version mismatch). The import proceeds; the operator decides whether to act.
- If M > N, the same warning is emitted. The bundle was exported from an older substrate; the target has tables the bundle does not cover.

There is no hard import block on schema version mismatch. Warnings are surfaced; decisions remain with the operator.

---

## Workflow Schema (`workflow_schema_version`)

The workflow schema version is stored in the `workflow_schema_version` table inside the workflow database. `init_db()` writes the version row on first initialization.

### Version History

| Version | Primary addition |
|---|---|
| 1 | `workflow_definitions`, `workflow_executions`, `execution_lineage_events` — core execution and replay substrate |
| 2 | `workflow_snapshots` — cache-only snapshot checkpoints for delta-replay optimization |
| 3 | `semantic_execution_runs`, `semantic_candidate_events` — semantic extraction workflow integration |

### Migration Invariants

- Same idempotency guarantees as the memory schema: `CREATE TABLE IF NOT EXISTS`, forward-only.
- Snapshots (`workflow_snapshots`) are cache-only. Deleting the table has no effect on lineage correctness — it only forces full replay.
- `execution_lineage_events` is append-only. No row is ever deleted.

---

## Continuity Bundle Schema (`schema_version` in bundle JSON)

The bundle schema version is a string field in the bundle's top-level `schema_version` key and is mirrored in `manifest.schema_version`.

### Version History

| Version | Sections | Manifest keys added |
|---|---|---|
| `1.0` | `memory_events`, `source_documents`, `ingestion_runs`, `workflow_references` | base set |
| `1.1` | + `semantic_execution_runs`, `semantic_candidate_events` | `semantic_execution_run_count`, `semantic_candidate_event_count` |
| `1.2` | same sections as 1.1 | + 7 recovery metadata fields (see below) |

### v1.2 Recovery Metadata Fields

| Field | Type | Default |
|---|---|---|
| `exported_db_schema_version` | integer or null | `null` |
| `compression_derived_proposed_excluded` | bool | `false` |
| `compression_derived_proposed_excluded_count` | int | `0` |
| `dangling_compression_source_count` | int | `0` |
| `lineage_integrity_checked` | bool | `false` |
| `lineage_integrity_all_ok` | bool or null | `null` |
| `lineage_integrity_broken_count` | int | `0` |

All seven fields are covered by the bundle checksum. Mutating any field after export causes checksum mismatch on validation.

### Supported Version Set

`validate_bundle()` accepts `'1.0'`, `'1.1'`, and `'1.2'`. Any other value raises `BundleValidationError`.

### Required Key Sets Per Version

| Schema | Required top-level keys | Required manifest keys |
|---|---|---|
| `1.0` | `memory_events`, `source_documents`, `ingestion_runs`, `workflow_references` | base manifest keys |
| `1.1` | + `semantic_execution_runs`, `semantic_candidate_events` | + semantic count keys |
| `1.2` | same as 1.1 | + 7 recovery metadata keys |

### Checksum Coverage Per Version

| Schema | Checksum covers |
|---|---|
| `1.0` | `schema_version`, manifest (minus checksum field), `memory_events`, `source_documents`, `ingestion_runs`, `workflow_references` |
| `1.1` | same + `semantic_execution_runs`, `semantic_candidate_events` |
| `1.2` | same as 1.1 (semantic sections included; recovery metadata fields covered via manifest inclusion) |

### Bundle Import Compatibility Rules

| Bundle version | Target schema version | Outcome |
|---|---|---|
| `1.0` | any | Accepted; no import warnings from schema mismatch unless `exported_db_schema_version` differs from target |
| `1.1` | any | Accepted; same warning rules |
| `1.2` | any | Accepted; seven recovery metadata fields are read by importer for warning detection |
| any | schema mismatch | Non-blocking warning emitted; import proceeds |
| any | collision detected | Import refused entirely; zero writes |

See `docs/CONTINUITY_BUNDLE_ARCHITECTURE.md` for canonical import semantics.

---

## See Also

- `docs/CONTINUITY_BUNDLE_ARCHITECTURE.md` — canonical bundle import/export semantics
- `docs/RECOVERY_HANDBOOK.md` — operator procedures for schema mismatch and import warning handling
- `docs/OPERATOR_GUIDE.md` — initialization and migration workflow

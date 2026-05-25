# Operator Guide

**Repository commit:** 6cc520f  
**Memory schema version:** 16  
**Workflow schema version:** 3  
**Continuity bundle schema version:** 1.2  
**Document date:** 2026-05-25  
**Replay compatibility note:** Procedures in this guide apply to the schema versions listed above. If the substrate has migrated forward, verify procedures against the updated `docs/SCHEMA_HISTORY.md` before use.

---

## Canonical Terminology

The following terms are used consistently throughout this guide and all other operator-facing documentation. Do not substitute synonyms.

| Term | Definition |
|---|---|
| **cognition substrate** | The complete system: memory layer, session layer, activation policy engine, semantic extraction pipeline, compression layer, continuity bundle transport, and governance verification tooling |
| **memory event** | A discrete, typed, governed unit of institutional knowledge stored as a row in `memory_events` |
| **compression artifact** | A compressed summary derived from one or more memory events, stored as a row in `compression_artifacts` |
| **continuity context** | The assembled set of memory events and related lineage presented to a reasoning session as structured context |
| **activation policy** | A rule stored in `activation_policies` that governs when a cognition session may fire a decision |
| **activation decision** | A logged record in `activation_decision_log` of a policy evaluation outcome (fire or no-fire) |
| **ontology term** | A governed concept stored in `ontology_terms`, with aliases and supersession/deprecation state |
| **governance issue** | A `GovernanceIssue` instance emitted by a detection function; carries severity, type, memory_id, rationale, and recommended_action |
| **replay** | Deterministic reconstruction of execution state by re-applying a lineage event log from the beginning |
| **verification** | An operator-initiated, read-only integrity check against live database state (governance, assembly, session, lineage) |
| **supersession** | Replacing one record with a newer record while preserving the original as auditable history; the original gains status `superseded` |
| **invalidation** | Marking a compression artifact as no longer valid without a replacement; does not supersede |
| **lineage integrity** | The property that all foreign-key relationships between activation decisions, context assemblies, activation log transitions, and cognition sessions are intact and unbroken |

> **Authority boundary:** This guide explains operational workflows only. Deep semantics, invariants, and replay compatibility rules are defined in the architecture documents. Do not treat this guide as the authoritative source for lifecycle semantics.

---

## 1. Initialization

### Memory database

```bash
python -m memory.cli init --db memory.db
```

Creates all tables through schema version 16. Safe to re-run on an existing database (`init_db` is idempotent). The schema version is recorded in `memory_schema_version`.

Canonical semantics: `docs/SCHEMA_HISTORY.md` — Memory Schema section.

### Workflow database

```bash
python -m cli.main --db workflow.db status
```

Workflow storage initializes automatically on first use. To inspect schema version:

```bash
sqlite3 workflow.db "SELECT version FROM workflow_schema_version;"
```

---

## 2. Source Ingestion

### Register a source document

```bash
python -m cli.main --db memory.db sources-register \
  --path data/report.txt \
  --source-type research_report \
  --created-by operator
```

### Ingest a file

```bash
python -m cli.main --db memory.db ingest-file \
  --path data/report.txt \
  --created-by operator
```

Ingestion parses the file, extracts signals, and writes memory events and an ingestion ledger row. The ingestion run id and committed memory event ids are recorded in `ingestion_runs`.

### Review ingestion history

```bash
python -m cli.main --db memory.db ingestion-runs
python -m cli.main --db memory.db ingestion-run-show --run-id RUN_ID
```

Canonical semantics: `docs/INGESTION_RUN_LEDGER_ARCHITECTURE.md`, `docs/SIGNAL_EXTRACTION_INGESTION_ARCHITECTURE.md`.

---

## 3. Retrieval and Context Assembly

### Retrieve memory events

```bash
python -m memory.cli retrieve --db memory.db --query "central bank rate" \
  --min-confidence 3 \
  --exclude-deprecated
```

### Assemble a continuity context

```bash
python -m cli.main --db memory.db session-context \
  --query "monetary policy" \
  --min-confidence 3 \
  --budget 20 \
  --exclude-deprecated
```

The assembled event set is printed to stdout. The assembly is recorded in `context_assembly_log` with an `assembly_id` that can be referenced by a subsequent session or verification.

Canonical semantics: `docs/CONTEXT_ASSEMBLY_CLI_ARCHITECTURE.md`, `docs/SESSION_RECONSTRUCTION_ARCHITECTURE.md`.

---

## 4. Governance Verification

Run these commands after any significant ingestion or session batch, or on a regular schedule.

### Full governance report

```bash
python -m memory.cli governance-report --db memory.db
python -m memory.cli governance-report --db memory.db --format json > report.json
```

Emits a list of governance issues with severity (`info`, `warning`, `critical`), type, affected memory event id, rationale, and recommended action. Lineage integrity checks run by default.

Each governance issue is a recommendation. No automatic resolution occurs.

### Lineage integrity check

```bash
python -m memory.cli lineage-integrity --db memory.db
```

Runs four foreign-key checks against the live database. Exits 0 when all pass, 1 when any broken relationship is found. Use this as a lightweight check independent of the full governance report.

### Verify a context assembly

```bash
python -m memory.cli verify-assembly --db memory.db --assembly-id N
```

Checks whether the current memory state diverges from what was assembled at assembly time. Divergence may indicate intervening status changes or new events that were not present at assembly time.

### Verify a cognition session timeline

```bash
python -m memory.cli verify-session --db memory.db --session-id N
```

Checks whether the session timeline reconstructed from the transition log matches the recorded session state.

Canonical semantics: `docs/MEMORY_GOVERNANCE_ARCHITECTURE.md` — Operational Governance Verification section.

---

## 5. Export

### Standard export

```bash
python -m cli.main --db memory.db export-bundle --out bundle.json
```

Applies the Phase 6D compression-derived proposed filter by default (excludes memory events sourced from compression artifacts with `proposed` status). The bundle manifest records what was excluded.

### Export with lineage integrity metadata

```bash
python -m cli.main --db memory.db export-bundle --out bundle.json \
  --include-lineage-integrity
```

Runs `check_lineage_integrity()` during export and records the results in the manifest. Adds no overhead to import; the check runs on the source database only.

### Export with compression-derived proposed events included

```bash
python -m cli.main --db memory.db export-bundle --out bundle.json \
  --include-compression-proposed
```

Override the Phase 6D filter. Use only when the receiving operator is aware that compression-derived proposed events are included.

### Filtered export

```bash
python -m cli.main --db memory.db export-bundle --out bundle.json \
  --tags governance,architecture \
  --since 2026-01-01T00:00:00Z
```

Canonical semantics: `docs/CONTINUITY_BUNDLE_ARCHITECTURE.md` — Export section.

---

## 6. Import

### Dry run first

```bash
python -m cli.main --db memory.db import-bundle --bundle bundle.json --dry-run
```

Always dry-run before a live import. A dry run performs the full collision detection and warning detection pass without writing anything. Exit code is always 0 on dry run regardless of warnings or collisions found.

### Live import

```bash
python -m cli.main --db memory.db import-bundle --bundle bundle.json
```

Exit codes:

| Exit code | Meaning |
|---|---|
| 0 | Success, no warnings |
| 1 | Collision detected or validation error — zero records written |
| 2 | Success, warnings present — records were written |

Warnings are printed to stderr. They are non-blocking. See `docs/RECOVERY_HANDBOOK.md` for warning interpretation.

Canonical semantics: `docs/CONTINUITY_BUNDLE_ARCHITECTURE.md` — Import section.

---

## 7. Bundle Inspection

```bash
python -m cli.main bundle-inspect bundle.json
python -m cli.main bundle-inspect bundle.json --db memory.db
python -m cli.main bundle-inspect bundle.json --format json
```

`bundle-inspect` is read-only. It validates the bundle checksum, prints the manifest, and (with `--db`) cross-checks manifest fields against a target database without writing anything.

Use `bundle-inspect` to:
- Verify a bundle before transport or import
- Compare `exported_db_schema_version` with the target's schema
- Check `dangling_compression_source_count` before importing to a target that may lack the referenced artifacts

Canonical semantics: `docs/CONTINUITY_BUNDLE_ARCHITECTURE.md` — Bundle Inspection section.

---

## 8. Workflow Recovery

### Check non-terminal executions

```bash
python -m cli.main --db workflow.db status
```

### Dry-run recovery

```bash
python -m cli.main --db workflow.db recover
```

Read-only. Reports divergence between mutable execution rows and replayed lineage state for all non-terminal executions.

### Apply recovery

```bash
python -m cli.main --db workflow.db recover --apply
```

Writes the replayed state back to the mutable row for executions where `is_recoverable=True`. Only run after reviewing the dry-run output.

### Point-in-time inspection

```bash
python -m cli.main --db workflow.db inspect --execution-id ID
python -m cli.main --db workflow.db inspect --execution-id ID --at-event 5
```

`--at-event N` replays only the first N events, enabling reconstruction of any historical state. Divergence comparison against the current mutable row is suppressed on partial replays.

Canonical semantics: `docs/PROCESS_ENTRYPOINT_AND_RECOVERY_ARCHITECTURE.md`.

---

## 9. Operational Invariants

The following invariants hold across all operations. They are not configurable.

| Invariant | Scope |
|---|---|
| All timestamps are UTC ISO-8601 (`YYYY-MM-DDTHH:MM:SSZ`) | All writes in `service.py` |
| All JSON serialization uses `sort_keys=True` | Export, bundle, governance report |
| `init_db()` is idempotent | Memory and workflow databases |
| All governance and verification functions are read-only | `governance.py`, `verify_*` functions |
| `bundle-inspect` is permanently read-only | `cli/main.py cmd_bundle_inspect` |
| Dry-run import never writes | `import_bundle(dry_run=True)` |
| Export never writes to the source database | `export_bundle()` |
| `recover` (without `--apply`) never writes | `workflow/recovery.py recover_execution()` |
| Memory events are never deleted — only transitioned through governed statuses | `service.py` |
| The lineage event log is append-only | `execution_lineage_events`, `memory_revisions` |

---

## See Also

- `docs/CLI_REFERENCE.md` — complete command reference with all flags
- `docs/SCHEMA_HISTORY.md` — schema version history and migration invariants
- `docs/RECOVERY_HANDBOOK.md` — recovery procedures and warning interpretation
- `docs/CONTINUITY_BUNDLE_ARCHITECTURE.md` — canonical bundle import/export semantics
- `docs/MEMORY_GOVERNANCE_ARCHITECTURE.md` — canonical governance semantics and detection function reference
- `docs/PROCESS_ENTRYPOINT_AND_RECOVERY_ARCHITECTURE.md` — canonical workflow recovery and replay semantics
- `docs/CONTEXT_ASSEMBLY_CLI_ARCHITECTURE.md` — canonical context assembly semantics
- `memory/README.md` — memory layer schema and approved vocabulary reference

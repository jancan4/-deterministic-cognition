# Recovery Handbook

**Repository commit:** 6cc520f  
**Memory schema version:** 16  
**Workflow schema version:** 3  
**Continuity bundle schema version:** 1.2  
**Document date:** 2026-05-25  
**Replay compatibility note:** Recovery procedures in this handbook apply to the schema versions listed above. Before using any procedure on a substrate that has migrated forward, verify current schema versions and review `docs/SCHEMA_HISTORY.md`.

---

## Canonical Terminology

The following terms are used consistently throughout this handbook. Do not substitute synonyms.

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

> **Authority boundary:** This handbook covers recovery procedures only. Invariants, replay semantics, and lifecycle semantics are defined in the architecture documents. Do not treat this handbook as the authoritative source for those definitions.

---

## 1. When to Use This Handbook

Use this handbook when you observe:

- `BundleValidationError` from `validate_bundle()` or `bundle-inspect`
- Checksum mismatch on a received bundle
- Import warnings (exit code 2 from `import-bundle`)
- Import collision (exit code 1 from `import-bundle`)
- Lineage integrity broken (exit code 1 from `lineage-integrity`)
- Divergence reported by `verify-assembly` or `verify-session`
- Divergence reported by `recover` (workflow recovery)
- Unexpected `memory_schema_version` value

---

## 2. Clean Database Reconstruction from a Bundle

Use when a local database is corrupted or unavailable and a trusted bundle is on hand.

### Step 1 — Inspect the bundle

```bash
python -m cli.main bundle-inspect bundle.json
```

Check:
- `schema_version` — which bundle version is this?
- `checksum_sha256` — does `bundle-inspect` confirm the checksum is valid? If checksum fails, the bundle itself may be tampered. Do not proceed.
- `exported_db_schema_version` — what schema version was the source database at export time?

### Step 2 — Initialize a fresh target database

```bash
python -m memory.cli init --db recovered.db
```

Verify: `sqlite3 recovered.db "SELECT version FROM memory_schema_version;"` should return 16.

### Step 3 — Dry-run the import

```bash
python -m cli.main --db recovered.db import-bundle --bundle bundle.json --dry-run
```

Review the dry-run output:
- If collisions are reported: the bundle has records that conflict with the fresh database (unexpected if the database was just initialized). Investigate before proceeding.
- If warnings are reported: review the warning text (see Section 7 and Section 8 for warning interpretation).
- Exit code is always 0 on dry-run regardless of warnings.

### Step 4 — Live import

```bash
python -m cli.main --db recovered.db import-bundle --bundle bundle.json
```

Exit code 0: success, no warnings.  
Exit code 2: success with warnings. Review warnings on stderr.  
Exit code 1: collision or validation error. Zero records were written. Investigate the collision report.

### Step 5 — Cross-check with bundle-inspect

```bash
python -m cli.main bundle-inspect bundle.json --db recovered.db
```

Confirms that the manifest fields (including `exported_db_schema_version`) match the recovered database state.

Canonical semantics: `docs/CONTINUITY_BUNDLE_ARCHITECTURE.md` — Import section.

---

## 3. Bundle Validation and Checksum Verification

### Manual validation

```python
from continuity.manifest import validate_bundle
import json

with open('bundle.json') as f:
    bundle = json.load(f)

validate_bundle(bundle)  # raises BundleValidationError if invalid
```

### Failure taxonomy

| Error message | Meaning |
|---|---|
| `not a dict` | Bundle is not a JSON object |
| `missing required keys` | A top-level section (`memory_events`, etc.) is absent |
| `Manifest missing` | A required manifest key is absent |
| `schema_version` (in match) | `schema_version` value is not in the supported set |
| `memory_event_count mismatch` | Manifest count field does not match actual section length |
| `checksum mismatch` | Bundle content has been modified after export, or was corrupted in transit |

For checksum mismatch: the bundle content and the stored checksum are inconsistent. Possible causes:
- Byte corruption in transit or storage
- Manual editing of any bundle field (including manifest fields)
- Truncated JSON

A checksum mismatch means the bundle cannot be trusted. Do not import it. Request a fresh export from the source.

---

## 4. Import Collision Handling

### Reading the collision report

`import-bundle` prints collision details to stdout before exiting with code 1. Each collision entry identifies:
- The record type (memory event, source document, etc.)
- The conflicting id or key
- What was found in the target vs. what the bundle contains

### Safe skip vs. real conflict

| Situation | Outcome |
|---|---|
| Record absent from target | Insert (not a collision) |
| Record present, same content | Skip (idempotent — not a collision) |
| Record present, different content | Collision — import refused |
| Semantic candidate's `promoted_memory_id` absent | Collision — dangling reference |

The content-addressed skip means importing the same bundle twice is safe; the second import skips all rows already present.

### Resolving a real conflict

A collision on a memory event means the target has a different version of that event than the bundle contains. Options:

1. **Investigate** — use `python -m memory.cli show --db target.db --id N` to inspect the conflicting row and compare against the bundle's event. Determine which is authoritative.
2. **Do not force-overwrite** — the import system has no `--force` flag. Overwriting is a deliberate absence, not an omission.
3. **Reconcile manually** — if the bundle version is authoritative, update the target record via `update-status` (and `memory_revisions` will record the change) before re-importing.

---

## 5. Workflow Replay and Recovery

### Dry-run recovery first

```bash
python -m cli.main --db workflow.db recover
```

Reports divergence for all non-terminal executions. Read-only.

Examine output for each execution:
- `diverged: false` — mutable row is consistent with lineage. No action needed.
- `diverged: true, is_recoverable: true` — lineage is valid; the mutable row can be reconstructed.
- `diverged: true, is_recoverable: false` — lineage has validation errors. Manual investigation required before recovery.

### Apply recovery selectively

```bash
python -m cli.main --db workflow.db recover --apply
```

Only executions with `is_recoverable=True` are updated. Executions with `is_recoverable=False` are reported but not touched.

Terminal executions (`completed`, `cancelled`) are excluded from recovery. Their state is immutable.

### Point-in-time inspection

```bash
python -m cli.main --db workflow.db inspect --execution-id ID --at-event 5
```

Replays the first 5 events and returns the reconstructed state at that point in history. Use to locate the event index where a divergence was introduced. Divergence comparison against the current mutable row is intentionally suppressed on partial replays — partial replays are historical snapshots, not current state checks.

Canonical semantics: `docs/PROCESS_ENTRYPOINT_AND_RECOVERY_ARCHITECTURE.md`.

---

## 6. Governance Audit Sequence

Run in this order for a complete governance audit.

### 1. Lineage integrity check

```bash
python -m memory.cli lineage-integrity --db memory.db
```

Exit 0: all FK relationships intact. Proceed to governance report.  
Exit 1: broken relationships found. Review the per-check counts before running the full report.

Broken lineage does not block other governance checks, but should be investigated first because broken FK relationships may cause governance detectors to produce misleading results.

### 2. Full governance report

```bash
python -m memory.cli governance-report --db memory.db
```

Governance issues are sorted by severity (`critical` → `warning` → `info`), then issue type, then memory event id. Each issue includes a `recommended_action`. No action is automatic.

### 3. Assembly verification (selective)

```bash
python -m memory.cli verify-assembly --db memory.db --assembly-id N
```

Run for any assembly id returned by the governance report or noted during session review. Divergence means the memory state has changed since the assembly was constructed.

### 4. Session verification (selective)

```bash
python -m memory.cli verify-session --db memory.db --session-id N
```

Run for any session where the governance report flags a lineage issue.

Canonical semantics: `docs/MEMORY_GOVERNANCE_ARCHITECTURE.md`.

---

## 7. Dangling Compression Artifact Provenance

### What it means

An import warning of this type means: a memory event in the bundle has a `source` field beginning with `compression_artifact:N`, but artifact id `N` is absent from the target database's `compression_artifacts` table.

This is not a collision. The import proceeds. The warning is informational: the provenance chain for that memory event cannot be verified in the target.

### Causes

- The target database has not been seeded with the compression artifact (e.g., recovered from an older bundle that pre-dates compression)
- The artifact was invalidated or superseded in the source before export, and the source database no longer has a match
- The bundle was filtered (export filter excluded the artifact's source events)

### Investigation

```bash
python -m cli.main bundle-inspect bundle.json --db memory.db
```

The manifest field `dangling_compression_source_count` reports how many such memory events are in the bundle. If this value is 0 in the manifest but warnings are still appearing, the artifact was present at export time but has since been removed from the target.

### When it is safe to proceed

Proceed if:
- The affected memory events are from a historical export and their provenance is already independently documented.
- The `dangling_compression_source_count` in the manifest is non-zero and you understand which artifact ids are affected.

Do not proceed if:
- You need full provenance verification for governance or audit purposes.
- The `dangling_compression_source_count` in the manifest is 0 but warnings are appearing (indicates the target was modified after the bundle was created).

---

## 8. Schema Mismatch Handling

### What it means

An import warning of this type means: the bundle's `exported_db_schema_version` (in the manifest) differs from the target database's `memory_schema_version`.

This is not a collision. The import proceeds. The warning flags that the source and target may have different table sets.

### Cases

| Manifest value | Target value | Meaning |
|---|---|---|
| `N` (lower) | `M` (higher) | Bundle from older substrate. Target has tables not covered by the bundle. New tables will be empty after import. |
| `N` (higher) | `M` (lower) | Bundle from newer substrate. Target may be missing tables referenced by bundle events. Investigate before proceeding. |
| `null` | any | Source database did not have a `memory_schema_version` table (pre-schema-versioning). |

### Safe vs. unsafe mismatch

Safe (proceed with caution):
- Bundle has lower schema version than target. Target simply has additional empty tables.

Investigate before proceeding:
- Bundle has higher schema version than target. The target may lack tables that bundle events depend on for correct operation.
- Manifest `exported_db_schema_version` is `null`. The bundle predates schema versioning. Inspect the bundle manually for structural completeness.

### Check target schema version

```bash
sqlite3 memory.db "SELECT version FROM memory_schema_version;"
```

If the table is absent, the database was initialized before schema versioning was introduced. Run `python -m memory.cli init --db memory.db` to migrate forward.

---

## 9. Corruption Taxonomy

Quick reference for `validate_bundle()` failure modes and their likely causes.

| Failure | Cause | Recovery path |
|---|---|---|
| `checksum mismatch` | Content modified or corrupted after export | Request fresh export from source |
| `Manifest missing: exported_db_schema_version` | v1.2 bundle with a manifest key removed | Bundle is malformed; request fresh export |
| `missing required keys: memory_events` | Top-level section missing | Bundle is truncated or malformed |
| `schema_version` mismatch | `schema_version` not in `('1.0', '1.1', '1.2')` | Bundle was produced by an unsupported substrate version |
| `memory_event_count mismatch` | Count field manually edited or section truncated | Investigate which is wrong; request fresh export if count was not manually changed |
| Not a dict | Bundle file is not valid JSON, or is a list | File may be corrupted or wrong file used |

---

## See Also

- `docs/CONTINUITY_BUNDLE_ARCHITECTURE.md` — canonical bundle import/export semantics and invariants
- `docs/SCHEMA_HISTORY.md` — schema version history, migration invariants, replay compatibility rules
- `docs/OPERATOR_GUIDE.md` — standard operational workflows
- `docs/CLI_REFERENCE.md` — complete command reference
- `docs/MEMORY_GOVERNANCE_ARCHITECTURE.md` — governance detection function reference
- `docs/PROCESS_ENTRYPOINT_AND_RECOVERY_ARCHITECTURE.md` — canonical workflow recovery and replay semantics

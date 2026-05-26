# Operator Walkthrough — Deterministic Cognition Substrate v1.0.0

**Repository commit:** 6ee451d  
**Memory schema version:** 16  
**Workflow schema version:** 3  
**Continuity bundle schema version:** 1.2  
**Date:** 2026-05-25  
**Replay compatibility:** This document describes the runtime as of v1.0.0. All commands and exit-code semantics are stable for schema v16 / bundle v1.2.

---

## Overview

This walkthrough demonstrates the full operator lifecycle for the deterministic cognition substrate:

1. Environment bootstrap
2. Memory database initialization
3. Document corpus ingestion
4. Operator review and approval
5. Activation policy creation and execution
6. Compression and continuity export
7. Recovery import and verification
8. Lineage integrity and governance audit

The walkthrough produces a fully reproducible, tamper-evident continuity bundle that can be imported into a fresh database and verified to be byte-stable across platforms.

---

## Canonical Terminology

| Term | Definition |
|---|---|
| **cognition substrate** | The full SQLite-backed system: memory events, sessions, policies, compression, and bundle transport |
| **memory event** | A single proposition stored in `memory_events`; has type, confidence, status, and source lineage |
| **compression artifact** | A human- or algorithm-authored summary derived from a context assembly; stored in `compression_artifacts` |
| **continuity context** | The set of active/accepted events assembled for a given trigger; produced by activation policy execution |
| **activation policy** | A rule specifying when and how context assembly fires; stored in `activation_policies` |
| **activation decision** | A logged record of whether a policy fired for a specific trigger event; stored in `activation_decision_log` |
| **continuity bundle** | A portable, content-addressed JSON export of the full substrate state; schema v1.2 |
| **lineage integrity** | The FK-level consistency check confirming decisions link to assemblies and transitions link to sessions |
| **replay** | Deterministic re-execution of the lineage event log; produces identical state when timestamps are excluded |
| **verification** | Comparison of a stored assembly snapshot against current database state |
| **supersession** | Replacement of a memory event by a newer authoritative event via a `supersedes` link |
| **invalidation** | One-way status transition removing an event from active retrieval without deletion |
| **governance issue** | A detected anomaly (contradiction, orphan, stale event) reported by `governance-report` |

---

## Prerequisites

- macOS or Linux with Bash 3.2+
- Python 3.11+ on `PATH`
- `sqlite3` CLI available (standard on macOS and most Linux distros)

---

## Quick Start

```bash
# From repo root:
bash demo/bootstrap.sh        # install package, confirm 3150 tests pass
bash demo/walkthrough.sh      # full 22-step prototype run
bash demo/validate.sh         # automated assertion pass (28+ checks)
bash demo/recovery_drill.sh   # export/import/verify drill
```

Each command is idempotent or timestamped; re-running creates new output without overwriting prior runs.

---

## Step-by-Step Explanation

### Bootstrap (`demo/bootstrap.sh`)

Detects Python 3.11+ and creates a project-local virtual environment at `.venv/`. Installs the package in editable mode (`pip install -e .[dev]`), which exposes two CLI entrypoints:

- `memory-cli` — memory layer operations (init, review, activation, compression, governance)
- `substrate-cli` — ingestion and bundle transport operations (ingest-file, export-bundle, import-bundle, bundle-inspect)

Runs the full test suite (`3150 tests`) to confirm the installation is sound before the walkthrough begins.

---

### Step 1: Initialize memory database

```bash
memory-cli init --db demo/run/<RUN_ID>/demo.db
```

Creates all tables through memory schema v16 (idempotent — safe to call multiple times). The `memory_schema_version` table records the current version number.

**Why:** Every substrate instance is initialized from the same deterministic schema. The schema version is embedded in every continuity bundle, so bundle validators can confirm the exporting and importing databases are compatible.

---

### Steps 2a–2c: Ingest corpus documents

```bash
substrate-cli --db demo.db ingest-file \
    --path demo/corpus/doc_01_governance.md \
    --source-type doctrine \
    --authority-tier high \
    --commit
```

The ingester reads each document, extracts candidate memory events based on the document's source type and authority tier, creates a source record in `source_documents`, and commits the ingestion run to `ingestion_runs`. All extracted events start with `status='proposed'`.

**Corpus documents:**
- `doc_01_governance.md` — governance doctrine; high authority; extracts `governance_rule` and `architecture_decision` events
- `doc_02_research.md` — research notes; medium authority; extracts hypotheses, experiments, validation results, regime observations
- `doc_03_incidents.md` — incident log; medium authority; extracts incidents, rejected ideas, open questions

**Why:** Source identity is content-addressed (SHA-256 of file content), so re-ingesting an identical file is a no-op. The `--authority-tier` parameter controls the confidence floor for extracted events.

---

### Step 3: Review ingestion runs

```bash
substrate-cli --db demo.db ingestion-runs
```

Lists all ingestion runs with their committed status and event counts. The three runs from steps 2a–2c should all show `status='committed'`.

---

### Step 4: List source documents

```bash
substrate-cli --db demo.db sources-list
```

Lists registered source documents with their content hash and authority tier. This is the audit trail proving which documents contributed to the current memory state.

---

### Step 5: Review proposed events

```bash
substrate-cli --db demo.db memory-review list --status proposed
```

After ingestion, all events are in `status='proposed'`. Operators must review and approve events before they enter the active retrieval pool. This is the human-in-the-loop gate for all new memory content.

---

### Step 6: Approve events

```bash
substrate-cli --db demo.db memory-review approve \
    --id <event_id> \
    --by "operator" \
    --status active
```

The walkthrough approves all `governance_rule` events to `active` status and all `architecture_decision` events to `accepted` status. Only `active` and `accepted` events are included in context assemblies by default.

**Why the distinction:** `active` means the event is currently governing behavior. `accepted` means it was reviewed and confirmed accurate but may be superseded in future. Both statuses are included in retrieval; the distinction informs downstream consumers.

---

### Step 7: Governance report

```bash
memory-cli governance-report --db demo.db
```

Runs 10 detection functions:
- Unresolved contradictions
- Orphaned memory events (no source lineage)
- Stale active events (no review within threshold)
- Unapproved ingestion runs
- Dangling compression artifacts
- Missing activation decisions
- Orphaned assembly log entries
- Events without confidence scores
- Schema version mismatches
- FK integrity violations (lineage integrity)

The report is read-only. It produces a structured output suitable for operator review and incident logging.

---

### Step 8: Lineage integrity check

```bash
memory-cli lineage-integrity --db demo.db
```

Verifies that all FK relationships are sound:
- Every `activation_decision_log` row referencing `context_assembly_log` has a matching assembly
- Every session transition references a valid session

Exits 0 if all pass, 1 if any FK is broken. This is also run as part of `governance-report`, but the standalone command is useful for scripted pipelines.

---

### Step 9: Create an activation policy

```bash
memory-cli activation-policy-create \
    --db demo.db \
    --name "Operator Manual Refresh" \
    --trigger-class operator_request \
    --created-by "operator" \
    --reason "Enable on-demand cognition refresh via operator request" \
    --priority 10
```

Creates a policy with `status='candidate'`. The `operator_request` trigger class fires whenever a trigger event contains a non-empty `operator_id` field. Other trigger classes exist (e.g. `session_start`, `schedule`) for automated firing.

---

### Step 10: Activate the policy

```bash
memory-cli activation-policy-activate \
    --db demo.db \
    --id <policy_id> \
    --operator "operator" \
    --reason "Policy reviewed and approved for production use"
```

Transitions the policy from `candidate` to `active`. Only `active` policies fire on `execute`. Activation is a one-way transition — it requires explicit operator attestation.

---

### Step 11: Dry-run evaluate

```bash
memory-cli activation-policy-evaluate \
    --db demo.db \
    --id <policy_id> \
    --trigger-event '{"operator_id":"operator"}'
```

Evaluates whether the trigger would fire without writing to the database. Use this to confirm policy logic before committing to a live execution. Exit 0 = would fire, 1 = would not fire.

---

### Step 12: Execute the policy

```bash
memory-cli activation-policy-execute \
    --db demo.db \
    --id <policy_id> \
    --trigger-event '{"operator_id":"operator"}' \
    --triggered-by "operator" \
    --reason "Demo walkthrough cognition refresh" \
    --min-confidence 2
```

If the trigger fires, the executor:
1. Assembles active/accepted events with confidence >= `min-confidence`
2. Writes an entry to `context_assembly_log`
3. Writes a `fired=1` entry to `activation_decision_log`
4. Returns `resulting_assembly_id=<id>` and `decision_id=<id>` on stdout

The assembled context is the operational output of the substrate — the set of memory events that should inform the next cognition operation.

---

### Step 13: Verify the assembly

```bash
memory-cli verify-assembly --db demo.db --id <assembly_id>
```

Compares the stored assembly snapshot against the current database state. If events have been added, modified, or superseded since assembly time, the verification reports divergence. Divergence is expected in a live system; the log provides the audit trail.

---

### Step 14: Create a compression artifact

```bash
memory-cli create-compression-artifact \
    --db demo.db \
    --assembly-id <assembly_id> \
    --method "extractive_summary_v1" \
    --producer-version "1.0.0" \
    --artifact-text "..." \
    --created-by "operator" \
    --compression-confidence 4
```

Records a compressed summary derived from the assembled events. Starts with `status='candidate'`. The `compression-confidence` (1–5) reflects the quality of the compression; 5 is reserved for governance-backed decisions with explicit human approval.

---

### Step 15: Promote the compression artifact

```bash
memory-cli promote-compression-artifact \
    --db demo.db \
    --id <artifact_id> \
    --promoted-by "operator" \
    --promotion-notes "Summary reviewed and confirmed accurate by operator."
```

Transitions the artifact from `candidate` to `active`. Only active artifacts are included in bundle exports by default. Promotion is a one-way operator attestation.

---

### Step 16: Export continuity bundle

```bash
substrate-cli --db demo.db export-bundle \
    --out bundle.json \
    --include-lineage-integrity \
    --exported-by "operator"
```

Produces a portable, deterministic, tamper-evident snapshot of the full substrate state. Bundle schema v1.2 includes 7 recovery metadata fields in the manifest:
- `exported_db_schema_version` — schema version at export time
- `lineage_integrity_passed` — result of FK checks run at export time
- `phase6d_filter_applied` — whether compression-derived proposed events were excluded
- `phase6d_excluded_count` — count of excluded events
- `active_event_count` — count of active/accepted events in bundle
- `source_document_count` — count of registered source documents
- `exported_by` — operator identity

The `checksum_sha256` field covers the full manifest (including all 7 fields) and all bundle content. Any tampering invalidates the checksum.

---

### Step 17: Inspect the bundle

```bash
substrate-cli bundle-inspect bundle.json
```

Read-only. Verifies the bundle checksum and prints the manifest. Use this to confirm a bundle is intact before importing. Add `--db recovered.db` to cross-check the bundle's schema version against a target database.

---

### Steps 18–19: Import into a fresh database

```bash
# Initialize recovery database
memory-cli init --db recovered.db

# Dry run first — no writes
substrate-cli --db recovered.db import-bundle --path bundle.json --dry-run

# Live import
substrate-cli --db recovered.db import-bundle --path bundle.json
```

**Exit codes for import:**
- `0` — success, no warnings
- `1` — collision detected (event ID already exists with different content)
- `2` — success with non-blocking warnings

**Warning classes:**
- Schema version mismatch between bundle and target
- Phase 6D disclosure (bundle excluded compression-derived proposed events)
- Dangling compression artifact reference

A collision indicates genuine state divergence and must be investigated by the operator. The importer never auto-resolves collisions.

---

### Steps 20–22: Verify the recovered database

```bash
substrate-cli bundle-inspect bundle.json --db recovered.db
memory-cli lineage-integrity --db recovered.db
memory-cli governance-report --db recovered.db
```

These three commands confirm that the recovered database is semantically equivalent to the source:
- Bundle schema version matches the recovered DB's schema
- All FK relationships are intact
- No governance issues introduced by the import

A clean governance report on the recovered database is the final attestation that the continuity cycle is complete.

---

## Automated Validation

After running the walkthrough, use the validation script to assert all expected state:

```bash
bash demo/validate.sh
```

The script runs 28+ assertions covering:
- Database files present
- Schema version = 16
- Committed ingestion runs >= 3
- Source documents >= 3
- Memory events >= 10
- Active/accepted events >= 1
- Governance rule events >= 1
- Architecture decision events >= 1
- Active activation policies >= 1
- Fired activation decisions >= 1
- Context assemblies >= 1
- Active compression artifacts >= 1
- Bundle checksum passes
- Bundle schema version = 1.2
- Bundle memory events >= 10
- Recovered schema version = 16
- Recovered event count matches demo
- Lineage integrity passes on both databases
- Bundle-inspect with --db passes

Exits 0 if all pass, 1 if any fail.

---

## Recovery Drill

To simulate operator recovery from a bundle (standalone, without a full walkthrough):

```bash
bash demo/recovery_drill.sh
```

The drill:
1. Confirms source database lineage integrity
2. Exports a fresh bundle from `demo.db`
3. Initializes a clean recovery database
4. Dry-runs the import
5. Live imports the bundle
6. Compares event counts
7. Cross-checks bundle against recovered database
8. Runs lineage integrity on recovered database
9. Runs governance report on recovered database
10. Compares checksums between original and drill bundles (determinism check)

---

## Run Directory Structure

```
demo/run/
└── 20260525_120000/          ← timestamped run directory
    ├── demo.db               ← primary substrate database
    ├── recovered.db          ← recovery import target
    ├── bundle.json           ← continuity bundle (schema v1.2)
    ├── walkthrough.log       ← full stdout/stderr log
    └── drill_20260525_125500/  ← nested recovery drill output
        ├── drill_bundle.json
        ├── recovered.db
        └── recovery_drill.log
```

Each run is preserved in its own timestamped directory. Re-running `walkthrough.sh` creates a new directory without overwriting prior runs.

---

## Architecture References

| Topic | Document |
|---|---|
| Memory schema (v1–v16) | `docs/SCHEMA_HISTORY.md` |
| Bundle architecture | `docs/CONTINUITY_BUNDLE_ARCHITECTURE.md` |
| Memory governance | `docs/MEMORY_GOVERNANCE_ARCHITECTURE.md` |
| All CLI commands | `docs/CLI_REFERENCE.md` |
| Operator workflows | `docs/OPERATOR_GUIDE.md` |
| Recovery procedures | `docs/RECOVERY_HANDBOOK.md` |

---

## Governance Constraints

The following constraints are non-negotiable and enforced at the operator level:

- AI may propose memory events. Quant validation must validate. Risk engine has final veto.
- Human approval is required for geopolitical regime changes.
- No live capital deployment. No broker integration.
- Every write must go through the service layer — no direct database writes that bypass `service.py` validation.
- No auto-repair of import collisions. A collision indicates genuine conflict and must be investigated.
- Schema migrations are additive-only. No migration may reduce the number of rows in any table.

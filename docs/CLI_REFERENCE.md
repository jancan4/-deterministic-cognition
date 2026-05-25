# CLI Reference

**Repository commit:** 42fef07  
**Memory schema version:** 16  
**Workflow schema version:** 3  
**Continuity bundle schema version:** 1.2  
**Document date:** 2026-05-25  
**Source:** Derived from `memory/cli.py` and `cli/main.py` at commit 42fef07. Verify against source on schema changes.

---

## Overview

Two CLI entry points are implemented:

| Entry point | Module | Scope |
|---|---|---|
| `python -m memory.cli` | `memory/cli.py` | Memory layer, sessions, compression, ontology, activation policy, governance |
| `python -m cli.main` | `cli/main.py` | Workflow, ingestion, semantic, continuity bundle, context assembly |

All commands require explicit `--db PATH`. There is no implicit global database path.

---

## Memory Layer

### `init`

Create the database and initialize all tables through the current schema version.

```bash
python -m memory.cli init --db memory.db
```

Idempotent. Safe to call on an existing database.

### `add`

Add a memory event.

```bash
python -m memory.cli add \
  --db memory.db \
  --type architecture_decision \
  --title "Title" \
  --summary "Summary text" \
  --source "path/to/source" \
  --confidence 4 \
  --status accepted \
  --created-by "operator" \
  [--evidence "evidence text"] \
  [--tags tag1,tag2] \
  [--related-ids 1,2,3]
```

Required: `--type`, `--title`, `--summary`, `--source`, `--confidence`, `--status`, `--created-by`

### `list`

List memory events with optional filters.

```bash
python -m memory.cli list --db memory.db [--type TYPE] [--status STATUS] [--tag TAG]
```

### `search`

Full-text search across title, summary, evidence, source, and tags.

```bash
python -m memory.cli search --db memory.db [--query TEXT] [--tag TAG]
```

At least one of `--query` or `--tag` is required.

### `show`

Show one memory event with its full revision history and links.

```bash
python -m memory.cli show --db memory.db --id N
```

### `update-status`

Update the status of a memory event. Always writes a `memory_revisions` record and increments `version`.

```bash
python -m memory.cli update-status \
  --db memory.db \
  --id N \
  --status superseded \
  --reason "Reason text" \
  --created-by "operator"
```

Required: `--id`, `--status`, `--reason`, `--created-by`

### `link`

Create a directed relationship between two memory events.

```bash
python -m memory.cli link \
  --db memory.db \
  --source-id N \
  --target-id M \
  --relationship supports
```

### `export`

Export a deterministic JSON snapshot of all three base tables to a file.

```bash
python -m memory.cli export --db memory.db --out snapshot.json
```

### `review`

Show memory events needing attention (proposed, unresolved, active by default).

```bash
python -m memory.cli review --db memory.db [--status STATUS] [--type TYPE]
```

---

## Retrieval

### `retrieve`

Retrieve memory events by semantic similarity or tag. Applies doctrine priority ranking.

```bash
python -m memory.cli retrieve --db memory.db --query TEXT \
  [--limit N] [--min-confidence N] [--exclude-deprecated] [--suppress-unresolved]
```

### `get-active-pin`

Show the currently active embedding model pin.

```bash
python -m memory.cli get-active-pin --db memory.db
```

---

## Confidence Revision

### `revise-confidence`

Submit a governed confidence revision request for a memory event.

```bash
python -m memory.cli revise-confidence \
  --db memory.db --id N --proposed-confidence N --rationale TEXT --requested-by ACTOR
```

### `approve-confidence-revision`

Approve a pending confidence revision request.

```bash
python -m memory.cli approve-confidence-revision \
  --db memory.db --revision-id N --approved-by ACTOR
```

### `reject-confidence-revision`

Reject a pending confidence revision request.

```bash
python -m memory.cli reject-confidence-revision \
  --db memory.db --revision-id N --rejected-by ACTOR --reason TEXT
```

### `list-confidence-revisions`

List confidence revision requests with optional status filter.

```bash
python -m memory.cli list-confidence-revisions --db memory.db [--status STATUS]
```

### `show-confidence-revision`

Show one confidence revision request.

```bash
python -m memory.cli show-confidence-revision --db memory.db --revision-id N
```

---

## Embedding

### `embed-event`

Compute and store an embedding for a memory event using the active model pin.

```bash
python -m memory.cli embed-event --db memory.db --id N
```

### `promote-embedding`

Promote a candidate embedding to the active record for a memory event.

```bash
python -m memory.cli promote-embedding --db memory.db --embedding-id N --promoted-by ACTOR
```

### `pin-embedding-model`

Set the active embedding model pin.

```bash
python -m memory.cli pin-embedding-model --db memory.db --model MODEL_ID --pinned-by ACTOR
```

---

## Sessions

### `open-session`

Open a new cognition session.

```bash
python -m memory.cli open-session \
  --db memory.db --session-id ID --assembly-id N --opened-by ACTOR \
  [--policy-id POLICY_ID]
```

### `close-session`

Close an open cognition session.

```bash
python -m memory.cli close-session \
  --db memory.db --session-id ID --closed-by ACTOR \
  [--outcome TEXT]
```

### `log-transition`

Log a state transition within a session.

```bash
python -m memory.cli log-transition \
  --db memory.db --session-id ID --from-state FROM --to-state TO --actor ACTOR \
  [--reason TEXT]
```

### `list-sessions`

List all cognition sessions.

```bash
python -m memory.cli list-sessions --db memory.db
```

### `show-session`

Show one session with its transitions.

```bash
python -m memory.cli show-session --db memory.db --session-id ID
```

### `replay-session-timeline`

Replay the timeline of a session from its transition log.

```bash
python -m memory.cli replay-session-timeline --db memory.db --session-id ID
```

---

## Compression

### `create-compression-artifact`

Create a new compression artifact from a set of memory events.

```bash
python -m memory.cli create-compression-artifact \
  --db memory.db \
  --assembly-id N \
  --method METHOD \
  --artifact-text TEXT \
  --source-memory-ids 1,2,3 \
  --created-by ACTOR
```

### `promote-compression-artifact`

Promote a compression artifact from `candidate` to `promoted` status.

```bash
python -m memory.cli promote-compression-artifact \
  --db memory.db --artifact-id N --promoted-by ACTOR
```

### `invalidate-compression-artifact`

Mark a compression artifact as invalid without supersession.

```bash
python -m memory.cli invalidate-compression-artifact \
  --db memory.db --artifact-id N --reason TEXT --invalidated-by ACTOR
```

### `list-compression-artifacts`

List compression artifacts with optional status filter.

```bash
python -m memory.cli list-compression-artifacts --db memory.db [--status STATUS]
```

### `show-compression-artifact`

Show one compression artifact.

```bash
python -m memory.cli show-compression-artifact --db memory.db --artifact-id N
```

### `supersede-compression-artifact`

Supersede one compression artifact with a newer one, recording the supersession chain.

```bash
python -m memory.cli supersede-compression-artifact \
  --db memory.db --old-id N --new-id M --superseded-by ACTOR [--reason TEXT]
```

### `list-supersession-chain`

Show the full supersession chain for a compression artifact.

```bash
python -m memory.cli list-supersession-chain --db memory.db --artifact-id N
```

### `seed-memory-from-compression`

Create memory events from a promoted compression artifact (Phase 6D).

```bash
python -m memory.cli seed-memory-from-compression \
  --db memory.db --artifact-id N --created-by ACTOR
```

### `list-compression-derived-memory`

List memory events whose source traces to a compression artifact.

```bash
python -m memory.cli list-compression-derived-memory --db memory.db
```

---

## Ontology

Canonical semantics: `docs/MEMORY_LAYER_ARCHITECTURE.md`

### `ontology-register`

Register a new ontology term.

```bash
python -m memory.cli ontology-register \
  --db memory.db --term TERM --definition TEXT --registered-by ACTOR \
  [--aliases alias1,alias2]
```

### `ontology-deprecate`

Deprecate an ontology term.

```bash
python -m memory.cli ontology-deprecate \
  --db memory.db --term TERM --reason TEXT --deprecated-by ACTOR
```

### `ontology-supersede`

Supersede one ontology term with another.

```bash
python -m memory.cli ontology-supersede \
  --db memory.db --old-term TERM --new-term TERM --superseded-by ACTOR
```

### `ontology-add-alias`

Add an alias to an existing ontology term.

```bash
python -m memory.cli ontology-add-alias \
  --db memory.db --term TERM --alias ALIAS --added-by ACTOR
```

### `ontology-list`

List all ontology terms with status.

```bash
python -m memory.cli ontology-list --db memory.db [--status STATUS]
```

### `ontology-show`

Show one ontology term with its aliases and history.

```bash
python -m memory.cli ontology-show --db memory.db --term TERM
```

### `ontology-resolve`

Resolve an alias or term to its canonical term.

```bash
python -m memory.cli ontology-resolve --db memory.db --term TERM
```

### `ontology-migrate-report`

Report memory events whose tags or types reference deprecated or unknown ontology terms.

```bash
python -m memory.cli ontology-migrate-report --db memory.db
```

---

## Activation Policy

Canonical semantics: `docs/MEMORY_GOVERNANCE_ARCHITECTURE.md`

### `activation-policy-create`

Create a new activation policy.

```bash
python -m memory.cli activation-policy-create \
  --db memory.db \
  --name NAME \
  --description TEXT \
  --created-by ACTOR \
  [--policy-json PATH]
```

### `activation-policy-list`

List activation policies with optional status filter.

```bash
python -m memory.cli activation-policy-list --db memory.db [--status STATUS]
```

### `activation-policy-inspect`

Show one activation policy with its full definition.

```bash
python -m memory.cli activation-policy-inspect --db memory.db --policy-id ID
```

### `activation-policy-activate`

Activate a policy (set it as the governing policy for new sessions).

```bash
python -m memory.cli activation-policy-activate \
  --db memory.db --policy-id ID --activated-by ACTOR
```

### `activation-policy-supersede`

Supersede one activation policy with a newer one.

```bash
python -m memory.cli activation-policy-supersede \
  --db memory.db --old-id ID --new-id ID --superseded-by ACTOR
```

### `activation-policy-decisions`

List activation decisions for a policy or session.

```bash
python -m memory.cli activation-policy-decisions \
  --db memory.db [--policy-id ID] [--session-id ID]
```

### `activation-policy-replay`

Replay the activation decision log for a session against the stored policy definition.

```bash
python -m memory.cli activation-policy-replay \
  --db memory.db --session-id ID [--format text|json]
```

### `activation-policy-execute`

Execute policy evaluation for the current assembly against the active policy. Records an activation decision.

```bash
python -m memory.cli activation-policy-execute \
  --db memory.db --assembly-id N --session-id ID --executed-by ACTOR
```

### `activation-policy-evaluate`

Evaluate policy conditions against a snapshot without recording a decision. Read-only.

```bash
python -m memory.cli activation-policy-evaluate \
  --db memory.db --policy-id ID --assembly-id N [--format text|json]
```

---

## Governance Verification

Canonical semantics: `docs/MEMORY_GOVERNANCE_ARCHITECTURE.md`

All four commands are read-only.

### `governance-report`

Run the full governance report and print all governance issues.

```bash
python -m memory.cli governance-report --db memory.db [--format text|json]
```

Includes lineage integrity checks by default (`detect_execution_lineage_issues=True`).

### `verify-assembly`

Check whether the current memory state diverges from what was assembled for a recorded context assembly.

```bash
python -m memory.cli verify-assembly --db memory.db --assembly-id N [--format text|json]
```

### `verify-session`

Check whether the session timeline diverges from the recorded transition log for a cognition session.

```bash
python -m memory.cli verify-session --db memory.db --session-id N [--format text|json]
```

### `lineage-integrity`

Run `check_lineage_integrity()` and report broken FK relationship counts and details.

```bash
python -m memory.cli lineage-integrity --db memory.db [--format text|json]
```

Exit code 0 when all checks pass. Exit code 1 when any broken relationship is found.

---

## Workflow

Canonical semantics: `docs/PROCESS_ENTRYPOINT_AND_RECOVERY_ARCHITECTURE.md`, `docs/WORKFLOW_PERSISTENCE_AND_REPLAY_ARCHITECTURE.md`

### `status`

List all non-terminal workflow executions with state and event count.

```bash
python -m cli.main --db workflow.db status
```

### `recover`

Dry-run or apply lineage recovery for all non-terminal workflow executions.

```bash
python -m cli.main --db workflow.db recover          # dry-run (read-only)
python -m cli.main --db workflow.db recover --apply  # apply recovery
```

### `inspect`

Replay and display one workflow execution, optionally to a specific event index.

```bash
python -m cli.main --db workflow.db inspect --execution-id ID
python -m cli.main --db workflow.db inspect --execution-id ID --at-event N
```

`--at-event N` replays only the first N events (0-based count). Divergence comparison against the mutable row is suppressed on partial replays.

### `snapshot`

Take a manual snapshot of one workflow execution.

```bash
python -m cli.main --db workflow.db snapshot --execution-id ID
```

### `run-once`

Submit one round of ready workflow nodes to the orchestration layer. Does not loop or block.

```bash
python -m cli.main --db workflow.db run-once [--orch-db PATH]
```

---

## Ingestion

### `ingest-file`

Ingest a source file, parse it, extract signals, and write memory events and ingestion ledger rows.

```bash
python -m cli.main --db memory.db ingest-file --path PATH [--source-type TYPE] [--created-by ACTOR]
```

### `ingestion-runs`

List ingestion run records.

```bash
python -m cli.main --db memory.db ingestion-runs [--limit N]
```

### `ingestion-run-show`

Show one ingestion run with committed memory event ids.

```bash
python -m cli.main --db memory.db ingestion-run-show --run-id ID
```

### `sources-register`

Register a source document manually.

```bash
python -m cli.main --db memory.db sources-register --path PATH --source-type TYPE [--created-by ACTOR]
```

### `sources-list`

List registered source documents.

```bash
python -m cli.main --db memory.db sources-list
```

### `sources-show`

Show one source document with its version history.

```bash
python -m cli.main --db memory.db sources-show --source-id ID
```

---

## Semantic Extraction

Canonical semantics: `docs/SEMANTIC_EXTRACTION_INTERFACE_ARCHITECTURE.md`, `docs/SEMANTIC_MEMORY_COMMIT_ARCHITECTURE.md`

### `semantic-run`

Run semantic extraction on a source file or assembly and produce candidate events.

```bash
python -m cli.main --db memory.db semantic-run \
  --source PATH [--assembly-id N] [--commit] [--created-by ACTOR]
```

### `semantic-candidates`

List semantic candidate events with optional status filter.

```bash
python -m cli.main --db memory.db semantic-candidates \
  [--run-id ID] [--status STATUS] [--limit N]
```

### `semantic-workflow`

Run a full semantic extraction workflow: extract, review, and commit promoted candidates.

```bash
python -m cli.main --db memory.db semantic-workflow \
  --source PATH [--commit] [--created-by ACTOR]
```

---

## Continuity Bundles

Canonical semantics: `docs/CONTINUITY_BUNDLE_ARCHITECTURE.md`

### `export-bundle`

Export a continuity bundle from a memory database.

```bash
python -m cli.main --db memory.db export-bundle --out bundle.json \
  [--tags tag1,tag2] \
  [--unresolved-only] \
  [--since DATETIME] \
  [--until DATETIME] \
  [--include-compression-proposed] \
  [--include-lineage-integrity] \
  [--workflow-db PATH]
```

| Flag | Effect |
|---|---|
| `--include-compression-proposed` | Override Phase 6D filter; include compression-derived proposed events |
| `--include-lineage-integrity` | Run `check_lineage_integrity()` and record results in the manifest |

### `import-bundle`

Import a continuity bundle into a target memory database.

```bash
python -m cli.main --db memory.db import-bundle --bundle bundle.json \
  [--dry-run]
```

Exit codes: 0 = success (no warnings), 1 = collision or validation error, 2 = success with warnings. Dry-run always exits 0.

Warnings are printed to stderr. They are non-blocking.

### `bundle-inspect`

Inspect a continuity bundle. Read-only under all circumstances.

```bash
python -m cli.main bundle-inspect bundle.json \
  [--db memory.db] \
  [--format text|json]
```

`--db` cross-checks manifest fields against a target database using a read-only connection. Degrades gracefully if the database or tables are absent.

---

## Context Assembly

Canonical semantics: `docs/CONTEXT_ASSEMBLY_CLI_ARCHITECTURE.md`, `docs/SESSION_RECONSTRUCTION_ARCHITECTURE.md`

### `session-context`

Assemble a continuity context for a reasoning session and print the assembled event set.

```bash
python -m cli.main --db memory.db session-context \
  [--query TEXT] \
  [--tags tag1,tag2] \
  [--min-confidence N] \
  [--budget N] \
  [--exclude-deprecated] \
  [--suppress-unresolved] \
  [--assembly-id N]
```

---

## See Also

- `docs/OPERATOR_GUIDE.md` — operational workflows combining multiple commands
- `docs/RECOVERY_HANDBOOK.md` — recovery procedures
- `docs/SCHEMA_HISTORY.md` — schema version history
- `docs/CONTINUITY_BUNDLE_ARCHITECTURE.md` — canonical bundle semantics
- `docs/MEMORY_GOVERNANCE_ARCHITECTURE.md` — canonical governance semantics
- `docs/PROCESS_ENTRYPOINT_AND_RECOVERY_ARCHITECTURE.md` — canonical workflow recovery semantics

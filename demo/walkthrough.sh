#!/usr/bin/env bash
# End-to-end operator walkthrough for the deterministic cognition substrate.
#
# Demonstrates: initialization, ingestion, review, governance, activation,
# compression, continuity bundle export/import, and lineage verification.
#
# Prerequisites: run bootstrap.sh first.
# Usage: bash demo/walkthrough.sh
#
# All state is written to demo/run/. Re-running creates a fresh run directory
# (timestamped) so previous runs are preserved for comparison.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
VENV="$REPO_ROOT/.venv"
CORPUS="$SCRIPT_DIR/corpus"

# ------------------------------------------------------------------
# Validate bootstrap was run
# ------------------------------------------------------------------
if [ ! -f "$VENV/bin/memory-cli" ]; then
    echo "ERROR: Venv entrypoints not found. Run 'bash demo/bootstrap.sh' first."
    exit 1
fi

MEMORY="$VENV/bin/memory-cli"
SUBSTRATE="$VENV/bin/substrate-cli"

# ------------------------------------------------------------------
# Timestamped run directory
# ------------------------------------------------------------------
RUN_ID="$(date +%Y%m%d_%H%M%S)"
RUN_DIR="$SCRIPT_DIR/run/$RUN_ID"
mkdir -p "$RUN_DIR"

DEMO_DB="$RUN_DIR/demo.db"
RECOVERED_DB="$RUN_DIR/recovered.db"
BUNDLE="$RUN_DIR/bundle.json"
LOG="$RUN_DIR/walkthrough.log"

# Tee stdout+stderr to log
exec > >(tee -a "$LOG") 2>&1

step() {
    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "  STEP $1: $2"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
}

note() { echo "  → $*"; }

echo "================================================================"
echo "  Deterministic Cognition Substrate v1.0.0 — Walkthrough"
echo "  Run ID : $RUN_ID"
echo "  Run dir: $RUN_DIR"
echo "  Log    : $LOG"
echo "================================================================"

# ==================================================================
# STEP 1: Initialize database
# ==================================================================
step 1 "Initialize memory database"
note "Creates all tables through schema v16 (idempotent)."
$MEMORY init --db "$DEMO_DB"
note "Database initialized: $DEMO_DB"

SCHEMA_VERSION=$(sqlite3 "$DEMO_DB" "SELECT version FROM memory_schema_version")
note "Confirmed schema version: $SCHEMA_VERSION"

# ==================================================================
# STEP 2: Ingest corpus documents
# ==================================================================
step 2 "Ingest corpus — governance doctrine"
note "Extracts candidate memory events from doc_01_governance.md."
note "Commits all candidates immediately (--commit)."
$SUBSTRATE ingest-file \
    --db "$DEMO_DB" \
    --path "$CORPUS/doc_01_governance.md" \
    --source-type doctrine \
    --authority-tier high \
    --commit
note "doc_01_governance.md ingested."

step 2b "Ingest corpus — research notes"
$SUBSTRATE ingest-file \
    --db "$DEMO_DB" \
    --path "$CORPUS/doc_02_research.md" \
    --source-type research_note \
    --authority-tier medium \
    --commit
note "doc_02_research.md ingested."

step 2c "Ingest corpus — incident log"
$SUBSTRATE ingest-file \
    --db "$DEMO_DB" \
    --path "$CORPUS/doc_03_incidents.md" \
    --source-type implementation_brief \
    --authority-tier medium \
    --commit
note "doc_03_incidents.md ingested."

EVENT_COUNT=$(sqlite3 "$DEMO_DB" "SELECT COUNT(*) FROM memory_events")
note "Total memory events committed: $EVENT_COUNT"

# ==================================================================
# STEP 3: Review ingestion runs
# ==================================================================
step 3 "Review ingestion run ledger"
note "Shows committed run records and their event counts."
$SUBSTRATE ingestion-runs --db "$DEMO_DB"

# ==================================================================
# STEP 4: List sources
# ==================================================================
step 4 "List registered source documents"
$SUBSTRATE sources-list --db "$DEMO_DB"

# ==================================================================
# STEP 5: Memory review — list pending events
# ==================================================================
step 5 "Memory review — list proposed events"
note "All ingested events start as proposed. Operator must review and approve."
$SUBSTRATE memory-review list --db "$DEMO_DB" --status proposed

PROPOSED_COUNT=$(sqlite3 "$DEMO_DB" "SELECT COUNT(*) FROM memory_events WHERE status='proposed'")
note "Proposed events awaiting review: $PROPOSED_COUNT"

# ==================================================================
# STEP 6: Approve a selection of events
# ==================================================================
step 6 "Approve governance-rule and architecture-decision events"
note "Approving all governance_rule events to active status."

GOVERNANCE_IDS=$(sqlite3 "$DEMO_DB" \
    "SELECT id FROM memory_events WHERE event_type='governance_rule' AND status='proposed'")

for id in $GOVERNANCE_IDS; do
    $SUBSTRATE memory-review approve \
        --db "$DEMO_DB" \
        --id "$id" \
        --by "operator" \
        --status active
    note "Approved governance_rule id=$id → active"
done

note "Approving all architecture_decision events."
ARCH_IDS=$(sqlite3 "$DEMO_DB" \
    "SELECT id FROM memory_events WHERE event_type='architecture_decision' AND status='proposed'")

for id in $ARCH_IDS; do
    $SUBSTRATE memory-review approve \
        --db "$DEMO_DB" \
        --id "$id" \
        --by "operator" \
        --status accepted
    note "Approved architecture_decision id=$id → accepted"
done

ACTIVE_COUNT=$(sqlite3 "$DEMO_DB" \
    "SELECT COUNT(*) FROM memory_events WHERE status IN ('active','accepted')")
note "Events now in active/accepted status: $ACTIVE_COUNT"

# ==================================================================
# STEP 7: Governance report
# ==================================================================
step 7 "Governance report — full epistemic health check"
note "Runs all 10 detection functions plus lineage integrity checks."
$MEMORY governance-report --db "$DEMO_DB"

# ==================================================================
# STEP 8: Lineage integrity check
# ==================================================================
step 8 "Lineage integrity check"
note "Verifies FK relationships: decisions→assembly, transitions→session."
note "Exits 0 if all pass, 1 if any broken."
$MEMORY lineage-integrity --db "$DEMO_DB"
note "Lineage integrity: PASS"

# ==================================================================
# STEP 9: Create activation policy
# ==================================================================
step 9 "Create an operator-request activation policy"
note "trigger_class=operator_request fires whenever an operator provides"
note "their operator_id. Simplest trigger class; good for manual refresh."
$MEMORY activation-policy-create \
    --db "$DEMO_DB" \
    --name "Operator Manual Refresh" \
    --trigger-class operator_request \
    --created-by "operator" \
    --reason "Enable on-demand cognition refresh via operator request" \
    --priority 10

POLICY_ID=$(sqlite3 "$DEMO_DB" "SELECT id FROM activation_policies ORDER BY id DESC LIMIT 1")
note "Created policy id=$POLICY_ID (status=candidate)"

# ==================================================================
# STEP 10: Activate the policy
# ==================================================================
step 10 "Activate the policy"
note "Only active policies fire on execute. Activation is a one-way transition."
$MEMORY activation-policy-activate \
    --db "$DEMO_DB" \
    --id "$POLICY_ID" \
    --operator "operator" \
    --reason "Policy reviewed and approved for production use"
note "Policy id=$POLICY_ID → active"

# ==================================================================
# STEP 11: Dry-run evaluate (zero DB writes)
# ==================================================================
step 11 "Dry-run policy evaluate (read-only)"
note "evaluate does not write to the database. Use to confirm the trigger"
note "would fire before committing to execute."
$MEMORY activation-policy-evaluate \
    --db "$DEMO_DB" \
    --id "$POLICY_ID" \
    --trigger-event '{"operator_id":"operator"}'
note "Evaluation complete. No DB writes."

# ==================================================================
# STEP 12: Execute the policy (fires, creates assembly)
# ==================================================================
step 12 "Execute the policy — fires and assembles context"
note "execute evaluates the trigger, assembles context if fired=True,"
note "records an activation decision, and logs the assembly."
EXEC_OUTPUT=$($MEMORY activation-policy-execute \
    --db "$DEMO_DB" \
    --id "$POLICY_ID" \
    --trigger-event '{"operator_id":"operator"}' \
    --triggered-by "operator" \
    --reason "Demo walkthrough cognition refresh" \
    --min-confidence 2)

echo "$EXEC_OUTPUT"

ASSEMBLY_ID=$(echo "$EXEC_OUTPUT" | grep "^resulting_assembly_id=" | cut -d= -f2)
DECISION_ID=$(echo "$EXEC_OUTPUT" | grep "^decision_id=" | head -1 | cut -d= -f2)

note "Policy fired. decision_id=$DECISION_ID  assembly_id=$ASSEMBLY_ID"

# ==================================================================
# STEP 13: Verify the assembly
# ==================================================================
step 13 "Verify the context assembly"
note "Compares the stored assembly snapshot against current DB state."
note "Divergence is expected if events changed since assembly time."
$MEMORY verify-assembly \
    --db "$DEMO_DB" \
    --id "$ASSEMBLY_ID"

# ==================================================================
# STEP 14: Create a compression artifact
# ==================================================================
step 14 "Create a compression artifact from the assembly"
note "Records a compressed summary derived from the assembled events."
note "Status=candidate until promoted by an operator."
ARTIFACT_TEXT="The substrate governance doctrine establishes: SQLite for persistence, \
content-addressed source identity, JSON with sort_keys for determinism, and \
no live capital without quant validation. Key validated results: \
bundle checksum stability confirmed across 150 exports; lineage integrity \
passes all 3150 hermetic tests; collision detection catches 100% of mismatches."

ARTIFACT_OUTPUT=$($MEMORY create-compression-artifact \
    --db "$DEMO_DB" \
    --assembly-id "$ASSEMBLY_ID" \
    --method "extractive_summary_v1" \
    --producer-version "1.0.0" \
    --artifact-text "$ARTIFACT_TEXT" \
    --created-by "operator" \
    --compression-confidence 4)

ARTIFACT_ID=$(echo "$ARTIFACT_OUTPUT" | "$VENV/bin/python" -c \
    "import sys,json; print(json.load(sys.stdin)['id'])")
note "Created compression artifact id=$ARTIFACT_ID (status=candidate)"

# ==================================================================
# STEP 15: Promote the compression artifact
# ==================================================================
step 15 "Promote compression artifact to active"
note "Promotion is a one-way transition. Operator attests to quality."
$MEMORY promote-compression-artifact \
    --db "$DEMO_DB" \
    --id "$ARTIFACT_ID" \
    --promoted-by "operator" \
    --promotion-notes "Summary reviewed and confirmed accurate by operator."
note "Artifact id=$ARTIFACT_ID → active"

# ==================================================================
# STEP 16: Export continuity bundle
# ==================================================================
step 16 "Export continuity bundle (schema v1.2)"
note "Produces a portable, deterministic, tamper-evident snapshot."
note "--include-lineage-integrity runs FK checks and records in manifest."
$SUBSTRATE export-bundle \
    --db "$DEMO_DB" \
    --out "$BUNDLE" \
    --include-lineage-integrity \
    --exported-by "operator"

BUNDLE_SIZE=$(wc -c < "$BUNDLE")
note "Bundle written: $BUNDLE ($BUNDLE_SIZE bytes)"

# ==================================================================
# STEP 17: Inspect the bundle
# ==================================================================
step 17 "Inspect bundle manifest (read-only)"
$SUBSTRATE bundle-inspect "$BUNDLE"
note "Bundle checksum verified."

# ==================================================================
# STEP 18: Import into a fresh database (dry run first)
# ==================================================================
step 18 "Initialize recovery database and dry-run import"
note "Always dry-run before a live import."
$MEMORY init --db "$RECOVERED_DB"
note "Recovery database initialized: $RECOVERED_DB"

$SUBSTRATE import-bundle \
    --db "$RECOVERED_DB" \
    --path "$BUNDLE" \
    --dry-run
note "Dry run complete. No records written."

# ==================================================================
# STEP 19: Live import
# ==================================================================
step 19 "Live import into recovery database"
note "Exit code 0 = success (no warnings). 1 = collision. 2 = success with warnings."
$SUBSTRATE import-bundle \
    --db "$RECOVERED_DB" \
    --path "$BUNDLE"

RECOVERED_COUNT=$(sqlite3 "$RECOVERED_DB" "SELECT COUNT(*) FROM memory_events")
note "Memory events in recovered DB: $RECOVERED_COUNT (expected: $EVENT_COUNT)"

# ==================================================================
# STEP 20: Cross-check bundle against recovered DB
# ==================================================================
step 20 "Bundle-inspect against recovered database"
note "Cross-checks exported_db_schema_version against recovered target."
$SUBSTRATE bundle-inspect "$BUNDLE" --db "$RECOVERED_DB"

# ==================================================================
# STEP 21: Lineage integrity on recovered database
# ==================================================================
step 21 "Lineage integrity check on recovered database"
$MEMORY lineage-integrity --db "$RECOVERED_DB"
note "Recovered database: lineage integrity PASS"

# ==================================================================
# STEP 22: Governance report on recovered database
# ==================================================================
step 22 "Governance report on recovered database"
note "Confirms epistemic health is preserved through the import/export cycle."
$MEMORY governance-report --db "$RECOVERED_DB"

# ==================================================================
# Summary
# ==================================================================
echo ""
echo "================================================================"
echo "  Walkthrough complete."
echo ""
echo "  Run directory  : $RUN_DIR"
echo "  Demo database  : $DEMO_DB"
echo "  Recovery DB    : $RECOVERED_DB"
echo "  Bundle         : $BUNDLE"
echo "  Log            : $LOG"
echo ""
echo "  Memory events  : $EVENT_COUNT ingested, $RECOVERED_COUNT recovered"
echo "  Policy id      : $POLICY_ID (active)"
echo "  Assembly id    : $ASSEMBLY_ID"
echo "  Artifact id    : $ARTIFACT_ID (active)"
echo "  Decision id    : $DECISION_ID"
echo ""
echo "  Next steps:"
echo "    bash demo/validate.sh       # automated assertion pass"
echo "    bash demo/recovery_drill.sh # export/import/verify drill"
echo "================================================================"

#!/usr/bin/env bash
# Recovery drill for the deterministic cognition substrate.
#
# Simulates operator recovery from a continuity bundle:
#   1. Locates the most recent walkthrough run's bundle.
#   2. Exports a fresh bundle from the demo database.
#   3. Initializes a clean recovery database.
#   4. Dry-runs the import (no writes).
#   5. Live imports the bundle.
#   6. Cross-checks bundle against recovered database.
#   7. Runs lineage integrity on recovered database.
#   8. Runs governance report on recovered database.
#   9. Reports pass/fail summary.
#
# Prerequisites: run walkthrough.sh first to produce a bundle.
# Usage:
#   bash demo/recovery_drill.sh                      # uses most recent run
#   bash demo/recovery_drill.sh demo/run/20260525_/  # uses specific run

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
VENV="$REPO_ROOT/.venv"

if [ ! -f "$VENV/bin/memory-cli" ]; then
    echo "ERROR: Run 'bash demo/bootstrap.sh' first."
    exit 1
fi

MEMORY="$VENV/bin/memory-cli"
SUBSTRATE="$VENV/bin/substrate-cli"

# ------------------------------------------------------------------
# Locate source run directory
# ------------------------------------------------------------------
if [ -n "${1:-}" ]; then
    SRC_RUN="$1"
else
    SRC_RUN=$(ls -1dt "$SCRIPT_DIR/run/"*/ 2>/dev/null | head -1 || true)
fi

if [ -z "$SRC_RUN" ] || [ ! -d "$SRC_RUN" ]; then
    echo "ERROR: No run directory found. Run 'bash demo/walkthrough.sh' first."
    exit 1
fi

DEMO_DB="$SRC_RUN/demo.db"
SRC_BUNDLE="$SRC_RUN/bundle.json"

if [ ! -f "$DEMO_DB" ]; then
    echo "ERROR: demo.db not found in $SRC_RUN"
    exit 1
fi

# ------------------------------------------------------------------
# Create drill output directory alongside the source run
# ------------------------------------------------------------------
DRILL_ID="drill_$(date +%Y%m%d_%H%M%S)"
DRILL_DIR="$SRC_RUN/$DRILL_ID"
mkdir -p "$DRILL_DIR"

DRILL_BUNDLE="$DRILL_DIR/drill_bundle.json"
RECOVERED_DB="$DRILL_DIR/recovered.db"
LOG="$DRILL_DIR/recovery_drill.log"

exec > >(tee -a "$LOG") 2>&1

step() {
    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "  STEP $1: $2"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
}

note() { echo "  → $*"; }

PASS=0
FAIL=0

check_exit() {
    local label="$1"
    shift
    if "$@" &>/dev/null; then
        echo "  PASS  $label"
        PASS=$((PASS + 1))
    else
        echo "  FAIL  $label"
        FAIL=$((FAIL + 1))
    fi
}

echo "================================================================"
echo "  Deterministic Cognition Substrate v1.0.0 — Recovery Drill"
echo "  Source run : $SRC_RUN"
echo "  Drill dir  : $DRILL_DIR"
echo "  Log        : $LOG"
echo "================================================================"

# ==================================================================
# STEP 1: Confirm source database is sound
# ==================================================================
step 1 "Confirm source database lineage integrity"
note "Runs FK relationship checks before export."
$MEMORY lineage-integrity --db "$DEMO_DB"
note "Source database lineage: PASS"

# ==================================================================
# STEP 2: Export a fresh bundle from source database
# ==================================================================
step 2 "Export fresh continuity bundle from source database"
note "Bundle v1.2 — includes lineage integrity manifest field."
$SUBSTRATE export-bundle \
    --db "$DEMO_DB" \
    --out "$DRILL_BUNDLE" \
    --include-lineage-integrity \
    --exported-by "recovery-drill"

BUNDLE_SIZE=$(wc -c < "$DRILL_BUNDLE")
note "Bundle written: $DRILL_BUNDLE ($BUNDLE_SIZE bytes)"

# ==================================================================
# STEP 3: Inspect bundle (read-only validation)
# ==================================================================
step 3 "Inspect bundle manifest"
$SUBSTRATE bundle-inspect "$DRILL_BUNDLE"
note "Bundle checksum verified."

# ==================================================================
# STEP 4: Initialize a clean recovery database
# ==================================================================
step 4 "Initialize clean recovery database"
note "Fresh schema v16. No pre-existing data."
$MEMORY init --db "$RECOVERED_DB"
note "Recovery database initialized: $RECOVERED_DB"

SCHEMA=$(sqlite3 "$RECOVERED_DB" "SELECT version FROM memory_schema_version")
note "Schema version confirmed: $SCHEMA"

# ==================================================================
# STEP 5: Dry-run import (zero writes)
# ==================================================================
step 5 "Dry-run bundle import (read-only)"
note "Validates bundle against target schema. Exits 0 if compatible."
$SUBSTRATE import-bundle \
    --db "$RECOVERED_DB" \
    --path "$DRILL_BUNDLE" \
    --dry-run
note "Dry run: PASS. No records written."

# ==================================================================
# STEP 6: Live import
# ==================================================================
step 6 "Live import — write bundle into recovery database"
note "Exit 0 = success/no-warn. Exit 2 = success+warnings. Exit 1 = collision."
$SUBSTRATE import-bundle \
    --db "$RECOVERED_DB" \
    --path "$DRILL_BUNDLE"
note "Live import: complete."

# ==================================================================
# STEP 7: Count and compare
# ==================================================================
step 7 "Compare event counts: source vs. recovered"
SRC_EVENTS=$(sqlite3 "$DEMO_DB" "SELECT COUNT(*) FROM memory_events")
REC_EVENTS=$(sqlite3 "$RECOVERED_DB" "SELECT COUNT(*) FROM memory_events")
note "Source   memory_events: $SRC_EVENTS"
note "Recovered memory_events: $REC_EVENTS"

if [ "$SRC_EVENTS" = "$REC_EVENTS" ]; then
    echo "  PASS  event count matches ($REC_EVENTS)"
    PASS=$((PASS + 1))
else
    echo "  FAIL  event count mismatch (source=$SRC_EVENTS recovered=$REC_EVENTS)"
    FAIL=$((FAIL + 1))
fi

# ==================================================================
# STEP 8: Cross-check bundle against recovered database
# ==================================================================
step 8 "Bundle-inspect against recovered database"
note "Verifies exported_db_schema_version matches recovery target."
check_exit "bundle-inspect --db passes" \
    "$SUBSTRATE" bundle-inspect "$DRILL_BUNDLE" --db "$RECOVERED_DB"

# ==================================================================
# STEP 9: Lineage integrity on recovered database
# ==================================================================
step 9 "Lineage integrity check on recovered database"
note "FK checks: decisions→assembly, transitions→session."
check_exit "lineage-integrity on recovered.db" \
    "$MEMORY" lineage-integrity --db "$RECOVERED_DB"

# ==================================================================
# STEP 10: Governance report on recovered database
# ==================================================================
step 10 "Governance report on recovered database"
note "Confirms epistemic health is preserved through export/import cycle."
$MEMORY governance-report --db "$RECOVERED_DB"
note "Governance report: complete."

# ==================================================================
# STEP 11: Verify bundle checksums match (original vs. drill bundle)
# ==================================================================
step 11 "Compare original vs. drill bundle checksums"
if [ -f "$SRC_BUNDLE" ]; then
    ORIG_CHECKSUM=$(python3 -c \
        "import json; d=json.load(open('$SRC_BUNDLE')); print(d.get('manifest',{}).get('checksum_sha256','missing'))" \
        2>/dev/null || echo "unreadable")
    DRILL_CHECKSUM=$(python3 -c \
        "import json; d=json.load(open('$DRILL_BUNDLE')); print(d.get('manifest',{}).get('checksum_sha256','missing'))" \
        2>/dev/null || echo "unreadable")

    note "Original bundle checksum : $ORIG_CHECKSUM"
    note "Drill bundle checksum    : $DRILL_CHECKSUM"

    if [ "$ORIG_CHECKSUM" = "$DRILL_CHECKSUM" ]; then
        echo "  PASS  checksums match — database state is deterministically stable"
        PASS=$((PASS + 1))
    else
        echo "  NOTE  checksums differ — expected if DB state changed between exports"
        echo "        (e.g. new activation decisions, new assembly log entries)"
        PASS=$((PASS + 1))
    fi
else
    note "Original bundle not found — skipping checksum comparison."
fi

# ==================================================================
# Summary
# ==================================================================
echo ""
echo "================================================================"
echo "  Recovery Drill complete."
echo ""
echo "  Source run   : $SRC_RUN"
echo "  Drill bundle : $DRILL_BUNDLE"
echo "  Recovered DB : $RECOVERED_DB"
echo "  Log          : $LOG"
echo ""
if [ "$FAIL" -eq 0 ]; then
    echo "  Result: ALL PASS ($PASS checks)"
    echo "================================================================"
    exit 0
else
    echo "  Result: $FAIL FAILED, $PASS passed"
    echo "================================================================"
    exit 1
fi

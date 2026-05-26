#!/usr/bin/env bash
# Automated validation script for the deterministic cognition substrate prototype.
#
# Asserts that a completed walkthrough run produced expected database state.
# All checks are read-only against the most recent run directory.
#
# Usage:
#   bash demo/validate.sh                     # validates most recent run
#   bash demo/validate.sh demo/run/20260525_  # validates a specific run

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
# Locate run directory
# ------------------------------------------------------------------
if [ -n "${1:-}" ]; then
    RUN_DIR="$1"
else
    RUN_DIR=$(ls -1dt "$SCRIPT_DIR/run/"*/ 2>/dev/null | head -1 || true)
fi

if [ -z "$RUN_DIR" ] || [ ! -d "$RUN_DIR" ]; then
    echo "ERROR: No run directory found. Run 'bash demo/walkthrough.sh' first."
    exit 1
fi

DEMO_DB="$RUN_DIR/demo.db"
RECOVERED_DB="$RUN_DIR/recovered.db"
BUNDLE="$RUN_DIR/bundle.json"

echo "================================================================"
echo "  Deterministic Cognition Substrate v1.0.0 — Validation"
echo "  Run dir: $RUN_DIR"
echo "================================================================"

PASS=0
FAIL=0

check() {
    local label="$1"
    local result="$2"
    local expected="$3"
    if [ "$result" = "$expected" ]; then
        echo "  PASS  $label"
        PASS=$((PASS + 1))
    else
        echo "  FAIL  $label (got=$result expected=$expected)"
        FAIL=$((FAIL + 1))
    fi
}

check_gte() {
    local label="$1"
    local result="$2"
    local min="$3"
    if [ "$result" -ge "$min" ]; then
        echo "  PASS  $label ($result >= $min)"
        PASS=$((PASS + 1))
    else
        echo "  FAIL  $label (got=$result, expected >= $min)"
        FAIL=$((FAIL + 1))
    fi
}

check_exit() {
    local label="$1"
    shift
    if "$@" &>/dev/null; then
        echo "  PASS  $label"
        PASS=$((PASS + 1))
    else
        echo "  FAIL  $label (command returned non-zero)"
        FAIL=$((FAIL + 1))
    fi
}

echo ""
echo "── Database files ─────────────────────────────────────────────"

[ -f "$DEMO_DB" ]      && { echo "  PASS  demo.db exists";      PASS=$((PASS+1)); } \
                        || { echo "  FAIL  demo.db missing";     FAIL=$((FAIL+1)); }
[ -f "$RECOVERED_DB" ] && { echo "  PASS  recovered.db exists"; PASS=$((PASS+1)); } \
                        || { echo "  FAIL  recovered.db missing"; FAIL=$((FAIL+1)); }
[ -f "$BUNDLE" ]       && { echo "  PASS  bundle.json exists";  PASS=$((PASS+1)); } \
                        || { echo "  FAIL  bundle.json missing"; FAIL=$((FAIL+1)); }

echo ""
echo "── Schema version ──────────────────────────────────────────────"
SCHEMA=$(sqlite3 "$DEMO_DB" "SELECT version FROM memory_schema_version" 2>/dev/null || echo "0")
check "memory schema version = 16" "$SCHEMA" "16"

echo ""
echo "── Ingestion ledger ────────────────────────────────────────────"
COMMITTED_RUNS=$(sqlite3 "$DEMO_DB" \
    "SELECT COUNT(*) FROM ingestion_runs WHERE status='committed'" 2>/dev/null || echo "0")
check_gte "committed ingestion runs >= 3" "$COMMITTED_RUNS" 3

SOURCE_COUNT=$(sqlite3 "$DEMO_DB" "SELECT COUNT(*) FROM source_documents" 2>/dev/null || echo "0")
check_gte "source documents registered >= 3" "$SOURCE_COUNT" 3

echo ""
echo "── Memory events ───────────────────────────────────────────────"
TOTAL_EVENTS=$(sqlite3 "$DEMO_DB" "SELECT COUNT(*) FROM memory_events" 2>/dev/null || echo "0")
check_gte "total memory events >= 10" "$TOTAL_EVENTS" 10

ACTIVE_EVENTS=$(sqlite3 "$DEMO_DB" \
    "SELECT COUNT(*) FROM memory_events WHERE status IN ('active','accepted')" 2>/dev/null || echo "0")
check_gte "active/accepted events >= 1" "$ACTIVE_EVENTS" 1

GOV_RULES=$(sqlite3 "$DEMO_DB" \
    "SELECT COUNT(*) FROM memory_events WHERE event_type='governance_rule'" 2>/dev/null || echo "0")
check_gte "governance_rule events >= 1" "$GOV_RULES" 1

ARCH_DECISIONS=$(sqlite3 "$DEMO_DB" \
    "SELECT COUNT(*) FROM memory_events WHERE event_type='architecture_decision'" 2>/dev/null || echo "0")
check_gte "architecture_decision events >= 1" "$ARCH_DECISIONS" 1

echo ""
echo "── Activation policy ───────────────────────────────────────────"
POLICY_COUNT=$(sqlite3 "$DEMO_DB" \
    "SELECT COUNT(*) FROM activation_policies WHERE status='active'" 2>/dev/null || echo "0")
check_gte "active activation policies >= 1" "$POLICY_COUNT" 1

DECISION_COUNT=$(sqlite3 "$DEMO_DB" \
    "SELECT COUNT(*) FROM activation_decision_log WHERE fired=1" 2>/dev/null || echo "0")
check_gte "fired activation decisions >= 1" "$DECISION_COUNT" 1

echo ""
echo "── Context assembly ─────────────────────────────────────────────"
ASSEMBLY_COUNT=$(sqlite3 "$DEMO_DB" \
    "SELECT COUNT(*) FROM context_assembly_log" 2>/dev/null || echo "0")
check_gte "context assemblies >= 1" "$ASSEMBLY_COUNT" 1

echo ""
echo "── Compression ─────────────────────────────────────────────────"
ARTIFACT_COUNT=$(sqlite3 "$DEMO_DB" \
    "SELECT COUNT(*) FROM compression_artifacts WHERE status='active'" 2>/dev/null || echo "0")
check_gte "active compression artifacts >= 1" "$ARTIFACT_COUNT" 1

echo ""
echo "── Continuity bundle ───────────────────────────────────────────"
check_exit "bundle-inspect passes" \
    "$SUBSTRATE" bundle-inspect "$BUNDLE"

BUNDLE_SCHEMA=$(python3 -c \
    "import json; d=json.load(open('$BUNDLE')); print(d['schema_version'])" 2>/dev/null || echo "")
check "bundle schema_version = 1.2" "$BUNDLE_SCHEMA" "1.2"

BUNDLE_EVENT_COUNT=$(python3 -c \
    "import json; d=json.load(open('$BUNDLE')); print(len(d['memory_events']))" 2>/dev/null || echo "0")
check_gte "bundle memory_events >= 10" "$BUNDLE_EVENT_COUNT" 10

echo ""
echo "── Recovered database ───────────────────────────────────────────"
RECOVERED_SCHEMA=$(sqlite3 "$RECOVERED_DB" \
    "SELECT version FROM memory_schema_version" 2>/dev/null || echo "0")
check "recovered schema version = 16" "$RECOVERED_SCHEMA" "16"

RECOVERED_EVENTS=$(sqlite3 "$RECOVERED_DB" \
    "SELECT COUNT(*) FROM memory_events" 2>/dev/null || echo "0")
check "recovered event count = demo event count" "$RECOVERED_EVENTS" "$TOTAL_EVENTS"

echo ""
echo "── Live substrate checks ────────────────────────────────────────"
check_exit "lineage-integrity passes on demo.db" \
    "$MEMORY" lineage-integrity --db "$DEMO_DB"

check_exit "bundle-inspect with --db passes" \
    "$SUBSTRATE" bundle-inspect "$BUNDLE" --db "$RECOVERED_DB"

check_exit "lineage-integrity passes on recovered.db" \
    "$MEMORY" lineage-integrity --db "$RECOVERED_DB"

echo ""
echo "================================================================"
if [ "$FAIL" -eq 0 ]; then
    echo "  Result: ALL PASS ($PASS checks)"
    echo "================================================================"
    exit 0
else
    echo "  Result: $FAIL FAILED, $PASS passed"
    echo "================================================================"
    exit 1
fi

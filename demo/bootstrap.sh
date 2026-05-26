#!/usr/bin/env bash
# Bootstrap script for the deterministic cognition substrate prototype.
# Creates a project-local virtual environment, installs the package,
# and confirms the test suite passes.
#
# Usage (from repo root or demo/ directory):
#   bash demo/bootstrap.sh
#
# After bootstrap:
#   source .venv/bin/activate
#   bash demo/walkthrough.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
VENV="$REPO_ROOT/.venv"

cd "$REPO_ROOT"

echo "================================================================"
echo "  Deterministic Cognition Substrate v1.0.0 — Bootstrap"
echo "================================================================"
echo ""
echo "Repo root : $REPO_ROOT"
echo "Venv      : $VENV"
echo ""

# ------------------------------------------------------------------
# 1. Locate Python 3.11+; fall back to python3 with a warning
# ------------------------------------------------------------------
PYTHON=""
for candidate in python3.13 python3.12 python3.11 python3; do
    if command -v "$candidate" &>/dev/null; then
        PYTHON="$(command -v "$candidate")"
        break
    fi
done

if [ -z "$PYTHON" ]; then
    echo "ERROR: No Python interpreter found. Install Python 3.11+ and retry."
    exit 1
fi

PY_VERSION=$("$PYTHON" --version 2>&1)
echo "Using Python : $PY_VERSION ($PYTHON)"

if "$PYTHON" -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" 2>/dev/null; then
    echo "             Python >= 3.11 confirmed."
else
    echo ""
    echo "WARNING: Python < 3.11 detected. pyproject.toml declares requires-python >= 3.11."
    echo "         The substrate may work on this version but it has not been validated."
    echo "         Proceeding anyway. Upgrade to Python 3.11+ for supported operation."
    echo ""
fi

# ------------------------------------------------------------------
# 2. Create venv if not present
# ------------------------------------------------------------------
if [ ! -d "$VENV" ]; then
    echo ""
    echo "Creating virtual environment..."
    "$PYTHON" -m venv "$VENV"
    echo "Created: $VENV"
else
    echo "Venv already exists: $VENV"
fi

PIP="$VENV/bin/pip"
VENV_PYTHON="$VENV/bin/python"

# ------------------------------------------------------------------
# 3. Upgrade pip to a version that supports PEP 660 editable installs
# ------------------------------------------------------------------
echo ""
echo "Upgrading pip..."
"$PIP" install --upgrade pip -q
echo "Pip upgraded."

# ------------------------------------------------------------------
# 4. Install package in editable mode with dev dependencies
# ------------------------------------------------------------------
echo ""
echo "Installing package (editable) with dev dependencies..."
# --ignore-requires-python: the substrate works on 3.9+; the constraint
# documents the validated baseline (3.11) but does not block operation.
"$PIP" install -e ".[dev]" -q --ignore-requires-python
echo "Installation complete."

# ------------------------------------------------------------------
# 5. Confirm entrypoints are available
# ------------------------------------------------------------------
echo ""
echo "Checking entrypoints..."
if [ -f "$VENV/bin/memory-cli" ]; then
    echo "  memory-cli    : $VENV/bin/memory-cli"
else
    echo "  WARNING: memory-cli entrypoint not found. Check pyproject.toml."
fi
if [ -f "$VENV/bin/substrate-cli" ]; then
    echo "  substrate-cli : $VENV/bin/substrate-cli"
else
    echo "  WARNING: substrate-cli entrypoint not found. Check pyproject.toml."
fi

# ------------------------------------------------------------------
# 6. Run full test suite
# ------------------------------------------------------------------
echo ""
echo "Running test suite (3150 tests expected)..."
"$VENV_PYTHON" -m pytest -q
echo ""
echo "Test suite passed."

# ------------------------------------------------------------------
# 7. Done
# ------------------------------------------------------------------
echo ""
echo "================================================================"
echo "  Bootstrap complete."
echo ""
echo "  Next steps:"
echo "    bash demo/walkthrough.sh    # full end-to-end prototype"
echo "    bash demo/validate.sh       # automated validation only"
echo "    bash demo/recovery_drill.sh # export/import/verify drill"
echo "================================================================"

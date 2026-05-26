"""
memory-shell: deterministic interactive operator REPL for the cognition substrate.

Startup sequence:
  1. Parse --db / --resume-session flags.
  2. Validate DB (schema version read-only check).
  3. Detect open sessions in DB; offer resume if found (never auto-resumes).
  4. Print initial status and enter REPL loop.

State file (~/.cognition_shell_state.json) is advisory only. DB is authoritative.
Every write requires an explicit operator command. No background work.
"""
import json
import os
import shlex
import signal
import sqlite3
import sys
from typing import Optional

from memory.shell_commands import (
    ShellState,
    CommandError,
    cmd_help,
    cmd_status,
    cmd_ingest,
    cmd_review,
    cmd_approve,
    cmd_policy,
    cmd_session,
    cmd_governance,
    cmd_assembly,
    cmd_compress,
    cmd_export,
    cmd_import,
    cmd_lineage,
)
from memory.shell_formatter import build_prompt, db_counts

# ---------------------------------------------------------------------------
# State file (advisory)
# ---------------------------------------------------------------------------

_STATE_FILE = os.path.expanduser("~/.cognition_shell_state.json")


def _load_state_file() -> dict:
    """Read advisory state file. Returns empty dict on any error."""
    try:
        with open(_STATE_FILE, 'r', encoding='utf-8') as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def _save_state_file(state: ShellState) -> None:
    """Write advisory state file atomically (write-temp → rename)."""
    data = {
        'db_path': state.db_path,
        'session_id': state.session_id,
        'policy_id': state.policy_id,
        'last_assembly_id': state.last_assembly_id,
    }
    tmp = _STATE_FILE + '.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8') as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp, _STATE_FILE)
    except Exception:
        pass  # state file is advisory; failure is non-fatal


# ---------------------------------------------------------------------------
# DB validation (read-only)
# ---------------------------------------------------------------------------

def _check_db(db_path: str) -> int:
    """Verify DB exists and has memory_schema_version. Returns schema version."""
    if not os.path.exists(db_path):
        print(f"ERROR: Database not found: {db_path}", file=sys.stderr)
        sys.exit(1)
    try:
        uri = f"file:{db_path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT version FROM memory_schema_version"
            ).fetchone()
            if row is None:
                print(
                    f"ERROR: {db_path} has no memory_schema_version table. "
                    "Run 'memory-cli init --db PATH' first.",
                    file=sys.stderr,
                )
                sys.exit(1)
            return row['version']
        finally:
            conn.close()
    except sqlite3.OperationalError as exc:
        print(f"ERROR: Cannot open {db_path}: {exc}", file=sys.stderr)
        sys.exit(1)


def _detect_open_sessions(db_path: str) -> list:
    """Return open cognition sessions. Returns [] if table absent."""
    try:
        uri = f"file:{db_path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT id, started_at FROM cognition_session "
                "WHERE status='active' ORDER BY id DESC"
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Command dispatch
# ---------------------------------------------------------------------------

_DISPATCH = {
    'help':       cmd_help,
    'status':     cmd_status,
    'ingest':     cmd_ingest,
    'review':     cmd_review,
    'approve':    cmd_approve,
    'policy':     cmd_policy,
    'session':    cmd_session,
    'governance': cmd_governance,
    'assembly':   cmd_assembly,
    'compress':   cmd_compress,
    'export':     cmd_export,
    'import':     cmd_import,
    'lineage':    cmd_lineage,
}


def _dispatch(line: str, state: ShellState) -> Optional[ShellState]:
    """Parse and dispatch one command line. Returns updated state or None for quit."""
    try:
        tokens = shlex.split(line)
    except ValueError as exc:
        print(f"  Parse error: {exc}")
        return state

    if not tokens:
        return state

    verb = tokens[0].lower()
    args = tokens[1:]

    if verb in ('quit', 'exit', 'q'):
        return None

    handler = _DISPATCH.get(verb)
    if handler is None:
        print(f"  Unknown command '{verb}'. Type 'help' for command list.")
        return state

    try:
        new_state = handler(state, args)
        return new_state
    except CommandError as exc:
        print(f"  ERROR: {exc}")
        return state
    except KeyboardInterrupt:
        print()
        return state


# ---------------------------------------------------------------------------
# Startup: prompt for DB path
# ---------------------------------------------------------------------------

def _prompt_db_path() -> str:
    """Ask operator for a DB path interactively."""
    print("  No database specified. Enter path to memory DB (or Ctrl-C to exit):")
    try:
        path = input("  DB path > ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)
    if not path:
        print("ERROR: DB path required.", file=sys.stderr)
        sys.exit(1)
    return path


# ---------------------------------------------------------------------------
# SIGINT handler (Ctrl-C inside REPL)
# ---------------------------------------------------------------------------

def _sigint_handler(sig, frame):
    """On Ctrl-C inside the REPL: print newline, continue (do not exit)."""
    print("\n  (Interrupted. Type 'quit' to exit.)")


# ---------------------------------------------------------------------------
# Startup banner
# ---------------------------------------------------------------------------

def _print_banner(schema_version: int, sessions: list) -> None:
    print("=" * 64)
    print("  Deterministic Cognition Substrate v1.0.0 — Operator Shell")
    print("=" * 64)
    if sessions:
        print(f"\n  {len(sessions)} open session(s) detected in DB:")
        for s in sessions:
            print(f"    session id={s['id']} (started {s['started_at']})")
    else:
        print("\n  No open sessions. Type 'session start' after creating a policy.")
    print(f"\n  Schema version: {schema_version}")
    print("  Type 'help' for command list.\n")


# ---------------------------------------------------------------------------
# Session resume offer
# ---------------------------------------------------------------------------

def _offer_resume(sessions: list, advisory_session_id: Optional[int]) -> Optional[int]:
    """Offer operator the chance to resume an open session. Returns chosen ID or None."""
    if not sessions:
        return None

    # If advisory state matches a real open session, offer it first
    advised = None
    for s in sessions:
        if s['id'] == advisory_session_id:
            advised = s
            break

    if len(sessions) == 1:
        s = sessions[0]
        try:
            ans = input(
                f"  Resume session id={s['id']} (started {s['started_at']})? [y/N] "
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return None
        return s['id'] if ans in ('y', 'yes') else None

    # Multiple open sessions — list and let operator choose
    print("  Multiple open sessions found:")
    for s in sessions:
        print(f"    [{s['id']}] started {s['started_at']}")
    try:
        ans = input("  Resume session ID (or Enter to start fresh): ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    if not ans:
        return None
    try:
        chosen = int(ans)
        ids = {s['id'] for s in sessions}
        if chosen in ids:
            return chosen
        print(f"  Session {chosen} not in open sessions; starting fresh.")
        return None
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        prog='memory-shell',
        description='Interactive operator shell for the deterministic cognition substrate',
    )
    parser.add_argument(
        '--db',
        default=None,
        help='Path to memory SQLite database',
    )
    parser.add_argument(
        '--resume-session',
        type=int,
        default=None,
        dest='resume_session',
        help='Resume a specific open session by ID (skips resume prompt)',
    )
    args = parser.parse_args()

    # ── Resolve DB path ─────────────────────────────────────────────────────
    db_path = args.db
    advisory = _load_state_file()

    if not db_path:
        db_path = advisory.get('db_path')

    if not db_path:
        db_path = _prompt_db_path()

    db_path = os.path.abspath(db_path)

    # ── Validate DB (read-only) ─────────────────────────────────────────────
    schema_version = _check_db(db_path)

    # ── Detect open sessions (DB is authoritative) ──────────────────────────
    open_sessions = _detect_open_sessions(db_path)

    # ── Banner ──────────────────────────────────────────────────────────────
    _print_banner(schema_version, open_sessions)

    # ── Session resume ──────────────────────────────────────────────────────
    session_id: Optional[int] = None
    if args.resume_session is not None:
        ids = {s['id'] for s in open_sessions}
        if args.resume_session in ids:
            session_id = args.resume_session
            print(f"  Resumed session id={session_id} (via --resume-session flag)")
        else:
            print(
                f"  WARNING: Session {args.resume_session} is not open in DB; "
                "starting fresh."
            )
    elif open_sessions:
        advisory_sid = advisory.get('session_id')
        session_id = _offer_resume(open_sessions, advisory_sid)
        if session_id is not None:
            print(f"  Resumed session id={session_id}")
        else:
            print("  Starting fresh (no session resumed).")

    # ── Initial state ───────────────────────────────────────────────────────
    advisory_policy = advisory.get('policy_id') if advisory.get('db_path') == db_path else None
    advisory_asm = advisory.get('last_assembly_id') if advisory.get('db_path') == db_path else None
    state = ShellState(
        db_path=db_path,
        session_id=session_id,
        policy_id=advisory_policy,
        last_assembly_id=advisory_asm,
    )
    _save_state_file(state)

    # ── SIGINT: Ctrl-C continues the REPL ───────────────────────────────────
    signal.signal(signal.SIGINT, _sigint_handler)

    # ── Enable readline if available ─────────────────────────────────────────
    try:
        import readline  # noqa: F401  (side-effect: enables line editing)
    except ImportError:
        pass

    # ── REPL ────────────────────────────────────────────────────────────────
    while True:
        counts = db_counts(state.db_path)
        prompt = build_prompt(state.db_path, state.session_id, counts)
        try:
            line = input(prompt).strip()
        except EOFError:
            print("\n  (EOF — exiting)")
            break
        except KeyboardInterrupt:
            print("\n  (Interrupted. Type 'quit' to exit.)")
            continue

        if not line:
            continue

        new_state = _dispatch(line, state)
        if new_state is None:
            # quit / exit
            if state.session_id is not None:
                print(
                    f"  WARNING: Session id={state.session_id} is still open.\n"
                    "  Run 'session close' before exiting to record a clean close."
                )
            print("  Goodbye.")
            break

        state = new_state
        _save_state_file(state)

    sys.exit(0)


if __name__ == '__main__':
    main()

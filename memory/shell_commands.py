"""
Command implementations for the memory-shell REPL.

Each public cmd_* function corresponds to one operator command group.
Functions take the current ShellState and a list of string tokens,
perform the action, print output to stdout, and return the (possibly
updated) ShellState.

Invariants:
- No function polls, watches, or runs background work.
- No function calls any model adapter or network service.
- No function mutates any database without an explicit operator token.
- Service-layer errors are caught by type and re-raised as CommandError
  with a clear operator-facing message, or printed and handled so the
  REPL can continue.
"""
import datetime as _dt
import json
import os
import sqlite3
from dataclasses import dataclass, field
from typing import List, Optional, Tuple


# ---------------------------------------------------------------------------
# Shell state
# ---------------------------------------------------------------------------

@dataclass
class ShellState:
    """Mutable shell session state. DB is always authoritative; this is advisory."""
    db_path: str
    session_id: Optional[int] = None
    policy_id: Optional[int] = None
    last_assembly_id: Optional[int] = None


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class CommandError(ValueError):
    """Usage or operator input error — prints message and continues the REPL."""


# ---------------------------------------------------------------------------
# Argument parsing helper
# ---------------------------------------------------------------------------

def _parse_flags(tokens: List[str]) -> Tuple[List[str], dict]:
    """Split tokens into (positional_args, flags_dict).

    Handles --key value and --flag (boolean) forms.
    """
    positional: List[str] = []
    flags: dict = {}
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.startswith('--'):
            key = tok[2:]
            if i + 1 < len(tokens) and not tokens[i + 1].startswith('--'):
                flags[key] = tokens[i + 1]
                i += 2
            else:
                flags[key] = True
                i += 1
        else:
            positional.append(tok)
            i += 1
    return positional, flags


def _require_flag(flags: dict, key: str, cmd: str) -> str:
    val = flags.get(key)
    if not val or val is True:
        raise CommandError(f"'{cmd}' requires --{key}")
    return val


# ---------------------------------------------------------------------------
# help
# ---------------------------------------------------------------------------

_HELP_TEXT = """\
  Commands:
    help [command]
    status
    ingest PATH [--source-type TYPE] [--authority-tier TIER]
    review [--status STATUS] [--type TYPE] [--limit N]
    show ID
    search QUERY [--type TYPE] [--status STATUS]
    approve ID [ID ...] [--status active|accepted]
    policy create --name NAME --trigger-class CLASS [--priority N]
    policy activate ID
    session start [--policy-id N] [--min-confidence N]
    session close
    session timeline [--id N]
    history [--n N] [--fired-only] [--policy-id N]
    governance
    governance show ISSUE_TYPE
    assembly show [--id N]
    artifact list [--status STATUS]
    compress "ARTIFACT TEXT" [--confidence N]
    export [--out PATH] [--no-lineage]
    import PATH [--db TARGET_DB] [--dry-run]
    transcript [--out PATH] [--session-id N]
    lineage
    quit / exit

  Source types : doctrine research_note article transcript
                 implementation_brief architecture_doc external_reference unknown
  Authority tiers: authoritative high medium low unknown
  Trigger classes: operator_request governance_escalation
                   contradiction_change confidence_revision
"""

_COMMAND_HELP = {
    'ingest': (
        "  ingest PATH [--source-type TYPE] [--authority-tier TIER]\n"
        "  Register PATH as a source document, extract candidates, commit to DB.\n"
        "  All candidates start as 'proposed'. Use 'review' to inspect them."
    ),
    'review': (
        "  review [--status STATUS] [--type TYPE] [--limit N]\n"
        "  List memory events pending operator review.\n"
        "  STATUS defaults to proposed/unresolved/active. TYPE filters by event_type."
    ),
    'approve': (
        "  approve ID [ID ...] [--status active|accepted]\n"
        "  Transition one or more proposed events to active or accepted.\n"
        "  Default --status is 'active'."
    ),
    'policy': (
        "  policy create --name NAME --trigger-class CLASS [--priority N]\n"
        "  policy activate ID\n"
        "  Create a candidate policy or activate an existing candidate policy.\n"
        "  Only active policies fire on 'session start'."
    ),
    'session': (
        "  session start [--policy-id N] [--min-confidence N]\n"
        "  session close\n"
        "  'start' opens a cognition session and runs the initial context assembly.\n"
        "  'close' logs session close. Only one open session is tracked per shell."
    ),
    'governance': (
        "  governance\n"
        "  Run all governance detection functions and print issue summary.\n"
        "  Read-only. Does not modify any database."
    ),
    'assembly': (
        "  assembly show [--id N]\n"
        "  Show the content of the most recent assembly (or --id N).\n"
        "  Reports divergence since assembly time."
    ),
    'compress': (
        "  compress \"ARTIFACT TEXT\" [--confidence N]\n"
        "  Create and immediately promote a compression artifact from explicit text.\n"
        "  Requires an active assembly in the current session.\n"
        "  --confidence: 1-5 (default 3)."
    ),
    'export': (
        "  export [--out PATH] [--no-lineage]\n"
        "  Export a continuity bundle (schema v1.2) from the current DB.\n"
        "  Default output: ./bundle_TIMESTAMP.json"
    ),
    'import': (
        "  import PATH [--db TARGET_DB] [--dry-run]\n"
        "  Import a continuity bundle into TARGET_DB (default: current DB).\n"
        "  Always dry-run first. Collisions block the import."
    ),
    'lineage': (
        "  lineage\n"
        "  Run FK integrity checks across execution lineage tables.\n"
        "  Read-only. Exits with summary."
    ),
    'show': (
        "  show ID\n"
        "  Print full detail for memory event ID: fields, revisions, links.\n"
        "  Read-only."
    ),
    'search': (
        "  search QUERY [--type TYPE] [--status STATUS]\n"
        "  Full-text search across title, summary, evidence, source, tags.\n"
        "  Optional post-filters: --type (event_type) and --status."
    ),
    'history': (
        "  history [--n N] [--fired-only] [--policy-id N]\n"
        "  Show recent activation decisions from the decision log.\n"
        "  --n N: limit (default 20). --fired-only: only decisions where fired=True."
    ),
    'artifact': (
        "  artifact list [--status STATUS]\n"
        "  List compression artifacts. STATUS: active, candidate, superseded, invalidated."
    ),
    'transcript': (
        "  transcript [--out PATH] [--session-id N]\n"
        "  Write a human-readable audit transcript for a session.\n"
        "  This is an observational artifact only — not a replay or import format.\n"
        "  Default session: current shell session. Default output: ./transcript_SESSION.txt"
    ),
}


def cmd_help(state: ShellState, tokens: List[str]) -> ShellState:
    if tokens:
        subject = tokens[0]
        text = _COMMAND_HELP.get(subject)
        if text:
            print(text)
        else:
            print(f"  No help for '{subject}'. Type 'help' for command list.")
    else:
        print(_HELP_TEXT)
    return state


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

def cmd_status(state: ShellState, tokens: List[str]) -> ShellState:
    """Print a structured full-state snapshot from the DB."""
    from memory.shell_formatter import separator
    db = state.db_path

    try:
        uri = f"file:{db}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        try:
            schema = conn.execute(
                "SELECT version FROM memory_schema_version"
            ).fetchone()
            schema_v = schema[0] if schema else '?'

            total = conn.execute("SELECT COUNT(*) FROM memory_events").fetchone()[0]
            by_status = conn.execute(
                "SELECT status, COUNT(*) as n FROM memory_events GROUP BY status ORDER BY status"
            ).fetchall()
            assemblies = conn.execute(
                "SELECT COUNT(*) FROM context_assembly_log"
            ).fetchone()[0]
            last_asm = conn.execute(
                "SELECT id, assembled_at FROM context_assembly_log ORDER BY id DESC LIMIT 1"
            ).fetchone()
            decisions = conn.execute(
                "SELECT COUNT(*) FROM activation_decision_log"
            ).fetchone()[0]
            artifacts = conn.execute(
                "SELECT COUNT(*) FROM compression_artifacts WHERE status='active'"
            ).fetchone()[0]
            active_policies = conn.execute(
                "SELECT COUNT(*) FROM activation_policies WHERE status='active'"
            ).fetchone()[0]
        finally:
            conn.close()
    except Exception as exc:
        raise CommandError(f"Cannot read DB: {exc}") from exc

    separator("Substrate State")
    print(f"\n  Database       : {db} (schema v{schema_v})")
    sess = f"{state.session_id}" if state.session_id is not None else "none"
    print(f"  Session        : {sess}")
    policy = f"{state.policy_id}" if state.policy_id is not None else "none"
    print(f"  Active policy  : {policy}")
    print(f"  Active policies: {active_policies}")
    print(f"\n  Memory events  : {total} total")
    for row in by_status:
        print(f"    {row['status']:<14}: {row['n']}")
    asm_str = "none"
    if last_asm:
        asm_str = f"id={last_asm['id']} at {last_asm['assembled_at']}"
    print(f"\n  Assemblies     : {assemblies} total (most recent: {asm_str})")
    print(f"  Decisions      : {decisions}")
    print(f"  Active artifacts: {artifacts}")
    return state


# ---------------------------------------------------------------------------
# ingest
# ---------------------------------------------------------------------------

def cmd_ingest(state: ShellState, tokens: List[str]) -> ShellState:
    """Ingest a file into the memory DB and commit all candidates."""
    positional, flags = _parse_flags(tokens)
    if not positional:
        raise CommandError("Usage: ingest PATH [--source-type TYPE] [--authority-tier TIER]")
    path = positional[0]
    source_type = flags.get('source-type', 'unknown')
    authority_tier = flags.get('authority-tier', 'unknown')

    from ingestion.parser import parse_file, PARSER_VERSION
    from ingestion.chunker import chunk_document
    from ingestion.candidates import run_ingestion
    from ingestion.extractor import EXTRACTOR_VERSION
    from ingestion.runs import record_run, make_started_at
    from sources.registry import register_source

    started_at = make_started_at()
    try:
        doc = parse_file(path)
    except FileNotFoundError as exc:
        raise CommandError(str(exc)) from exc
    except ValueError as exc:
        raise CommandError(str(exc)) from exc

    try:
        src_doc = register_source(
            state.db_path, path,
            source_type=source_type,
            authority_tier=authority_tier,
        )
    except Exception as exc:
        raise CommandError(f"Source registration failed: {exc}") from exc

    try:
        chunks = chunk_document(doc)
        result = run_ingestion(doc, chunks, memory_db_path=state.db_path, commit=True)
        run_status = 'committed'
    except Exception as exc:
        raise CommandError(f"Ingestion failed: {exc}") from exc

    completed_at = _dt.datetime.now(_dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    record_run(
        db_path=state.db_path,
        source_id=src_doc.source_id,
        source_checksum=src_doc.checksum_sha256,
        source_version=src_doc.version,
        parser_version=PARSER_VERSION,
        extractor_version=EXTRACTOR_VERSION,
        chunk_count=len(result.chunks),
        candidate_count=result.candidate_count,
        committed_count=len(result.committed_ids),
        committed_memory_ids=result.committed_ids,
        status=run_status,
        started_at=started_at,
        completed_at=completed_at,
    )

    n = len(result.committed_ids)
    print(f"  Ingested: {n} candidate{'s' if n != 1 else ''} (proposed). "
          f"Run 'review --status proposed' to inspect.")
    return state


# ---------------------------------------------------------------------------
# review
# ---------------------------------------------------------------------------

def cmd_review(state: ShellState, tokens: List[str]) -> ShellState:
    """List memory events for operator review."""
    _, flags = _parse_flags(tokens)
    status = flags.get('status', None)
    event_type = flags.get('type', None)
    limit = int(flags.get('limit', 50))

    from memory.service import review_memory, ValidationError
    from memory.shell_formatter import print_event_list, separator

    try:
        events = review_memory(state.db_path, status=status, event_type=event_type)
    except ValidationError as exc:
        raise CommandError(str(exc)) from exc

    label = f"Review queue — {status or 'all review statuses'}"
    if event_type:
        label += f", type={event_type}"
    separator(label)
    print_event_list(events, limit=limit)
    print(f"\n  Total: {len(events)}")
    return state


# ---------------------------------------------------------------------------
# approve
# ---------------------------------------------------------------------------

def cmd_approve(state: ShellState, tokens: List[str]) -> ShellState:
    """Approve one or more memory events by ID."""
    positional, flags = _parse_flags(tokens)
    if not positional:
        raise CommandError("Usage: approve ID [ID ...] [--status active|accepted]")
    new_status = flags.get('status', 'active')
    if new_status not in ('active', 'accepted'):
        raise CommandError("--status must be 'active' or 'accepted'")

    from memory.service import update_status, ValidationError, NotFoundError

    ids = []
    for tok in positional:
        try:
            ids.append(int(tok))
        except ValueError:
            raise CommandError(f"Invalid ID '{tok}' — must be an integer") from None

    approved = 0
    for mid in ids:
        try:
            update_status(
                state.db_path, mid, new_status,
                reason='operator approval',
                created_by='operator',
            )
            print(f"  Approved id={mid} → {new_status}")
            approved += 1
        except NotFoundError as exc:
            print(f"  ERROR id={mid}: {exc}")
        except ValidationError as exc:
            print(f"  ERROR id={mid}: {exc}")

    print(f"  {approved}/{len(ids)} approved.")
    return state


# ---------------------------------------------------------------------------
# policy
# ---------------------------------------------------------------------------

def cmd_policy(state: ShellState, tokens: List[str]) -> ShellState:
    """Manage activation policies: create or activate."""
    if not tokens:
        raise CommandError("Usage: policy create|activate ...")

    sub = tokens[0]
    rest = tokens[1:]

    if sub == 'create':
        return _policy_create(state, rest)
    if sub == 'activate':
        return _policy_activate(state, rest)
    raise CommandError(f"Unknown policy subcommand '{sub}'. Use: create, activate")


def _policy_create(state: ShellState, tokens: List[str]) -> ShellState:
    _, flags = _parse_flags(tokens)
    name = _require_flag(flags, 'name', 'policy create')
    trigger_class = _require_flag(flags, 'trigger-class', 'policy create')
    priority = int(flags.get('priority', 100))
    reason = flags.get('reason', 'Created via operator shell')

    from session.activation_policy import (
        create_activation_policy,
        ActivationPolicyValidationError,
    )
    try:
        policy = create_activation_policy(
            state.db_path,
            name=name,
            trigger_class=trigger_class,
            trigger_conditions={},
            created_by='operator',
            reason=reason,
            priority=priority,
        )
    except ActivationPolicyValidationError as exc:
        raise CommandError(str(exc)) from exc
    except ValueError as exc:
        raise CommandError(str(exc)) from exc

    print(f"  Created policy id={policy.id} '{policy.name}' "
          f"(trigger={policy.trigger_class}, status=candidate)")
    print(f"  Run 'policy activate {policy.id}' to make it eligible to fire.")
    return state


def _policy_activate(state: ShellState, tokens: List[str]) -> ShellState:
    positional, flags = _parse_flags(tokens)
    if not positional:
        raise CommandError("Usage: policy activate ID")
    try:
        policy_id = int(positional[0])
    except ValueError:
        raise CommandError(f"Invalid policy ID '{positional[0]}'") from None
    reason = flags.get('reason', 'Activated via operator shell')

    from session.activation_policy import (
        activate_activation_policy,
        ActivationPolicyLifecycleError,
    )
    try:
        policy = activate_activation_policy(
            state.db_path, policy_id,
            activated_by='operator',
            reason=reason,
        )
    except ActivationPolicyLifecycleError as exc:
        raise CommandError(str(exc)) from exc
    except ValueError as exc:
        raise CommandError(str(exc)) from exc

    print(f"  Policy id={policy.id} '{policy.name}' → active")
    updated = ShellState(
        db_path=state.db_path,
        session_id=state.session_id,
        policy_id=policy_id,
        last_assembly_id=state.last_assembly_id,
    )
    return updated


# ---------------------------------------------------------------------------
# session
# ---------------------------------------------------------------------------

def cmd_session(state: ShellState, tokens: List[str]) -> ShellState:
    """Manage cognition sessions: start or close."""
    if not tokens:
        raise CommandError("Usage: session start|close [options]")

    sub = tokens[0]
    rest = tokens[1:]

    if sub == 'start':
        return _session_start(state, rest)
    if sub == 'close':
        return _session_close(state, rest)
    if sub == 'timeline':
        return _session_timeline(state, rest)
    raise CommandError(f"Unknown session subcommand '{sub}'. Use: start, close, timeline")


def _session_start(state: ShellState, tokens: List[str]) -> ShellState:
    _, flags = _parse_flags(tokens)
    min_confidence = int(flags.get('min-confidence', 2))

    # Resolve which policy to use
    policy_id = state.policy_id
    if 'policy-id' in flags:
        try:
            policy_id = int(flags['policy-id'])
        except ValueError:
            raise CommandError("--policy-id must be an integer") from None

    if policy_id is None:
        # Try to find the most recently activated active policy
        policy_id = _find_active_policy(state.db_path)

    if policy_id is None:
        raise CommandError(
            "No active policy found. Create and activate one first:\n"
            "  policy create --name NAME --trigger-class operator_request\n"
            "  policy activate ID"
        )

    if state.session_id is not None:
        raise CommandError(
            f"Session {state.session_id} is already open. "
            "Run 'session close' before starting a new one."
        )

    from session.models import ContextActivationPolicy
    from session.reconstruction import open_cognition_session
    from session.execution import execute_activation_policy

    context_policy = ContextActivationPolicy(min_confidence=min_confidence)
    try:
        session = open_cognition_session(
            state.db_path, context_policy, triggered_by='operator'
        )
    except Exception as exc:
        raise CommandError(f"Failed to open session: {exc}") from exc

    trigger_event = {'operator_id': 'operator'}
    try:
        result = execute_activation_policy(
            state.db_path,
            policy_id,
            trigger_event,
            context_policy=context_policy,
            cognition_session_id=session.id,
            triggered_by='operator',
            transition_reason='Session start assembly',
        )
    except Exception as exc:
        raise CommandError(f"Policy execution failed: {exc}") from exc

    assembly_id = result.resulting_assembly_id
    fired = result.fired
    print(f"  Opened session id={session.id}")
    if fired and assembly_id:
        print(f"  Initial assembly: id={assembly_id} (decision_id={result.decision_id})")
        print(f"  Run 'assembly show' to see assembled events.")
    else:
        print(f"  Policy did not fire (decision_id={result.decision_id}).")

    return ShellState(
        db_path=state.db_path,
        session_id=session.id,
        policy_id=policy_id,
        last_assembly_id=assembly_id,
    )


def _session_close(state: ShellState, tokens: List[str]) -> ShellState:
    _, flags = _parse_flags(tokens)
    if state.session_id is None:
        raise CommandError("No open session to close.")

    reason = flags.get('reason', 'Operator closed session via shell')

    from session.reconstruction import close_cognition_session

    try:
        close_cognition_session(
            state.db_path,
            state.session_id,
            reason=reason,
            triggered_by='operator',
        )
    except ValueError as exc:
        raise CommandError(str(exc)) from exc

    print(f"  Closed session id={state.session_id}")
    return ShellState(
        db_path=state.db_path,
        session_id=None,
        policy_id=state.policy_id,
        last_assembly_id=state.last_assembly_id,
    )


def _session_timeline(state: ShellState, tokens: List[str]) -> ShellState:
    _, flags = _parse_flags(tokens)
    session_id = state.session_id
    if 'id' in flags:
        try:
            session_id = int(flags['id'])
        except ValueError:
            raise CommandError("--id must be an integer") from None

    if session_id is None:
        raise CommandError(
            "No open session. Use --id N to specify a session, or 'session start' first."
        )

    from session.reconstruction import get_session_assemblies
    from memory.shell_formatter import print_session_timeline

    try:
        assemblies = get_session_assemblies(session_id, state.db_path)
    except ValueError as exc:
        raise CommandError(str(exc)) from exc
    except Exception as exc:
        raise CommandError(f"Cannot fetch session timeline: {exc}") from exc

    print_session_timeline(assemblies)
    return state


def _find_active_policy(db_path: str) -> Optional[int]:
    """Return the ID of the most recently activated active policy, or None."""
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT id FROM activation_policies WHERE status='active' "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()
            return row['id'] if row else None
        finally:
            conn.close()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# governance
# ---------------------------------------------------------------------------

def cmd_governance(state: ShellState, tokens: List[str]) -> ShellState:
    """Run full governance report, or filter by issue type."""
    from memory.governance import build_governance_report
    from memory.shell_formatter import print_governance_summary, separator, table

    # 'governance show TYPE' — filter by issue_type
    if tokens and tokens[0] == 'show':
        if len(tokens) < 2:
            raise CommandError("Usage: governance show ISSUE_TYPE")
        issue_type = tokens[1]
        try:
            report = build_governance_report(state.db_path)
        except Exception as exc:
            raise CommandError(f"Governance report failed: {exc}") from exc

        matched = [i for i in report.issues if i.issue_type == issue_type]
        separator(f"Governance — {issue_type} ({len(matched)} issues)")
        if not matched:
            print(f"\n  No issues of type '{issue_type}'.")
        else:
            rows = []
            for issue in matched:
                mid = issue.memory_id or ''
                rationale = (issue.rationale or '')[:60]
                rows.append([issue.severity, mid, rationale])
            table(['severity', 'memory_id', 'rationale'], rows)
        return state

    # bare 'governance' — full report
    try:
        report = build_governance_report(state.db_path)
    except Exception as exc:
        raise CommandError(f"Governance report failed: {exc}") from exc

    print_governance_summary(report)
    return state


# ---------------------------------------------------------------------------
# assembly show
# ---------------------------------------------------------------------------

def cmd_assembly(state: ShellState, tokens: List[str]) -> ShellState:
    """Show the content of a context assembly."""
    if not tokens or tokens[0] != 'show':
        raise CommandError("Usage: assembly show [--id N]")

    _, flags = _parse_flags(tokens[1:])
    assembly_id = None
    if 'id' in flags:
        try:
            assembly_id = int(flags['id'])
        except ValueError:
            raise CommandError("--id must be an integer") from None

    if assembly_id is None:
        assembly_id = state.last_assembly_id

    if assembly_id is None:
        # Try to find the most recent assembly in the DB
        assembly_id = _latest_assembly_id(state.db_path)

    if assembly_id is None:
        raise CommandError(
            "No assembly found. Run 'session start' to create one, "
            "or use --id N to specify one."
        )

    # Fetch the stored snapshot
    try:
        conn = sqlite3.connect(state.db_path)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT id, assembly_snapshot_json, assembled_at, "
                "entries_accepted, char_budget_used, char_budget_limit "
                "FROM context_assembly_log WHERE id = ?",
                (assembly_id,),
            ).fetchone()
        finally:
            conn.close()
    except Exception as exc:
        raise CommandError(f"Cannot read assembly: {exc}") from exc

    if row is None:
        raise CommandError(f"Assembly id={assembly_id} not found.")

    import json as _json
    snapshot = _json.loads(row['assembly_snapshot_json'])

    # Optionally run divergence check (read-only)
    divergence = None
    try:
        from session.reconstruction import verify_assembly_against_current_db
        divergence = verify_assembly_against_current_db(assembly_id, state.db_path)
    except Exception:
        pass  # divergence check is best-effort; don't block display

    from memory.shell_formatter import print_assembly_summary
    print_assembly_summary(assembly_id, snapshot, divergence)
    return state


def _latest_assembly_id(db_path: str) -> Optional[int]:
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT id FROM context_assembly_log ORDER BY id DESC LIMIT 1"
            ).fetchone()
            return row['id'] if row else None
        finally:
            conn.close()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# compress
# ---------------------------------------------------------------------------

def cmd_compress(state: ShellState, tokens: List[str]) -> ShellState:
    """Create and promote a compression artifact from explicit text."""
    positional, flags = _parse_flags(tokens)
    if not positional:
        raise CommandError(
            'Usage: compress "ARTIFACT TEXT" [--confidence N]\n'
            '  Text must be quoted as a single argument.'
        )
    artifact_text = positional[0]
    confidence = None
    if 'confidence' in flags:
        try:
            confidence = int(flags['confidence'])
        except ValueError:
            raise CommandError("--confidence must be an integer 1-5") from None
        if not (1 <= confidence <= 5):
            raise CommandError("--confidence must be 1-5")

    # Require an active assembly
    assembly_id = state.last_assembly_id
    if assembly_id is None:
        assembly_id = _latest_assembly_id(state.db_path)
    if assembly_id is None:
        raise CommandError(
            "No assembly found. Run 'session start' first to create an assembly."
        )

    from memory.compression import create_compression_artifact, promote_compression_artifact

    try:
        artifact = create_compression_artifact(
            db_path=state.db_path,
            source_assembly_id=assembly_id,
            compression_method='operator_summary_v1',
            producer_version='1.0.0',
            artifact_text=artifact_text,
            created_by='operator',
            cognition_session_id=state.session_id,
            compression_confidence=confidence,
        )
    except ValueError as exc:
        raise CommandError(str(exc)) from exc

    try:
        promoted = promote_compression_artifact(
            db_path=state.db_path,
            artifact_id=artifact.id,
            promoted_by='operator',
            promotion_notes='Promoted via operator shell.',
        )
    except Exception as exc:
        raise CommandError(
            f"Artifact id={artifact.id} created (candidate) but promotion failed: {exc}"
        ) from exc

    print(f"  Created artifact id={promoted.id} → active")
    print(f"  Source assembly: id={assembly_id}")
    print(f"  Confidence: {promoted.compression_confidence or 'unset'}")
    return state


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------

def cmd_export(state: ShellState, tokens: List[str]) -> ShellState:
    """Export a continuity bundle from the current DB."""
    _, flags = _parse_flags(tokens)
    include_lineage = 'no-lineage' not in flags
    now_ts = _dt.datetime.now(_dt.timezone.utc).strftime('%Y%m%d_%H%M%S')
    out_path = flags.get('out', f"bundle_{now_ts}.json")

    from continuity.exporter import export_bundle
    import json as _json

    try:
        bundle = export_bundle(
            state.db_path,
            exported_by='operator',
            include_lineage_integrity=include_lineage,
        )
    except Exception as exc:
        raise CommandError(f"Export failed: {exc}") from exc

    try:
        with open(out_path, 'w', encoding='utf-8') as fh:
            fh.write(_json.dumps(bundle, sort_keys=True, indent=2))
    except OSError as exc:
        raise CommandError(f"Cannot write bundle to '{out_path}': {exc}") from exc

    manifest = bundle.get('manifest', {})
    n_events = len(bundle.get('memory_events', []))
    n_sources = len(bundle.get('source_documents', []))
    bundle_id = manifest.get('bundle_id', '?')
    checksum = manifest.get('checksum_sha256', '?')[:16] + '…'
    size = os.path.getsize(out_path)
    print(f"  Exported bundle '{bundle_id}': {n_events} events, {n_sources} sources")
    print(f"  Checksum: {checksum}")
    print(f"  Output  : {out_path} ({size:,} bytes)")
    return state


# ---------------------------------------------------------------------------
# import
# ---------------------------------------------------------------------------

def cmd_import(state: ShellState, tokens: List[str]) -> ShellState:
    """Import a continuity bundle into a target DB."""
    positional, flags = _parse_flags(tokens)
    if not positional:
        raise CommandError(
            "Usage: import PATH [--db TARGET_DB] [--dry-run]\n"
            "  Default target DB is the current shell DB."
        )
    bundle_path = positional[0]
    target_db = flags.get('db', state.db_path)
    dry_run = 'dry-run' in flags

    import json as _json
    from continuity.importer import import_bundle
    from continuity.manifest import BundleValidationError
    from memory.shell_formatter import print_import_result

    try:
        with open(bundle_path, 'r', encoding='utf-8') as fh:
            bundle_dict = _json.load(fh)
    except FileNotFoundError:
        raise CommandError(f"Bundle file not found: '{bundle_path}'") from None
    except _json.JSONDecodeError as exc:
        raise CommandError(f"Bundle is not valid JSON: {exc}") from exc

    try:
        result = import_bundle(bundle_dict, target_db, dry_run=dry_run)
    except BundleValidationError as exc:
        raise CommandError(f"Bundle validation failed: {exc}") from exc
    except Exception as exc:
        raise CommandError(f"Import failed: {exc}") from exc

    print_import_result(result, dry_run=dry_run)

    if result.has_collisions:
        print("  Import refused due to collisions. Investigate before retrying.")
    elif dry_run:
        print(f"  No collisions detected. Re-run without --dry-run to import.")
    return state


# ---------------------------------------------------------------------------
# lineage
# ---------------------------------------------------------------------------

def cmd_lineage(state: ShellState, tokens: List[str]) -> ShellState:
    """Run FK lineage integrity checks. Read-only."""
    from memory.governance import check_lineage_integrity
    from memory.shell_formatter import separator

    try:
        result = check_lineage_integrity(state.db_path)
    except Exception as exc:
        raise CommandError(f"Lineage check failed: {exc}") from exc

    separator("Lineage Integrity")
    status = "OK" if result['all_ok'] else f"BROKEN ({result['total_broken']} violations)"
    print(f"\n  lineage_integrity={status}  total_broken={result['total_broken']}")
    for check in result.get('checks', []):
        ok = "OK" if check['broken_count'] == 0 else f"BROKEN ({check['broken_count']})"
        print(f"  {check['name']:<45}: {ok}")
    return state


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------

def cmd_show(state: ShellState, tokens: List[str]) -> ShellState:
    """Print full detail for a single memory event by ID."""
    positional, _ = _parse_flags(tokens)
    if not positional:
        raise CommandError("Usage: show ID")
    try:
        memory_id = int(positional[0])
    except ValueError:
        raise CommandError(f"Invalid ID '{positional[0]}' — must be an integer") from None

    from memory.service import get_memory_event, NotFoundError
    from memory.shell_formatter import print_event_detail

    try:
        event, revisions, links = get_memory_event(state.db_path, memory_id)
    except NotFoundError as exc:
        raise CommandError(str(exc)) from exc
    except Exception as exc:
        raise CommandError(f"Cannot fetch event {memory_id}: {exc}") from exc

    print_event_detail(event, revisions, links)
    return state


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------

def cmd_search(state: ShellState, tokens: List[str]) -> ShellState:
    """Full-text search across memory events."""
    positional, flags = _parse_flags(tokens)
    if not positional:
        raise CommandError("Usage: search QUERY [--type TYPE] [--status STATUS]")
    query = positional[0]
    filter_type = flags.get('type', None)
    filter_status = flags.get('status', None)
    limit = int(flags.get('limit', 50))

    from memory.service import search_memory_events, ValidationError
    from memory.shell_formatter import print_event_list, separator

    try:
        events = search_memory_events(state.db_path, query=query)
    except ValidationError as exc:
        raise CommandError(str(exc)) from exc
    except Exception as exc:
        raise CommandError(f"Search failed: {exc}") from exc

    if filter_type:
        events = [e for e in events if e.event_type == filter_type]
    if filter_status:
        events = [e for e in events if e.status == filter_status]

    label = f"Search: {query!r}"
    if filter_type:
        label += f"  type={filter_type}"
    if filter_status:
        label += f"  status={filter_status}"
    separator(label)
    print_event_list(events, limit=limit)
    print(f"\n  Total: {len(events)}")
    return state


# ---------------------------------------------------------------------------
# history
# ---------------------------------------------------------------------------

def cmd_history(state: ShellState, tokens: List[str]) -> ShellState:
    """Show recent activation decisions from the decision log."""
    _, flags = _parse_flags(tokens)
    limit = int(flags.get('n', 20))
    fired_only = 'fired-only' in flags
    policy_id = None
    if 'policy-id' in flags:
        try:
            policy_id = int(flags['policy-id'])
        except ValueError:
            raise CommandError("--policy-id must be an integer") from None

    from session.activation_policy import list_activation_decisions
    from memory.shell_formatter import print_decision_history

    try:
        decisions = list_activation_decisions(
            state.db_path,
            policy_id=policy_id,
            fired_only=fired_only,
            limit=limit,
        )
    except Exception as exc:
        raise CommandError(f"Cannot fetch decision history: {exc}") from exc

    print_decision_history(decisions)
    return state


# ---------------------------------------------------------------------------
# artifact
# ---------------------------------------------------------------------------

def cmd_artifact(state: ShellState, tokens: List[str]) -> ShellState:
    """Manage compression artifacts: list."""
    if not tokens:
        raise CommandError("Usage: artifact list [--status STATUS]")
    sub = tokens[0]
    if sub != 'list':
        raise CommandError(f"Unknown artifact subcommand '{sub}'. Use: list")

    _, flags = _parse_flags(tokens[1:])
    status = flags.get('status', None)
    limit = int(flags.get('limit', 50))

    from memory.compression import list_compression_artifacts
    from memory.shell_formatter import print_artifact_list

    try:
        artifacts = list_compression_artifacts(state.db_path, status=status, limit=limit)
    except Exception as exc:
        raise CommandError(f"Cannot list artifacts: {exc}") from exc

    print_artifact_list(artifacts)
    return state


# ---------------------------------------------------------------------------
# transcript
# ---------------------------------------------------------------------------

def cmd_transcript(state: ShellState, tokens: List[str]) -> ShellState:
    """Write a human-readable observational audit transcript for a session."""
    _, flags = _parse_flags(tokens)

    session_id = state.session_id
    if 'session-id' in flags:
        try:
            session_id = int(flags['session-id'])
        except ValueError:
            raise CommandError("--session-id must be an integer") from None

    if session_id is None:
        raise CommandError(
            "No session set. Use --session-id N to specify one, "
            "or start a session with 'session start'."
        )

    out_path = flags.get('out', f"transcript_{session_id}.txt")

    from memory.shell_formatter import write_transcript

    try:
        n_assemblies = write_transcript(state.db_path, session_id, out_path)
    except ValueError as exc:
        raise CommandError(str(exc)) from exc
    except OSError as exc:
        raise CommandError(f"Cannot write transcript to '{out_path}': {exc}") from exc

    size = os.path.getsize(out_path)
    print(f"  Transcript written: {out_path} ({size:,} bytes, {n_assemblies} assemblies)")
    print(
        "  NOTE: This is a human-readable audit artifact only. "
        "It is not a continuity bundle and cannot be re-imported."
    )
    return state

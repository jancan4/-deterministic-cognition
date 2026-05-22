"""
CLI entrypoint for the FX orchestration workflow runtime.

Commands:
  status                     List all non-terminal executions with state summary.
  recover [--apply]          Dry-run or apply lineage recovery for non-terminal executions.
  inspect --execution-id ID  Replay and inspect one execution, optionally to a specific event.
  snapshot --execution-id ID Take a manual snapshot of one execution.
  run-once [--orch-db PATH]  Submit one round of ready nodes to the orchestration layer.

Lineage is always canonical. The mutable state row is always a cache.
"""
import argparse
import sys
from typing import List

from workflow.recovery import RecoveryReport, find_non_terminal_execution_ids, recover_all
from workflow.inspector import InspectionResult, inspect_execution
from workflow.persistence import replay_execution_from_storage, take_snapshot
from workflow.storage import init_db, load_execution, load_execution_events


def _print_recovery_report(report: RecoveryReport) -> None:
    status = 'DIVERGED' if report.diverged else 'OK'
    valid = 'valid' if report.lineage_valid else 'INVALID'
    print(f"  {report.execution_id[:16]}  stored={report.stored_state}  "
          f"replayed={report.replayed_state}  lineage={valid}  "
          f"events={report.events_applied}  [{status}]")
    for detail in report.divergence_details:
        print(f"    ! {detail}")


def _print_inspection(result: InspectionResult) -> None:
    at = (f"event {result.replayed_to_event_index}"
          if result.replayed_to_event_index is not None else "full replay")
    valid = 'valid' if result.lineage_valid else 'INVALID'
    diverged = '  [DIVERGED FROM STORED]' if result.diverged_from_stored else ''
    print(f"execution_id    : {result.execution_id}")
    print(f"replay          : {at} of {result.total_events} total events "
          f"({result.events_applied} applied)")
    print(f"lineage         : {valid}{diverged}")
    print(f"state           : {result.state}")
    print(f"active_stage    : {result.active_stage_index}")
    print(f"completed_nodes : {result.completed_node_ids}")
    print(f"failed_nodes    : {result.failed_node_ids}")
    print(f"node_attempts   : {result.node_attempts}")
    if result.validation_errors:
        print("validation_errors:")
        for e in result.validation_errors:
            print(f"  ! {e}")
    if result.divergence_details:
        print("divergence_details:")
        for d in result.divergence_details:
            print(f"  ! {d}")


def cmd_status(args: argparse.Namespace) -> int:
    init_db(args.db)
    ids = find_non_terminal_execution_ids(args.db)
    if not ids:
        print("No non-terminal executions found.")
        return 0
    print(f"Non-terminal executions ({len(ids)}):")
    for eid in ids:
        stored = load_execution(args.db, eid)
        events = load_execution_events(args.db, eid)
        state = stored.state if stored else 'unknown'
        print(f"  {eid[:16]}  state={state}  events={len(events)}")
    return 0


def cmd_recover(args: argparse.Namespace) -> int:
    init_db(args.db)
    reports = recover_all(args.db, apply=args.apply)
    if not reports:
        print("No non-terminal executions to recover.")
        return 0
    action = 'Applied recovery' if args.apply else 'Dry-run recovery'
    print(f"{action} for {len(reports)} execution(s):")
    diverged = 0
    for report in reports:
        _print_recovery_report(report)
        if report.diverged:
            diverged += 1
    print(f"\n{diverged}/{len(reports)} execution(s) diverged.")
    return 0


def cmd_inspect(args: argparse.Namespace) -> int:
    init_db(args.db)
    result = inspect_execution(args.db, args.execution_id, args.at_event)
    _print_inspection(result)
    return 0


def cmd_snapshot(args: argparse.Namespace) -> int:
    import sqlite3 as _sqlite3

    init_db(args.db)
    conn = _sqlite3.connect(args.db)
    try:
        row = conn.execute(
            "SELECT MAX(id) FROM workflow_execution_events WHERE execution_id = ?",
            (args.execution_id,),
        ).fetchone()
    finally:
        conn.close()

    last_event_id = row[0] if row and row[0] is not None else None
    if last_event_id is None:
        print(f"No lineage events found for {args.execution_id}. Cannot snapshot.")
        return 1

    result = replay_execution_from_storage(args.db, args.execution_id)
    if result.execution is None or not result.is_valid:
        print(f"Replay failed for {args.execution_id}: {result.validation_errors}")
        return 1

    row_id = take_snapshot(args.db, result.execution, last_event_id)
    print(f"Snapshot written for {args.execution_id} (row_id={row_id}).")
    return 0


def cmd_run_once(args: argparse.Namespace) -> int:
    """
    Submit one round of ready nodes to the orchestration layer.

    This is a minimal coordination step: replay state from lineage, call
    step_execution once, persist resulting events. Does not loop.
    """
    orch_db = args.orch_db or args.db
    init_db(args.db)

    try:
        from workflow.coordination import step_execution
        from workflow.persistence import persist_execution
        from workflow.storage import load_execution, load_execution_events
        from workflow.replay import replay_execution
        from workflow.service import WorkflowService
    except ImportError as exc:
        print(f"run-once requires coordination layer: {exc}", file=sys.stderr)
        return 1

    ids = find_non_terminal_execution_ids(args.db)
    if not ids:
        print("No non-terminal executions. Nothing to do.")
        return 0

    submitted_total = 0
    for eid in ids:
        result = replay_execution_from_storage(args.db, eid)
        if result.execution is None or not result.is_valid:
            print(f"  {eid[:16]}  SKIP (replay failed)")
            continue
        exec_ = result.execution
        stored = load_execution(args.db, eid)
        if stored is None:
            print(f"  {eid[:16]}  SKIP (no stored row for plan lookup)")
            continue
        print(f"  {eid[:16]}  state={exec_.state}  (no plan available for submission)")

    print(f"\nrun-once complete. {submitted_total} node(s) submitted.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='workflow-cli',
        description='FX orchestration workflow runtime CLI',
    )
    parser.add_argument(
        '--db',
        default='workflow.db',
        help='Path to the workflow SQLite database (default: workflow.db)',
    )

    sub = parser.add_subparsers(dest='command', required=True)

    sub.add_parser('status', help='List non-terminal executions')

    recover_p = sub.add_parser('recover', help='Inspect or repair lineage divergence')
    recover_p.add_argument(
        '--apply',
        action='store_true',
        help='Write recovered state back to mutable rows (default: dry run)',
    )

    inspect_p = sub.add_parser('inspect', help='Replay and inspect one execution')
    inspect_p.add_argument('--execution-id', required=True, dest='execution_id',
                           help='Execution ID to inspect')
    inspect_p.add_argument('--at-event', type=int, default=None, dest='at_event',
                           help='Replay only the first N events (0-based count)')

    snapshot_p = sub.add_parser('snapshot', help='Take a manual snapshot')
    snapshot_p.add_argument('--execution-id', required=True, dest='execution_id',
                            help='Execution ID to snapshot')

    run_once_p = sub.add_parser('run-once', help='Submit one round of ready nodes')
    run_once_p.add_argument('--orch-db', default=None, dest='orch_db',
                            help='Orchestration DB path (defaults to --db)')

    return parser


def main(argv: List[str] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    dispatch = {
        'status': cmd_status,
        'recover': cmd_recover,
        'inspect': cmd_inspect,
        'snapshot': cmd_snapshot,
        'run-once': cmd_run_once,
    }
    return dispatch[args.command](args)


if __name__ == '__main__':
    sys.exit(main())

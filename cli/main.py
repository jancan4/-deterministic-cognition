"""
CLI entrypoint for the FX orchestration workflow runtime.

Commands:
  status                     List all non-terminal executions with state summary.
  recover [--apply]          Dry-run or apply lineage recovery for non-terminal executions.
  inspect --execution-id ID  Replay and inspect one execution, optionally to a specific event.
  snapshot --execution-id ID Take a manual snapshot of one execution.
  run-once [--orch-db PATH]  Submit one round of ready nodes to the orchestration layer.
  session-context            Export a deterministic context bundle from memory.
  ingest-file --path PATH    Extract candidate memory events from a source file.
  sources-register --path P  Register a file in the source document registry.
  sources-list               List registered source documents.
  sources-show --source-id S Show a single source document record.

Lineage is always canonical. The mutable state row is always a cache.
Session context export is read-only. No memory is mutated.
"""
import argparse
import json
import sys
from typing import List, Optional

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


def _session_context_to_json(reconstruction, filters: dict) -> str:
    """Serialise a SessionReconstruction to a structured JSON string."""
    ctx = reconstruction.context

    def _mem_dicts(items):
        return [m.to_dict() for m in items]

    def _wf_dicts(items):
        return [w.to_dict() for w in items]

    payload = {
        'session_id': ctx.session_id,
        'created_at': ctx.created_at,
        'char_budget': ctx.char_budget,
        'chars_used': ctx.chars_used,
        'total_candidates': ctx.total_candidates,
        'included_entries': ctx.included_entries,
        'truncated': ctx.truncated,
        'filters': filters,
        'sections': {
            'governance_context': _mem_dicts(ctx.governance_context),
            'unresolved_items': _mem_dicts(ctx.unresolved_items),
            'active_workflows': _wf_dicts(ctx.active_workflows),
            'active_investigations': _mem_dicts(ctx.active_investigations),
            'relevant_memory': _mem_dicts(ctx.relevant_memory),
            'execution_lineage': _wf_dicts(ctx.execution_lineage),
            'runtime_snapshots': [r.to_dict() for r in ctx.runtime_snapshots],
        },
    }
    return json.dumps(payload, indent=2, sort_keys=True)


def _apply_output_filters(
    reconstruction,
    filter_event_types: List[str],
    filter_statuses: List[str],
):
    """
    Return a filtered view of the reconstruction's memory sections.

    Filters are applied to the activated memory lists only — workflow and
    runtime sections are never filtered. Filtering is post-reconstruction
    (read-only: does not re-run activation).

    Returns the same reconstruction object with filtered section lists
    attached as a plain namespace for display purposes.
    """
    import types
    ctx = reconstruction.context

    def _keep(mem) -> bool:
        if filter_event_types and mem.event_type not in filter_event_types:
            return False
        if filter_statuses and mem.status not in filter_statuses:
            return False
        return True

    filtered = types.SimpleNamespace(
        session_id=ctx.session_id,
        created_at=ctx.created_at,
        char_budget=ctx.char_budget,
        chars_used=ctx.chars_used,
        total_candidates=ctx.total_candidates,
        included_entries=ctx.included_entries,
        truncated=ctx.truncated,
        governance_context=[m for m in ctx.governance_context if _keep(m)],
        unresolved_items=[m for m in ctx.unresolved_items if _keep(m)],
        active_workflows=ctx.active_workflows,
        active_investigations=[m for m in ctx.active_investigations if _keep(m)],
        relevant_memory=[m for m in ctx.relevant_memory if _keep(m)],
        execution_lineage=ctx.execution_lineage,
        runtime_snapshots=ctx.runtime_snapshots,
    )
    return filtered


def _render_filtered_markdown(ctx, filters: dict) -> str:
    """Render a filtered context view as a Markdown-formatted string."""
    from session.models import _render_section

    lines = [
        "# SESSION CONTEXT",
        f"session_id : {ctx.session_id}",
        f"created_at : {ctx.created_at}",
        f"budget     : {ctx.chars_used}/{ctx.char_budget} chars  "
        f"({ctx.included_entries} entries)"
        + ("  [TRUNCATED]" if ctx.truncated else ""),
    ]
    active_filters = {k: v for k, v in filters.items() if v}
    if active_filters:
        lines.append(f"filters    : {json.dumps(active_filters)}")

    sections = []

    if ctx.governance_context:
        sections.append(_render_section(
            'ACTIVE GOVERNANCE CONTEXT',
            [m.render() for m in ctx.governance_context],
        ))
    if ctx.active_workflows:
        sections.append(_render_section(
            'ACTIVE WORKFLOWS',
            [w.render() for w in ctx.active_workflows],
        ))
    if ctx.execution_lineage:
        sections.append(_render_section(
            'RECENT EXECUTION LINEAGE',
            [w.render() for w in ctx.execution_lineage],
        ))
    if ctx.unresolved_items:
        sections.append(_render_section(
            'UNRESOLVED ITEMS',
            [m.render() for m in ctx.unresolved_items],
        ))
    if ctx.relevant_memory:
        sections.append(_render_section(
            'RELEVANT MEMORY',
            [m.render() for m in ctx.relevant_memory],
        ))
    if ctx.active_investigations:
        sections.append(_render_section(
            'ACTIVE INVESTIGATIONS',
            [m.render() for m in ctx.active_investigations],
        ))
    if ctx.runtime_snapshots:
        sections.append(_render_section(
            'RUNTIME STATE',
            [r.render() for r in ctx.runtime_snapshots],
        ))

    header = '\n'.join(lines)
    if sections:
        return header + '\n\n' + '\n\n'.join(sections)
    return header + '\n\n(no items matched the activation policy)'


def cmd_session_context(args: argparse.Namespace) -> int:
    """
    Reconstruct and export a deterministic session context bundle.

    Reads from the memory database. Does not write to any database.
    Output is deterministic: same db state + same flags = same output.
    """
    from session.models import ContextActivationPolicy
    from session.reconstruction import reconstruct

    # Build activation policy from CLI flags
    policy = ContextActivationPolicy(
        tags=list(args.tags),
        max_entries=args.max_entries,
        max_chars=args.max_chars,
        include_governance=True,
        include_unresolved=True,
        include_adaptations=True,
        expand_related=True,
        workflow_db_path=args.workflow_db or None,
        include_active_workflows=bool(args.workflow_db),
        runtime_db_path=None,
        include_runtime_state=False,
    )

    try:
        reconstruction = reconstruct(args.db, policy)
    except Exception as exc:
        print(f"Error: reconstruction failed: {exc}", file=sys.stderr)
        return 1

    filters = {
        'tags': list(args.tags),
        'event_types': list(args.event_types),
        'statuses': list(args.statuses),
    }

    # Apply optional post-reconstruction filters
    filtered_ctx = _apply_output_filters(
        reconstruction,
        filter_event_types=list(args.event_types),
        filter_statuses=list(args.statuses),
    )

    # Render output
    if args.format == 'json':
        # For JSON, build payload from the filtered context
        payload = {
            'session_id': filtered_ctx.session_id,
            'created_at': filtered_ctx.created_at,
            'char_budget': filtered_ctx.char_budget,
            'chars_used': filtered_ctx.chars_used,
            'total_candidates': filtered_ctx.total_candidates,
            'included_entries': filtered_ctx.included_entries,
            'truncated': filtered_ctx.truncated,
            'filters': filters,
            'sections': {
                'governance_context': [m.to_dict() for m in filtered_ctx.governance_context],
                'unresolved_items': [m.to_dict() for m in filtered_ctx.unresolved_items],
                'active_workflows': [w.to_dict() for w in filtered_ctx.active_workflows],
                'active_investigations': [m.to_dict() for m in filtered_ctx.active_investigations],
                'relevant_memory': [m.to_dict() for m in filtered_ctx.relevant_memory],
                'execution_lineage': [w.to_dict() for w in filtered_ctx.execution_lineage],
                'runtime_snapshots': [r.to_dict() for r in filtered_ctx.runtime_snapshots],
            },
        }
        output = json.dumps(payload, indent=2, sort_keys=True)
    else:
        output = _render_filtered_markdown(filtered_ctx, filters)

    # Write to file or stdout
    if args.out:
        try:
            with open(args.out, 'w', encoding='utf-8') as f:
                f.write(output)
                f.write('\n')
            print(f"Context bundle written to: {args.out}")
        except OSError as exc:
            print(f"Error: could not write to {args.out!r}: {exc}", file=sys.stderr)
            return 1
    else:
        print(output)

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

    sc_p = sub.add_parser(
        'session-context',
        help='Export a deterministic context bundle from memory',
    )
    # For session-context, --db is the memory database (different default from workflow --db)
    sc_p.add_argument(
        '--db',
        default='memory.db',
        dest='db',
        help='Path to the memory SQLite database (default: memory.db)',
    )
    sc_p.add_argument(
        '--workflow-db',
        default=None,
        dest='workflow_db',
        help='Path to the workflow SQLite database (optional; adds ACTIVE WORKFLOWS section)',
    )
    sc_p.add_argument(
        '--max-entries', type=int, default=60, dest='max_entries',
        help='Maximum number of entries in the context bundle (default: 60)',
    )
    sc_p.add_argument(
        '--max-chars', type=int, default=12000, dest='max_chars',
        help='Maximum character budget for the context bundle (default: 12000)',
    )
    sc_p.add_argument(
        '--tag', action='append', default=[], dest='tags', metavar='TAG',
        help='Filter by tag (repeatable; e.g. --tag fx --tag macro)',
    )
    sc_p.add_argument(
        '--event-type', action='append', default=[], dest='event_types',
        metavar='TYPE',
        help='Post-filter by event type (repeatable; e.g. --event-type governance_rule)',
    )
    sc_p.add_argument(
        '--status', action='append', default=[], dest='statuses',
        metavar='STATUS',
        help='Post-filter by status (repeatable; e.g. --status unresolved)',
    )
    sc_p.add_argument(
        '--out', default=None, dest='out',
        help='Write output to this file instead of stdout',
    )
    sc_p.add_argument(
        '--format', choices=['json', 'markdown'], default='markdown', dest='format',
        help='Output format: json or markdown (default: markdown)',
    )

    # ingest-file --------------------------------------------------------
    if_p = sub.add_parser(
        'ingest-file',
        help='Extract candidate memory events from a source file',
    )
    if_p.set_defaults(command='ingest-file')
    if_p.add_argument(
        '--path', required=True, dest='path',
        help='Source file to ingest (.txt, .md, .markdown, or no extension)',
    )
    if_p.add_argument(
        '--db', default='memory.db', dest='db',
        help='Memory SQLite database (used only with --commit)',
    )
    if_p.add_argument(
        '--out', default=None, dest='out',
        help='Write candidate JSON to this file instead of stdout',
    )
    if_p.add_argument(
        '--commit', action='store_true', default=False, dest='commit',
        help='Commit accepted candidates to the memory database',
    )
    if_p.add_argument(
        '--source-type', default='unknown', dest='source_type',
        choices=[
            'doctrine', 'research_note', 'article', 'transcript',
            'implementation_brief', 'architecture_doc', 'external_reference', 'unknown',
        ],
        help='Source document type for registry (default: unknown)',
    )
    if_p.add_argument(
        '--authority-tier', default='unknown', dest='authority_tier',
        choices=['authoritative', 'high', 'medium', 'low', 'unknown'],
        help='Authority tier for registry (default: unknown)',
    )

    # sources-register ---------------------------------------------------
    sr_p = sub.add_parser(
        'sources-register',
        help='Register a file in the source document registry',
    )
    sr_p.set_defaults(command='sources-register')
    sr_p.add_argument('--path', required=True, dest='path', help='File to register')
    sr_p.add_argument('--db', default='memory.db', dest='db', help='Registry database')
    sr_p.add_argument(
        '--source-type', default='unknown', dest='source_type',
        choices=[
            'doctrine', 'research_note', 'article', 'transcript',
            'implementation_brief', 'architecture_doc', 'external_reference', 'unknown',
        ],
        help='Source document type (default: unknown)',
    )
    sr_p.add_argument(
        '--authority-tier', default='unknown', dest='authority_tier',
        choices=['authoritative', 'high', 'medium', 'low', 'unknown'],
        help='Authority tier (default: unknown)',
    )

    # sources-list -------------------------------------------------------
    sl_p = sub.add_parser(
        'sources-list',
        help='List registered source documents',
    )
    sl_p.set_defaults(command='sources-list')
    sl_p.add_argument('--db', default='memory.db', dest='db', help='Registry database')
    sl_p.add_argument(
        '--status', default=None, dest='status',
        choices=['active', 'superseded', 'deprecated', 'rejected', 'archived'],
        help='Filter by status',
    )
    sl_p.add_argument(
        '--source-type', default=None, dest='source_type',
        choices=[
            'doctrine', 'research_note', 'article', 'transcript',
            'implementation_brief', 'architecture_doc', 'external_reference', 'unknown',
        ],
        help='Filter by source type',
    )

    # sources-show -------------------------------------------------------
    ss_p = sub.add_parser(
        'sources-show',
        help='Show a single source document record',
    )
    ss_p.set_defaults(command='sources-show')
    ss_p.add_argument('--db', default='memory.db', dest='db', help='Registry database')
    ss_p.add_argument('--source-id', required=True, dest='source_id', help='16-char source_id')

    return parser


def cmd_ingest_file(args: argparse.Namespace) -> int:
    import json
    from ingestion.parser import parse_file
    from ingestion.chunker import chunk_document
    from ingestion.candidates import run_ingestion
    from sources.registry import register_source

    try:
        doc = parse_file(args.path)
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    # Register source provenance before candidate extraction.
    source_type = getattr(args, 'source_type', 'unknown')
    authority_tier = getattr(args, 'authority_tier', 'unknown')
    src_doc = register_source(
        args.db,
        args.path,
        source_type=source_type,
        authority_tier=authority_tier,
    )

    chunks = chunk_document(doc)
    result = run_ingestion(
        doc, chunks,
        memory_db_path=args.db if args.commit else None,
        commit=args.commit,
    )

    output_dict = result.to_dict()
    output_dict['source_registry'] = src_doc.to_dict()
    output = json.dumps(output_dict, indent=2, sort_keys=True)

    if args.out:
        with open(args.out, 'w', encoding='utf-8') as fh:
            fh.write(output)
        committed_note = (
            f" ({len(result.committed_ids)} committed)" if result.committed_ids else ""
        )
        print(
            f"Ingested {result.candidate_count} candidate(s){committed_note} "
            f"[src:{src_doc.source_id}] → {args.out}",
            file=sys.stderr,
        )
    else:
        print(output)

    return 0


def cmd_sources_register(args: argparse.Namespace) -> int:
    import json
    from sources.registry import register_source
    from sources.models import SourceValidationError

    try:
        doc = register_source(
            args.db,
            args.path,
            source_type=args.source_type,
            authority_tier=args.authority_tier,
        )
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except SourceValidationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(doc.to_dict(), indent=2, sort_keys=True))
    return 0


def cmd_sources_list(args: argparse.Namespace) -> int:
    import json
    from sources.registry import list_sources
    from sources.models import SourceValidationError

    try:
        docs = list_sources(args.db, status=args.status, source_type=args.source_type)
    except SourceValidationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(json.dumps([d.to_dict() for d in docs], indent=2, sort_keys=True))
    return 0


def cmd_sources_show(args: argparse.Namespace) -> int:
    import json
    from sources.registry import get_source_by_id

    doc = get_source_by_id(args.db, args.source_id)
    if doc is None:
        print(f"ERROR: source_id {args.source_id!r} not found", file=sys.stderr)
        return 1

    print(json.dumps(doc.to_dict(), indent=2, sort_keys=True))
    return 0


def main(argv: List[str] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    dispatch = {
        'status': cmd_status,
        'recover': cmd_recover,
        'inspect': cmd_inspect,
        'snapshot': cmd_snapshot,
        'run-once': cmd_run_once,
        'session-context': cmd_session_context,
        'ingest-file': cmd_ingest_file,
        'sources-register': cmd_sources_register,
        'sources-list': cmd_sources_list,
        'sources-show': cmd_sources_show,
    }
    return dispatch[args.command](args)


if __name__ == '__main__':
    sys.exit(main())

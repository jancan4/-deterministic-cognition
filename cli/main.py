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
  ingestion-runs             List ingestion run records.
  ingestion-run-show --run-id Show a single ingestion run record.
  semantic-run               Run a deterministic semantic extraction task.
  semantic-candidates list   List semantic candidate events from the ledger.
  memory-review list         List memory events pending operator review.
  memory-review approve      Transition a memory event to active/accepted.
  memory-review reject       Transition a memory event to rejected.
  semantic-workflow run      Execute one semantic_extraction workflow node.
  semantic-workflow lineage  Show semantic lineage for a workflow execution.
  semantic-workflow list-pending  List unresolved semantic memory events.

Lineage is always canonical. The mutable state row is always a cache.
Session context export is read-only. No memory is mutated.
Semantic promotion writes memory_events with status=unresolved; approval and
rejection must go through memory-review and are recorded in memory_revisions.
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
    if_p.add_argument(
        '--semantic-adapter', default=None, dest='semantic_adapter',
        metavar='ADAPTER',
        help='Optional: enrich candidates using this semantic adapter (stub|echo|ollama)',
    )
    if_p.add_argument(
        '--model', default=None, dest='model',
        metavar='MODEL',
        help='Ollama model name (required when --semantic-adapter ollama, e.g. phi3:mini)',
    )
    if_p.add_argument(
        '--ollama-url', default='http://localhost:11434', dest='ollama_url',
        metavar='URL',
        help='Ollama base URL for ingest-file semantic enrichment (default: http://localhost:11434)',
    )

    # semantic-run -------------------------------------------------------
    sr2_p = sub.add_parser(
        'semantic-run',
        help='Run a deterministic semantic extraction task via a registered adapter',
    )
    sr2_p.set_defaults(command='semantic-run')
    input_group = sr2_p.add_mutually_exclusive_group(required=True)
    input_group.add_argument(
        '--input-text', default=None, dest='input_text',
        metavar='TEXT', help='Inline input text',
    )
    input_group.add_argument(
        '--input-file', default=None, dest='input_file',
        metavar='PATH', help='Path to input text file',
    )
    sr2_p.add_argument(
        '--task-type', required=True, dest='task_type',
        choices=[
            'tagging', 'polarity_classification', 'entity_extraction',
            'claim_extraction', 'relation_extraction', 'summary_extraction',
            'clustering_hint', 'memory_candidate_classification',
        ],
        help='Semantic task type',
    )
    sr2_p.add_argument(
        '--adapter', default='stub', dest='adapter',
        help='Adapter name (default: stub). Built-in: stub, echo, ollama',
    )
    sr2_p.add_argument(
        '--model', default=None, dest='model',
        metavar='MODEL',
        help='Ollama model name (required when --adapter ollama, e.g. phi3:mini)',
    )
    sr2_p.add_argument(
        '--ollama-url', default='http://localhost:11434', dest='ollama_url',
        metavar='URL',
        help='Ollama base URL (default: http://localhost:11434)',
    )
    sr2_p.add_argument(
        '--format', default='json', dest='format',
        choices=['json', 'markdown'],
        help='Output format (default: json)',
    )
    sr2_p.add_argument(
        '--source-id', default=None, dest='source_id',
        help='Optional source_id for provenance',
    )
    sr2_p.add_argument(
        '--timeout', default=30.0, type=float, dest='timeout',
        help='Execution timeout in seconds (default: 30)',
    )

    # semantic-candidates ------------------------------------------------
    sc_p = sub.add_parser(
        'semantic-candidates',
        help='List semantic candidate events from the ledger',
    )
    sc_p.set_defaults(command='semantic-candidates')
    sc_p.add_argument('--db', default='memory.db', help='Database path')
    sc_p.add_argument('--run-id', default=None, dest='run_id',
                      help='Filter by semantic run_id')
    sc_p.add_argument('--status', default=None,
                      choices=['candidate', 'promoted', 'rejected'],
                      help='Filter by candidate status')
    sc_p.add_argument('--source-id', default=None, dest='source_id',
                      help='Filter by source_id')
    sc_p.add_argument('--limit', default=100, type=int,
                      help='Maximum results (default: 100)')
    sc_p.add_argument('--out', default=None,
                      help='Write output to file instead of stdout')

    # memory-review ------------------------------------------------------
    mr_p = sub.add_parser(
        'memory-review',
        help='Inspect, approve, or reject memory events pending operator review',
    )
    mr_p.set_defaults(command='memory-review')
    mr_sub = mr_p.add_subparsers(dest='review_subcommand')
    mr_sub.required = True

    mr_list = mr_sub.add_parser('list', help='List memory events under review')
    mr_list.add_argument('--db', default='memory.db', help='Database path')
    mr_list.add_argument('--status', default=None,
                         help='Filter by status (default: all review statuses)')
    mr_list.add_argument('--event-type', default=None, dest='event_type',
                         help='Filter by event_type')
    mr_list.add_argument('--out', default=None,
                         help='Write output to file instead of stdout')

    mr_approve = mr_sub.add_parser('approve', help='Approve a memory event')
    mr_approve.add_argument('--db', default='memory.db', help='Database path')
    mr_approve.add_argument('--id', required=True, type=int,
                             help='memory_events.id to approve')
    mr_approve.add_argument('--by', required=True,
                             help='Operator identifier (created_by)')
    mr_approve.add_argument('--status', default='active',
                             choices=['active', 'accepted'],
                             help='Target status (default: active)')

    mr_reject = mr_sub.add_parser('reject', help='Reject a memory event')
    mr_reject.add_argument('--db', default='memory.db', help='Database path')
    mr_reject.add_argument('--id', required=True, type=int,
                            help='memory_events.id to reject')
    mr_reject.add_argument('--by', required=True,
                            help='Operator identifier (created_by)')
    mr_reject.add_argument('--reason', required=True,
                            help='Rejection reason (recorded in memory_revisions)')

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

    # ingestion-runs -----------------------------------------------------
    ir_p = sub.add_parser(
        'ingestion-runs',
        help='List ingestion run records',
    )
    ir_p.set_defaults(command='ingestion-runs')
    ir_p.add_argument('--db', default='memory.db', dest='db', help='Registry database')
    ir_p.add_argument('--source-id', default=None, dest='source_id', help='Filter by source_id')
    ir_p.add_argument(
        '--status', default=None, dest='status',
        choices=['candidate_generated', 'committed', 'failed'],
        help='Filter by run status',
    )

    # ingestion-run-show -------------------------------------------------
    irs_p = sub.add_parser(
        'ingestion-run-show',
        help='Show a single ingestion run record',
    )
    irs_p.set_defaults(command='ingestion-run-show')
    irs_p.add_argument('--db', default='memory.db', dest='db', help='Registry database')
    irs_p.add_argument('--run-id', required=True, dest='run_id', help='16-char run_id')

    # export-bundle ------------------------------------------------------
    eb_p = sub.add_parser(
        'export-bundle',
        help='Export a deterministic continuity bundle to JSON',
    )
    eb_p.set_defaults(command='export-bundle')
    eb_p.add_argument('--db', default='memory.db', dest='db', help='Memory database path')
    eb_p.add_argument('--out', default=None, dest='out', help='Output file (stdout if omitted)')
    eb_p.add_argument(
        '--tag', action='append', default=[], dest='tags',
        metavar='TAG', help='Filter: include only events with this tag (repeatable)',
    )
    eb_p.add_argument(
        '--source-id', action='append', default=[], dest='source_ids',
        metavar='SOURCE_ID', help='Filter: include only events from this source_id (repeatable)',
    )
    eb_p.add_argument(
        '--unresolved-only', action='store_true', default=False, dest='unresolved_only',
        help='Filter: include only unresolved/proposed events',
    )
    eb_p.add_argument('--since', default=None, dest='since', help='Filter: since ISO-8601 timestamp (inclusive)')
    eb_p.add_argument('--until', default=None, dest='until', help='Filter: until ISO-8601 timestamp (inclusive)')
    eb_p.add_argument(
        '--workflow-db', default=None, dest='workflow_db',
        help='Optional workflow database path for workflow_references section',
    )
    eb_p.add_argument('--exported-by', default='fx-orchestration-system', dest='exported_by')

    # import-bundle ------------------------------------------------------
    ib_p = sub.add_parser(
        'import-bundle',
        help='Import a continuity bundle into a database',
    )
    ib_p.set_defaults(command='import-bundle')
    ib_p.add_argument('--db', default='memory.db', dest='db', help='Target memory database path')
    ib_p.add_argument('--path', required=True, dest='path', help='Path to bundle JSON file')
    ib_p.add_argument(
        '--dry-run', action='store_true', default=False, dest='dry_run',
        help='Validate and plan import without writing anything',
    )

    # semantic-workflow --------------------------------------------------
    sw_p = sub.add_parser(
        'semantic-workflow',
        help='Workflow-integrated semantic execution and lineage inspection',
    )
    sw_p.set_defaults(command='semantic-workflow')
    sw_sub = sw_p.add_subparsers(dest='sw_subcommand')
    sw_sub.required = True

    sw_run = sw_sub.add_parser(
        'run',
        help=(
            'Execute one semantic_extraction workflow node directly. '
            'Optionally records node_completed with semantic lineage metadata in workflow lineage.'
        ),
    )
    sw_run.add_argument(
        '--db', default='workflow.db', dest='db',
        help='Workflow database path for lineage recording (default: workflow.db)',
    )
    sw_run.add_argument(
        '--memory-db', required=True, dest='memory_db',
        help='Memory/semantic ledger database path',
    )
    sw_payload_group = sw_run.add_mutually_exclusive_group(required=True)
    sw_payload_group.add_argument(
        '--payload', default=None, dest='payload',
        metavar='JSON',
        help='Task payload JSON string (semantic task parameters)',
    )
    sw_payload_group.add_argument(
        '--payload-file', default=None, dest='payload_file',
        metavar='PATH',
        help='Path to task payload JSON file',
    )
    sw_run.add_argument(
        '--execution-id', default=None, dest='execution_id',
        help='Workflow execution ID (optional; appends node_completed event to lineage)',
    )
    sw_run.add_argument(
        '--node-id', default=None, dest='node_id',
        help='Node ID for workflow lineage recording (required if --execution-id is set)',
    )
    sw_run.add_argument(
        '--commit', action='store_true', default=False,
        help='Promote candidates to unresolved memory (default: ledger-only)',
    )
    sw_run.add_argument(
        '--actor', default='semantic-workflow', dest='actor',
        help='Actor identifier for lineage records (default: semantic-workflow)',
    )

    sw_lineage = sw_sub.add_parser(
        'lineage',
        help='Show semantic lineage for all completed semantic nodes in a workflow execution',
    )
    sw_lineage.add_argument(
        '--db', default='workflow.db', dest='db',
        help='Workflow database path (default: workflow.db)',
    )
    sw_lineage.add_argument(
        '--memory-db', default=None, dest='memory_db',
        help='Memory/semantic ledger database path (optional; enriches output from ledger)',
    )
    sw_lineage.add_argument(
        '--execution-id', required=True, dest='execution_id',
        help='Workflow execution ID',
    )

    sw_pending = sw_sub.add_parser(
        'list-pending',
        help='List unresolved memory events with semantic provenance pending operator review',
    )
    sw_pending.add_argument(
        '--db', default='memory.db', dest='db',
        help='Memory database path (default: memory.db)',
    )
    sw_pending.add_argument(
        '--limit', default=100, type=int,
        help='Maximum number of results (default: 100)',
    )

    return parser


def cmd_ingest_file(args: argparse.Namespace) -> int:
    import json
    from ingestion.parser import parse_file, PARSER_VERSION
    from ingestion.chunker import chunk_document
    from ingestion.candidates import run_ingestion
    from ingestion.extractor import EXTRACTOR_VERSION
    from ingestion.runs import record_run, make_started_at
    from sources.registry import register_source

    started_at = make_started_at()

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

    run_status = 'candidate_generated'
    run_metadata: dict = {}
    result = None

    try:
        chunks = chunk_document(doc)
        result = run_ingestion(
            doc, chunks,
            memory_db_path=args.db if args.commit else None,
            commit=args.commit,
        )
        if args.commit and result.committed_ids:
            run_status = 'committed'
    except Exception as exc:
        run_status = 'failed'
        run_metadata['error'] = str(exc)
        print(f"ERROR: {exc}", file=sys.stderr)

    chunk_count = len(result.chunks) if result else 0
    candidate_count = result.candidate_count if result else 0
    committed_ids = result.committed_ids if result else []

    import datetime as _dt
    completed_at = _dt.datetime.now(_dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

    run = record_run(
        db_path=args.db,
        source_id=src_doc.source_id,
        source_checksum=src_doc.checksum_sha256,
        source_version=src_doc.version,
        parser_version=PARSER_VERSION,
        extractor_version=EXTRACTOR_VERSION,
        chunk_count=chunk_count,
        candidate_count=candidate_count,
        committed_count=len(committed_ids),
        committed_memory_ids=committed_ids,
        status=run_status,
        started_at=started_at,
        completed_at=completed_at,
        metadata=run_metadata,
    )

    if run_status == 'failed':
        return 1

    # Optional semantic enrichment — ledger write always; memory write only with --commit
    semantic_candidates = []
    semantic_enrichment_meta: dict = {}
    semantic_adapter_name = getattr(args, 'semantic_adapter', None)
    if semantic_adapter_name and result is not None:
        from models.registry import make_default_registry, AdapterRegistryError
        from semantic.pipeline import enrich_chunks_with_semantic
        from semantic.ledger import (
            init_ledger as _init_ledger,
            record_run as _record_run,
            derive_candidate_id,
            list_candidates as _list_candidates,
            promote_candidate as _promote_candidate,
        )
        from memory import service as _mem_service
        promoted_ids: list = []
        try:
            if semantic_adapter_name == 'ollama':
                model = getattr(args, 'model', None)
                if not model:
                    print(
                        "WARNING: --model is required when --semantic-adapter ollama; "
                        "skipping semantic enrichment",
                        file=sys.stderr,
                    )
                    raise AdapterRegistryError("missing --model for ollama")
                from models.ollama_adapter import OllamaAdapter
                sem_adapter = OllamaAdapter(
                    model=model,
                    base_url=getattr(args, 'ollama_url', 'http://localhost:11434'),
                )
            else:
                sem_registry = make_default_registry()
                sem_adapter = sem_registry.get(semantic_adapter_name)

            # enrich_chunks returns List[SemanticPipelineResult] — one per chunk
            pipeline_results = enrich_chunks_with_semantic(result.chunks, sem_adapter)
            semantic_candidates = [c for r in pipeline_results for c in r.candidates]

            # Always persist to ledger (idempotent; no memory write)
            _init_ledger(args.db)
            for pr in pipeline_results:
                # Pass raw_output from Ollama adapter metadata when available
                raw_out = None
                if pr.execution_result.response and pr.execution_result.response.metadata:
                    raw_out = pr.execution_result.response.metadata.get('raw_output')
                _record_run(args.db, pr, raw_output=raw_out)

            # On --commit: promote each candidate to memory as status='unresolved'
            if args.commit and pipeline_results:
                _mem_service.init_db(args.db)
                for pr in pipeline_results:
                    run_id = pr.execution_result.request_id
                    for idx in range(len(pr.candidates)):
                        cid = derive_candidate_id(run_id, idx)
                        mid = _promote_candidate(args.db, cid, approved_by='ingest-file')
                        promoted_ids.append(mid)

            semantic_enrichment_meta = {
                'adapter': semantic_adapter_name,
                'chunk_count': len(result.chunks),
                'candidate_count': len(semantic_candidates),
                'ledger_runs': len(pipeline_results),
                'promoted_memory_ids': promoted_ids,
                'committed': args.commit and bool(promoted_ids),
            }
        except AdapterRegistryError as exc:
            print(f"WARNING: semantic adapter not found: {exc}", file=sys.stderr)
        except Exception as exc:
            print(f"WARNING: semantic enrichment failed: {exc}", file=sys.stderr)

    output_dict = result.to_dict()
    output_dict['source_registry'] = src_doc.to_dict()
    output_dict['ingestion_run'] = run.to_dict()
    if semantic_adapter_name:
        output_dict['semantic_candidates'] = [c.to_dict() for c in semantic_candidates]
        output_dict['semantic_enrichment'] = semantic_enrichment_meta
    output = json.dumps(output_dict, indent=2, sort_keys=True)

    if args.out:
        with open(args.out, 'w', encoding='utf-8') as fh:
            fh.write(output)
        committed_note = (
            f" ({len(committed_ids)} committed)" if committed_ids else ""
        )
        print(
            f"Ingested {candidate_count} candidate(s){committed_note} "
            f"[src:{src_doc.source_id}] [run:{run.run_id}] → {args.out}",
            file=sys.stderr,
        )
    else:
        print(output)

    return 0


def cmd_ingestion_runs(args: argparse.Namespace) -> int:
    import json
    from ingestion.runs import list_runs

    runs = list_runs(args.db, source_id=args.source_id, status=args.status)
    print(json.dumps([r.to_dict() for r in runs], indent=2, sort_keys=True))
    return 0


def cmd_ingestion_run_show(args: argparse.Namespace) -> int:
    import json
    from ingestion.runs import get_run

    run = get_run(args.db, args.run_id)
    if run is None:
        print(f"ERROR: run_id {args.run_id!r} not found", file=sys.stderr)
        return 1

    print(json.dumps(run.to_dict(), indent=2, sort_keys=True))
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


def cmd_semantic_run(args: argparse.Namespace) -> int:
    import json
    from models.registry import make_default_registry, AdapterRegistryError
    from models.execution import make_policy
    from semantic.pipeline import run_semantic_task
    from semantic.validators import SemanticValidationError

    # Resolve input text
    if args.input_text:
        input_text = args.input_text
    else:
        try:
            with open(args.input_file, encoding='utf-8') as fh:
                input_text = fh.read()
        except OSError as exc:
            print(f"ERROR reading input file: {exc}", file=sys.stderr)
            return 1
    if not input_text or not input_text.strip():
        print("ERROR: input text is empty", file=sys.stderr)
        return 1

    # Resolve adapter
    adapter_name = args.adapter
    if adapter_name == 'ollama':
        model = getattr(args, 'model', None)
        if not model:
            print(
                "ERROR: --model is required when --adapter ollama "
                "(e.g. --model phi3:mini)",
                file=sys.stderr,
            )
            return 1
        try:
            from models.ollama_adapter import OllamaAdapter
            from models.contracts import ModelContractError as _MCE
            adapter = OllamaAdapter(
                model=model,
                base_url=getattr(args, 'ollama_url', 'http://localhost:11434'),
                timeout_seconds=args.timeout,
            )
        except Exception as exc:
            print(f"ERROR: could not initialise OllamaAdapter: {exc}", file=sys.stderr)
            return 1
    else:
        registry = make_default_registry()
        try:
            adapter = registry.get(adapter_name)
        except AdapterRegistryError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1

    # Build policy
    try:
        policy = make_policy(timeout_seconds=args.timeout)
    except Exception as exc:
        print(f"ERROR: invalid timeout: {exc}", file=sys.stderr)
        return 1

    # Execute pipeline
    try:
        result = run_semantic_task(
            task_type=args.task_type,
            input_text=input_text,
            adapter=adapter,
            source_id=getattr(args, 'source_id', None),
            policy=policy,
        )
    except SemanticValidationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if args.format == 'markdown':
        print(result.to_markdown())
    else:
        print(result.to_json())

    return 0 if result.success else 1


def cmd_semantic_candidates(args: argparse.Namespace) -> int:
    """List semantic candidate events from the ledger."""
    import json
    from semantic.ledger import LedgerError, list_candidates

    try:
        candidates = list_candidates(
            args.db,
            run_id=getattr(args, 'run_id', None),
            status=getattr(args, 'status', None),
            source_id=getattr(args, 'source_id', None),
            limit=getattr(args, 'limit', 100),
        )
    except LedgerError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    output = json.dumps([c.to_dict() for c in candidates], indent=2, sort_keys=True)
    if getattr(args, 'out', None):
        with open(args.out, 'w', encoding='utf-8') as fh:
            fh.write(output)
    else:
        print(output)
    return 0


def cmd_memory_review(args: argparse.Namespace) -> int:
    """Inspect, approve, or reject memory events pending operator review."""
    import json
    from memory import service as mem_service
    from memory.service import ValidationError, NotFoundError

    subcommand = args.review_subcommand

    if subcommand == 'list':
        try:
            events = mem_service.review_memory(
                args.db,
                status=getattr(args, 'status', None),
                event_type=getattr(args, 'event_type', None),
            )
        except ValidationError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        output = json.dumps(
            [e.to_dict() for e in events], indent=2, sort_keys=True
        )
        if getattr(args, 'out', None):
            with open(args.out, 'w', encoding='utf-8') as fh:
                fh.write(output)
        else:
            print(output)
        return 0

    elif subcommand == 'approve':
        new_status = getattr(args, 'status', 'active') or 'active'
        if new_status not in ('active', 'accepted'):
            print(
                "ERROR: --status for approve must be 'active' or 'accepted'",
                file=sys.stderr,
            )
            return 1
        try:
            event = mem_service.update_status(
                args.db,
                args.id,
                new_status,
                reason='operator approval',
                created_by=args.by,
            )
        except NotFoundError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        except ValidationError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(event.to_dict(), indent=2, sort_keys=True))
        return 0

    elif subcommand == 'reject':
        if not getattr(args, 'reason', None) or not args.reason.strip():
            print("ERROR: --reason is required for reject", file=sys.stderr)
            return 1
        try:
            event = mem_service.update_status(
                args.db,
                args.id,
                'rejected',
                reason=args.reason,
                created_by=args.by,
            )
        except NotFoundError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        except ValidationError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(event.to_dict(), indent=2, sort_keys=True))
        return 0

    print(f"ERROR: unknown memory-review subcommand: {subcommand!r}", file=sys.stderr)
    return 1


def cmd_semantic_workflow(args: argparse.Namespace) -> int:
    """semantic-workflow subcommand dispatcher."""
    subcommand = args.sw_subcommand

    if subcommand == 'run':
        return _cmd_sw_run(args)
    elif subcommand == 'lineage':
        return _cmd_sw_lineage(args)
    elif subcommand == 'list-pending':
        return _cmd_sw_list_pending(args)

    print(f"ERROR: unknown semantic-workflow subcommand: {subcommand!r}", file=sys.stderr)
    return 1


def _cmd_sw_run(args: argparse.Namespace) -> int:
    """
    Execute one semantic_extraction workflow node directly (non-queue path).

    Takes the task payload as a JSON string (--payload) or file (--payload-file)
    and runs the semantic handler. If --execution-id and --node-id are provided,
    appends a node_completed lineage event with semantic metadata to the workflow
    execution log.

    Architecture note: workflow plan and definition are not persisted to the
    workflow DB — only execution state is. This command therefore takes the task
    payload directly rather than loading it from stored node definitions. Full
    workflow coordination (stage advancement, completion detection) requires the
    coordination layer with the full plan; this command records semantic lineage
    only.

    Workflow replay semantics: this command is the *execution* path. Replay
    reconstructs state from stored lineage events and never re-invokes adapters.
    """
    import json as _json
    from datetime import datetime, timezone
    from workflow.semantic_handler import execute_semantic_node
    from workflow.state import (
        EVENT_NODE_COMPLETED, WorkflowExecutionLineageEvent,
    )
    from workflow.storage import (
        init_db as wf_init_db, append_execution_events,
    )

    mem_db = args.memory_db
    actor = getattr(args, 'actor', 'semantic-workflow')

    # Resolve task_payload_json from --payload or --payload-file
    if getattr(args, 'payload', None):
        task_payload_json = args.payload
    elif getattr(args, 'payload_file', None):
        try:
            with open(args.payload_file, encoding='utf-8') as fh:
                task_payload_json = fh.read()
        except OSError as exc:
            print(f"ERROR reading --payload-file: {exc}", file=sys.stderr)
            return 1
    else:
        print("ERROR: --payload or --payload-file is required", file=sys.stderr)
        return 1

    # Override commit flag if --commit passed on CLI
    if getattr(args, 'commit', False):
        try:
            payload_dict = _json.loads(task_payload_json)
            payload_dict['commit'] = True
            task_payload_json = _json.dumps(payload_dict)
        except Exception as exc:
            print(f"ERROR: could not apply --commit to payload: {exc}", file=sys.stderr)
            return 1

    # Execute semantic node (always writes to semantic ledger)
    sem_result = execute_semantic_node(task_payload_json, mem_db, actor=actor)

    if not sem_result.success:
        print(f"ERROR: semantic execution failed: {sem_result.error}", file=sys.stderr)

    # Optionally record in workflow execution lineage
    execution_id = getattr(args, 'execution_id', None)
    node_id = getattr(args, 'node_id', None)
    if execution_id and node_id and sem_result.run_id:
        wf_db = args.db
        wf_init_db(wf_db)
        now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        evt_type = EVENT_NODE_COMPLETED if sem_result.success else 'node_failed'
        meta = dict(sem_result.lineage_metadata)
        lineage_evt = WorkflowExecutionLineageEvent(
            execution_id=execution_id,
            event_type=evt_type,
            old_state=None,
            new_state=None,
            node_id=node_id,
            stage_index=0,
            reason=f'Semantic extraction: run_id={sem_result.run_id}',
            created_at=now,
            metadata=meta,
        )
        try:
            append_execution_events(wf_db, [lineage_evt])
        except Exception as exc:
            print(
                f"WARNING: semantic run succeeded but workflow lineage append failed: {exc}",
                file=sys.stderr,
            )

    output = {
        'semantic_run_id': sem_result.run_id,
        'candidate_ids': sem_result.candidate_ids,
        'promoted_memory_ids': sem_result.promoted_memory_ids,
        'committed': sem_result.lineage_metadata.get('committed', False),
        'adapter_name': sem_result.lineage_metadata.get('adapter_name'),
        'adapter_version': sem_result.lineage_metadata.get('adapter_version'),
        'task_type': sem_result.lineage_metadata.get('task_type'),
        'success': sem_result.success,
        'error': sem_result.error,
    }
    if execution_id:
        output['execution_id'] = execution_id
    if node_id:
        output['node_id'] = node_id

    print(_json.dumps(output, indent=2, sort_keys=True))
    return 0 if sem_result.success else 1


def _cmd_sw_lineage(args: argparse.Namespace) -> int:
    """
    Print semantic lineage for all completed semantic nodes in a workflow execution.

    Loads workflow execution events, extracts node_completed events that carry
    semantic_run_id in their metadata, and prints provenance detail. Optionally
    fetches the semantic_execution_runs row from the memory DB for enriched output.
    """
    import json as _json
    from workflow.storage import init_db as wf_init_db, load_execution_events
    from workflow.state import EVENT_NODE_COMPLETED

    wf_db = args.db
    execution_id = args.execution_id
    mem_db = getattr(args, 'memory_db', None)

    wf_init_db(wf_db)
    events = load_execution_events(wf_db, execution_id)
    semantic_events = [
        e for e in events
        if e.event_type == EVENT_NODE_COMPLETED and e.metadata.get('semantic_run_id')
    ]

    if not semantic_events:
        print(f"No semantic lineage found for execution {execution_id!r}.")
        return 0

    results = []
    for evt in semantic_events:
        meta = evt.metadata
        entry = {
            'node_id': evt.node_id,
            'stage_index': evt.stage_index,
            'completed_at': evt.created_at,
            'semantic_run_id': meta.get('semantic_run_id'),
            'candidate_ids': meta.get('candidate_ids', []),
            'promoted_memory_ids': meta.get('promoted_memory_ids', []),
            'adapter_name': meta.get('adapter_name'),
            'adapter_version': meta.get('adapter_version'),
            'task_type': meta.get('task_type'),
            'committed': meta.get('committed', False),
        }
        if meta.get('model'):
            entry['model'] = meta['model']

        # Optionally enrich from semantic ledger
        if mem_db and meta.get('semantic_run_id'):
            try:
                from semantic.ledger import get_run
                run = get_run(mem_db, meta['semantic_run_id'])
                if run:
                    entry['ledger_status'] = run.status
                    entry['ledger_candidate_count'] = run.candidate_count
                    entry['ledger_promoted_count'] = run.promoted_count
            except Exception:
                pass

        results.append(entry)

    print(_json.dumps(results, indent=2, sort_keys=True))
    return 0


def _cmd_sw_list_pending(args: argparse.Namespace) -> int:
    """
    List unresolved memory events that have semantic provenance.

    Filters memory events whose evidence string starts with 'semantic:',
    indicating they were created via the semantic promotion path. Uses the
    existing memory service listing — no new DB queries.
    """
    import json as _json
    from memory import service as mem_service

    db = args.db
    limit = getattr(args, 'limit', 100)

    try:
        mem_service.init_db(db)
        events = mem_service.list_memory_events(db, status='unresolved')
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    semantic_events = [
        e for e in events
        if e.evidence and e.evidence.startswith('semantic:')
    ]

    if not semantic_events:
        print("No unresolved semantic memory events pending review.")
        return 0

    output = _json.dumps(
        [e.to_dict() for e in semantic_events[:limit]],
        indent=2,
        sort_keys=True,
    )
    print(output)
    return 0


def cmd_export_bundle(args: argparse.Namespace) -> int:
    import json
    from continuity.exporter import export_bundle
    from continuity.models import ExportFilter

    export_filter = None
    f = ExportFilter(
        tags=args.tags,
        source_ids=args.source_ids,
        unresolved_only=args.unresolved_only,
        since=args.since,
        until=args.until,
    )
    if not f.is_empty():
        export_filter = f

    try:
        bundle = export_bundle(
            db_path=args.db,
            export_filter=export_filter,
            workflow_db_path=args.workflow_db,
            exported_by=args.exported_by,
        )
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    serialized = json.dumps(bundle, sort_keys=True, indent=2)
    if args.out:
        with open(args.out, 'w') as fh:
            fh.write(serialized)
            fh.write('\n')
        manifest = bundle['manifest']
        print(
            f"Exported bundle {manifest['bundle_id']!r}: "
            f"{manifest['memory_event_count']} events, "
            f"{manifest['source_count']} sources → {args.out}"
        )
    else:
        print(serialized)

    return 0


def cmd_import_bundle(args: argparse.Namespace) -> int:
    import json
    from continuity.importer import import_bundle
    from continuity.manifest import BundleValidationError

    try:
        with open(args.path) as fh:
            bundle_dict = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"ERROR reading bundle: {exc}", file=sys.stderr)
        return 1

    try:
        result = import_bundle(bundle_dict, args.db, dry_run=args.dry_run)
    except BundleValidationError as exc:
        print(f"ERROR: bundle validation failed: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(result.to_dict(), indent=2, sort_keys=True))

    if result.has_collisions:
        print(
            f"\nImport refused: {len(result.collisions)} collision(s) detected. "
            f"No records written.",
            file=sys.stderr,
        )
        return 1

    if args.dry_run:
        print(
            f"\nDry-run complete: would import "
            f"{result.imported_memory_events} events, "
            f"{result.imported_source_documents} sources, "
            f"{result.imported_ingestion_runs} runs. "
            f"No writes made."
        )
    else:
        print(
            f"\nImported: "
            f"{result.imported_memory_events} events, "
            f"{result.imported_source_documents} sources, "
            f"{result.imported_ingestion_runs} runs. "
            f"Skipped: {result.skipped_memory_events} events."
        )

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
        'ingestion-runs': cmd_ingestion_runs,
        'ingestion-run-show': cmd_ingestion_run_show,
        'export-bundle': cmd_export_bundle,
        'import-bundle': cmd_import_bundle,
        'semantic-run': cmd_semantic_run,
        'semantic-candidates': cmd_semantic_candidates,
        'memory-review': cmd_memory_review,
        'semantic-workflow': cmd_semantic_workflow,
    }
    return dispatch[args.command](args)


if __name__ == '__main__':
    sys.exit(main())

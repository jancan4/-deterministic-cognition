"""
Session reconstruction: assembles a deterministic SessionReconstruction from
memory, workflow lineage, and runtime state.

Canonical truth remains the persisted lineage and memory events. This module
reads from those stores and assembles an ephemeral session context. Given the
same database state and activation policy, reconstruct() always returns the
same result.

I/O pattern:
  1. Activate memory events (memory.retrieval)
  2. Partition into sections (activation.partition_by_section)
  3. Load active workflows (workflow.storage + workflow.recovery)
  4. Load runtime snapshots (runtime.state_store)
  5. Apply context window budget (context_window.apply_context_budget)
  6. Return SessionReconstruction

No autonomous decisions. No hidden context injection. No mutation.
"""
import hashlib
import json as _json
import sqlite3 as _sqlite3
from datetime import datetime, timezone
from typing import Dict, List, Optional

from .activation import activate_memory, partition_by_section
from .context_window import apply_context_budget
from .models import (
    AssemblyDivergenceReport,
    ActiveWorkflow,
    CONTEXT_ASSEMBLY_VERSION,
    ContextActivationPolicy,
    RuntimeSnapshot,
    SessionContext,
    SessionReconstruction,
)


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _mem_connect(db_path: str) -> _sqlite3.Connection:
    conn = _sqlite3.connect(db_path)
    conn.row_factory = _sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA foreign_keys=ON')
    return conn


def _make_session_id(
    memory_db_path: str,
    policy: ContextActivationPolicy,
    query_vector_hash: Optional[str] = None,
) -> str:
    """
    Deterministic session ID derived from policy inputs only.

    Same memory_db_path + policy tags + min_confidence always produces the
    same session_id. Used for supersession tracking in context_assembly_log.
    Does NOT include the reconstruction timestamp so that repeated assemblies
    of the same policy intent share a session_id and can supersede each other.
    """
    components = [memory_db_path, str(sorted(policy.tags)), str(policy.min_confidence)]
    if query_vector_hash:
        components.append(query_vector_hash)
    return hashlib.sha256('|'.join(components).encode()).hexdigest()[:32]


def _make_assembly_hash(
    session_id: str,
    policy: ContextActivationPolicy,
    context: SessionContext,
    snapshot_json: str,
) -> str:
    """
    Deterministic content-addressable hash for one assembly.

    Same reconstruction output (same snapshot_json, same budget accounting,
    same policy, same CONTEXT_ASSEMBLY_VERSION) → same assembly_hash.
    """
    payload = _json.dumps({
        'assembly_version': CONTEXT_ASSEMBLY_VERSION,
        'session_id': session_id,
        'compression_mode': policy.compression_mode,
        'policy_json': _json.dumps(policy.to_dict(), sort_keys=True, separators=(',', ':')),
        'entries_accepted': context.included_entries,
        'char_budget_used': context.chars_used,
        'snapshot_json': snapshot_json,
    }, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(payload.encode()).hexdigest()[:32]


def _find_non_terminal_execution_ids(workflow_db_path: str) -> List[str]:
    """
    Query workflow_executions for non-terminal execution IDs.

    Implemented inline to avoid depending on workflow.recovery, which may
    not be present in all deployment configurations.
    """
    import sqlite3

    try:
        from workflow.state import TERMINAL_WORKFLOW_EXECUTION_STATES
        terminal = tuple(TERMINAL_WORKFLOW_EXECUTION_STATES)
    except ImportError:
        terminal = ('completed', 'cancelled')

    placeholders = ','.join('?' * len(terminal))
    conn = sqlite3.connect(workflow_db_path)
    conn.execute('PRAGMA journal_mode=WAL')
    try:
        rows = conn.execute(
            f"SELECT execution_id FROM workflow_executions "
            f"WHERE state NOT IN ({placeholders}) "
            f"ORDER BY updated_at ASC",
            terminal,
        ).fetchall()
    finally:
        conn.close()
    return [r[0] for r in rows]


def _load_active_workflows(workflow_db_path: str, max_workflows: int) -> List[ActiveWorkflow]:
    """
    Load non-terminal workflow executions from the workflow SQLite store.

    Returns up to max_workflows executions ordered by updated_at ascending
    (oldest first, so the most-recently-updated are at the end — callers
    that want freshest-first should reverse the result).
    """
    try:
        from workflow.storage import load_execution, load_execution_events, init_db
    except ImportError:
        return []

    try:
        init_db(workflow_db_path)
    except Exception:
        return []

    try:
        execution_ids = _find_non_terminal_execution_ids(workflow_db_path)
    except Exception:
        return []

    workflows: List[ActiveWorkflow] = []

    for eid in execution_ids[:max_workflows]:
        try:
            stored = load_execution(workflow_db_path, eid)
            if stored is None:
                continue
            events = load_execution_events(workflow_db_path, eid)
        except Exception:
            continue
        workflows.append(ActiveWorkflow(
            execution_id=stored.execution_id,
            workflow_id=stored.workflow_id,
            plan_id=stored.plan_id,
            state=stored.state,
            active_stage_index=stored.active_stage_index,
            completed_node_ids=list(stored.completed_node_ids),
            failed_node_ids=list(stored.failed_node_ids),
            node_attempts=dict(stored.node_attempts),
            total_lineage_events=len(events),
            updated_at=stored.updated_at,
        ))

    return workflows


def _load_runtime_snapshots(
    runtime_db_path: str,
    max_events: int,
) -> List[RuntimeSnapshot]:
    """
    Load active runtime processes and their recent lineage transitions.
    """
    try:
        from runtime.state_store import list_runtimes, get_runtime_lineage, init_db
    except ImportError:
        return []

    try:
        init_db(runtime_db_path)
    except Exception:
        return []

    try:
        runtimes = list_runtimes(runtime_db_path)
    except Exception:
        return []

    snapshots: List[RuntimeSnapshot] = []
    for rt in runtimes:
        try:
            lineage = get_runtime_lineage(runtime_db_path, rt.id)
        except Exception:
            lineage = []
        recent = lineage[-max_events:] if lineage else []
        snapshots.append(RuntimeSnapshot(
            runtime_id=rt.id,
            name=rt.name,
            state=rt.state,
            current_iteration=rt.current_iteration,
            updated_at=rt.updated_at,
            recent_transitions=[t.to_dict() for t in recent],
        ))

    return snapshots


def reconstruct(
    memory_db_path: str,
    policy: Optional[ContextActivationPolicy] = None,
) -> SessionReconstruction:
    """
    Reconstruct a session context from persisted memory, workflow, and runtime state.

    Deterministic: same database state + same policy → same result.
    Read-only: no database is mutated.

    Args:
        memory_db_path: path to the memory SQLite database.
        policy: activation policy; defaults to ContextActivationPolicy() if None.

    Returns:
        SessionReconstruction wrapping a fully-assembled SessionContext.
    """
    if policy is None:
        policy = ContextActivationPolicy()

    created_at = _now_utc()
    session_id = _make_session_id(memory_db_path, policy)

    # 1. Activate and rank memory events
    activated = activate_memory(memory_db_path, policy)
    sections = partition_by_section(activated)

    governance_context = sections['governance_context']
    unresolved_items = sections['unresolved_items']
    active_investigations = sections['active_investigations']
    relevant_memory = sections['relevant_memory']

    # 2. Load active workflows (if configured)
    active_workflows: List[ActiveWorkflow] = []
    if policy.include_active_workflows and policy.workflow_db_path:
        active_workflows = _load_active_workflows(
            policy.workflow_db_path, policy.max_workflows
        )

    # 3. Load runtime snapshots (if configured)
    runtime_snapshots: List[RuntimeSnapshot] = []
    if policy.include_runtime_state and policy.runtime_db_path:
        runtime_snapshots = _load_runtime_snapshots(
            policy.runtime_db_path, policy.max_runtime_events
        )

    # 4. Apply context window budget
    budgeted = apply_context_budget(
        policy=policy,
        governance_context=governance_context,
        unresolved_items=unresolved_items,
        active_workflows=active_workflows,
        active_investigations=active_investigations,
        relevant_memory=relevant_memory,
        execution_lineage=[],   # terminal workflows not surfaced by default
        runtime_snapshots=runtime_snapshots,
    )

    # 5. Assemble SessionContext
    context = SessionContext(
        session_id=session_id,
        created_at=created_at,
        policy=policy,
        governance_context=budgeted.governance_context,
        unresolved_items=budgeted.unresolved_items,
        active_workflows=budgeted.active_workflows,
        execution_lineage=budgeted.execution_lineage,
        relevant_memory=budgeted.relevant_memory,
        active_investigations=budgeted.active_investigations,
        runtime_snapshots=budgeted.runtime_snapshots,
        total_candidates=budgeted.total_candidates,
        included_entries=budgeted.included_entries,
        char_budget=budgeted.char_budget,
        chars_used=budgeted.chars_used,
        truncated=budgeted.truncated,
        assembly_version=CONTEXT_ASSEMBLY_VERSION,
    )

    return SessionReconstruction(context=context)


def reconstruct_from_dict(
    context_dict: dict,
    policy: Optional[ContextActivationPolicy] = None,
) -> 'SessionContext':
    """
    Restore a SessionContext from its to_dict() representation.

    Used for audit and replay: allows inspection of a previously-captured
    session without re-querying the databases.

    Returns the SessionContext; does not re-run retrieval or scoring.
    """
    from .models import ActivatedMemory, ActiveWorkflow, RuntimeSnapshot, SessionContext

    def _mem(d: dict) -> ActivatedMemory:
        return ActivatedMemory(
            memory_id=d['memory_id'],
            event_type=d['event_type'],
            title=d['title'],
            summary=d['summary'],
            evidence=d.get('evidence'),
            confidence=d['confidence'],
            status=d['status'],
            tags=d['tags'],
            source=d['source'],
            related_ids=d['related_ids'],
            created_at=d['created_at'],
            updated_at=d['updated_at'],
            is_expanded=d['is_expanded'],
            tag_overlap=d['tag_overlap'],
            activation_rank=(),   # rank not needed for replay display
        )

    def _wf(d: dict) -> ActiveWorkflow:
        return ActiveWorkflow(
            execution_id=d['execution_id'],
            workflow_id=d['workflow_id'],
            plan_id=d['plan_id'],
            state=d['state'],
            active_stage_index=d['active_stage_index'],
            completed_node_ids=d['completed_node_ids'],
            failed_node_ids=d['failed_node_ids'],
            node_attempts=d['node_attempts'],
            total_lineage_events=d['total_lineage_events'],
            updated_at=d['updated_at'],
        )

    def _rt(d: dict) -> RuntimeSnapshot:
        return RuntimeSnapshot(
            runtime_id=d['runtime_id'],
            name=d['name'],
            state=d['state'],
            current_iteration=d['current_iteration'],
            updated_at=d['updated_at'],
            recent_transitions=d['recent_transitions'],
        )

    p = policy if policy is not None else ContextActivationPolicy()

    return SessionContext(
        session_id=context_dict['session_id'],
        created_at=context_dict['created_at'],
        policy=p,
        governance_context=[_mem(d) for d in context_dict.get('governance_context', [])],
        unresolved_items=[_mem(d) for d in context_dict.get('unresolved_items', [])],
        active_workflows=[_wf(d) for d in context_dict.get('active_workflows', [])],
        execution_lineage=[_wf(d) for d in context_dict.get('execution_lineage', [])],
        relevant_memory=[_mem(d) for d in context_dict.get('relevant_memory', [])],
        active_investigations=[_mem(d) for d in context_dict.get('active_investigations', [])],
        runtime_snapshots=[_rt(d) for d in context_dict.get('runtime_snapshots', [])],
        total_candidates=context_dict['total_candidates'],
        included_entries=context_dict['included_entries'],
        char_budget=context_dict['char_budget'],
        chars_used=context_dict['chars_used'],
        truncated=context_dict['truncated'],
        assembly_version=context_dict.get('assembly_version', 'unknown'),
    )


# ---------------------------------------------------------------------------
# Assembly log: persist, replay, verify
# ---------------------------------------------------------------------------

def log_assembly(
    db_path: str,
    reconstruction: SessionReconstruction,
    *,
    query_vector_hash: Optional[str] = None,
    query_vector_provenance_json: Optional[str] = None,
) -> dict:
    """
    Persist a reconstruction to context_assembly_log and return the log row.

    Idempotency: if the same reconstruction is logged twice (identical
    assembly_hash), the existing row is returned without any write.

    Supersession: if a different reconstruction shares the same session_id,
    the previous active row is superseded and the new one is inserted.

    Returns the log row as a plain dict (keys match context_assembly_log columns).
    """
    ctx = reconstruction.context
    policy = ctx.policy
    snapshot_json = _json.dumps(ctx.to_dict(), sort_keys=True, separators=(',', ':'))
    assembly_hash = _make_assembly_hash(ctx.session_id, policy, ctx, snapshot_json)
    now = _now_utc()

    conn = _mem_connect(db_path)
    try:
        with conn:
            existing = conn.execute(
                'SELECT * FROM context_assembly_log WHERE assembly_hash = ?',
                (assembly_hash,),
            ).fetchone()
            if existing is not None:
                return dict(existing)

            conn.execute(
                """UPDATE context_assembly_log
                   SET status = 'superseded', superseded_at = ?, superseded_reason = 'new_assembly'
                   WHERE session_id = ? AND status = 'active'""",
                (now, ctx.session_id),
            )

            cur = conn.execute(
                """INSERT INTO context_assembly_log
                   (assembly_hash, session_id, assembly_version, assembled_at, db_path,
                    policy_json, query_vector_hash, query_vector_provenance_json,
                    entries_accepted, entries_rejected_budget, entries_rejected_filter,
                    char_budget_used, char_budget_limit, compression_mode,
                    assembly_snapshot_json, status)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'active')""",
                (
                    assembly_hash,
                    ctx.session_id,
                    CONTEXT_ASSEMBLY_VERSION,
                    now,
                    db_path,
                    _json.dumps(policy.to_dict(), sort_keys=True),
                    query_vector_hash,
                    query_vector_provenance_json,
                    ctx.included_entries,
                    ctx.total_candidates - ctx.included_entries,
                    0,
                    ctx.chars_used,
                    ctx.char_budget,
                    policy.compression_mode,
                    snapshot_json,
                ),
            )
            row = conn.execute(
                'SELECT * FROM context_assembly_log WHERE id = ?', (cur.lastrowid,)
            ).fetchone()
            return dict(row)
    finally:
        conn.close()


def replay_assembly(assembly_id: int, db_path: str) -> SessionReconstruction:
    """
    Restore a SessionReconstruction from a stored context_assembly_log row.

    Pure snapshot replay: loads assembly_snapshot_json and calls
    reconstruct_from_dict(). Does not re-query memory_events or any other
    table beyond fetching the single log row.

    Raises ValueError if assembly_id is not found.
    """
    conn = _mem_connect(db_path)
    try:
        row = conn.execute(
            'SELECT * FROM context_assembly_log WHERE id = ?', (assembly_id,)
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        raise ValueError(f"Assembly {assembly_id} not found in context_assembly_log")

    snapshot_dict = _json.loads(row['assembly_snapshot_json'])
    ctx = reconstruct_from_dict(snapshot_dict)
    return SessionReconstruction(context=ctx, replayed=True)


def verify_assembly_against_current_db(
    assembly_id: int,
    db_path: str,
) -> AssemblyDivergenceReport:
    """
    Re-run reconstruction and compare its output against the stored snapshot.

    Diagnostic only: reads the stored policy, re-runs reconstruct() against
    the current DB, and diffs memory_ids across all activated sections.

    Does not write to context_assembly_log or any other table.
    Does not perform replay — this is verification, not replay.

    Raises ValueError if assembly_id is not found.
    """
    conn = _mem_connect(db_path)
    try:
        row = conn.execute(
            'SELECT * FROM context_assembly_log WHERE id = ?', (assembly_id,)
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        raise ValueError(f"Assembly {assembly_id} not found in context_assembly_log")

    policy = ContextActivationPolicy.from_dict(_json.loads(row['policy_json']))

    current_recon = reconstruct(db_path, policy)
    current_ctx = current_recon.context

    stored_snapshot = _json.loads(row['assembly_snapshot_json'])

    def _ids_from_snapshot(d: dict) -> set:
        ids: set = set()
        for key in ('governance_context', 'unresolved_items', 'active_investigations', 'relevant_memory'):
            for item in d.get(key, []):
                ids.add(item['memory_id'])
        return ids

    def _ids_from_ctx(ctx: SessionContext) -> set:
        ids: set = set()
        for section in (ctx.governance_context, ctx.unresolved_items,
                        ctx.active_investigations, ctx.relevant_memory):
            for item in section:
                ids.add(item.memory_id)
        return ids

    stored_ids = _ids_from_snapshot(stored_snapshot)
    current_ids = _ids_from_ctx(current_ctx)

    added = sorted(current_ids - stored_ids)
    removed = sorted(stored_ids - current_ids)
    diverged = bool(added or removed)

    return AssemblyDivergenceReport(
        assembly_id=assembly_id,
        assembly_hash=row['assembly_hash'],
        diverged=diverged,
        events_added_since_assembly=added,
        events_removed_since_assembly=removed,
        events_rescored_since_assembly=[],
    )

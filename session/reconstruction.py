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
    AssemblyTransition,
    ActiveWorkflow,
    CognitionSession,
    ConflictingPair,
    CONTEXT_ASSEMBLY_VERSION,
    ContextActivationPolicy,
    ContinuityArtifactEntry,
    RuntimeSnapshot,
    SessionContext,
    SessionReconstruction,
    SessionTimelineDivergenceReport,
    VALID_TRANSITION_TYPES,
)


class ContinuityGovernanceError(ValueError):
    """
    Raised when a compression artifact referenced in ContextActivationPolicy
    is not in 'active' status at assembly time.

    Only active artifacts may be surfaced in continuity_context. Candidate,
    superseded, and invalidated artifacts raise this error to prevent silent
    use of unreviewed or stale reductions.
    """


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


def resolve_assembly_contradictions(
    db_path: str,
    assembled_ids: List[int],
) -> List[ConflictingPair]:
    """
    Query active contradicts links where BOTH sides are in assembled_ids.

    Returns ConflictingPair objects sorted by link_id ascending. Pure read —
    no writes. Returns empty list when assembled_ids is empty or no
    intra-assembly contradictions exist.

    Only links with status='active' are included. Retracted links are not
    surfaced even if they connected events in this assembly.
    """
    if not assembled_ids:
        return []

    id_set = set(assembled_ids)
    placeholders = ','.join('?' * len(assembled_ids))

    conn = _mem_connect(db_path)
    try:
        rows = conn.execute(
            f"""
            SELECT id, source_id, target_id, created_by, reason,
                   link_confidence, created_at
            FROM memory_links
            WHERE relationship = 'contradicts'
              AND status = 'active'
              AND source_id IN ({placeholders})
              AND target_id IN ({placeholders})
            ORDER BY id ASC
            """,
            assembled_ids + assembled_ids,
        ).fetchall()
    finally:
        conn.close()

    return [
        ConflictingPair(
            link_id=row['id'],
            source_id=row['source_id'],
            target_id=row['target_id'],
            created_by=row['created_by'],
            reason=row['reason'],
            link_confidence=row['link_confidence'],
            link_created_at=row['created_at'],
        )
        for row in rows
        if row['source_id'] in id_set and row['target_id'] in id_set
    ]


def _load_continuity_artifacts(
    db_path: str,
    artifact_ids: List[int],
    char_budget: int,
) -> List[ContinuityArtifactEntry]:
    """
    Load operator-promoted compression artifacts for inclusion in continuity_context.

    Governance invariants:
    - Only artifacts with status='active' are permitted.
    - Any artifact in candidate, superseded, or invalidated status raises
      ContinuityGovernanceError — no silent fallback.
    - Artifacts are loaded in declaration order; loading stops when char_budget
      is exhausted (remaining artifacts are silently skipped to respect budget).

    Read-only. Does not write to any table.
    """
    if not artifact_ids:
        return []

    conn = _mem_connect(db_path)
    try:
        entries: List[ContinuityArtifactEntry] = []
        chars_used = 0
        for artifact_id in artifact_ids:
            row = conn.execute(
                "SELECT id, status, source_assembly_id, source_assembly_hash, "
                "compression_method, producer_version, promoted_by, promoted_at, "
                "artifact_text, artifact_char_count "
                "FROM compression_artifacts WHERE id = ?",
                (artifact_id,),
            ).fetchone()
            if row is None:
                raise ContinuityGovernanceError(
                    f"compression artifact id={artifact_id} not found; "
                    "only active artifacts may be referenced in compression_artifact_ids"
                )
            if row['status'] != 'active':
                raise ContinuityGovernanceError(
                    f"compression artifact id={artifact_id} has status={row['status']!r}; "
                    "only 'active' artifacts may be surfaced in continuity_context"
                )
            text = row['artifact_text']
            char_count = row['artifact_char_count']
            if chars_used + char_count > char_budget:
                # Budget exhausted — skip remaining artifacts rather than truncating text
                continue
            chars_used += char_count
            entries.append(ContinuityArtifactEntry(
                artifact_id=row['id'],
                source_assembly_id=row['source_assembly_id'],
                source_assembly_hash=row['source_assembly_hash'],
                compression_method=row['compression_method'],
                producer_version=row['producer_version'],
                promoted_by=row['promoted_by'] or '',
                promoted_at=row['promoted_at'] or '',
                artifact_text=text,
                artifact_char_count=char_count,
            ))
        return entries
    finally:
        conn.close()


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

    # 3b. Load continuity artifacts (Phase 6B).
    # Raises ContinuityGovernanceError if any listed artifact is not 'active'.
    # Uses a separate char budget; does not consume from the main memory budget.
    continuity_context = _load_continuity_artifacts(
        memory_db_path,
        policy.compression_artifact_ids,
        policy.continuity_char_budget,
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

    # 5. Resolve contradiction pairs for the budgeted event set.
    # Runs after budgeting so char accounting is not affected.
    included_ids = budgeted.all_included_ids()
    contradiction_pairs = resolve_assembly_contradictions(memory_db_path, included_ids)

    # Build annotation map: memory_id → sorted list of contradicting memory_ids in this assembly
    contradiction_map: Dict[int, List[int]] = {}
    for pair in contradiction_pairs:
        contradiction_map.setdefault(pair.source_id, []).append(pair.target_id)
        contradiction_map.setdefault(pair.target_id, []).append(pair.source_id)

    for section in (
        budgeted.governance_context,
        budgeted.unresolved_items,
        budgeted.active_investigations,
        budgeted.relevant_memory,
    ):
        for item in section:
            item.contradiction_ids = sorted(contradiction_map.get(item.memory_id, []))

    # 6. Assemble SessionContext
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
        contradiction_pairs=contradiction_pairs,
        assembly_version=CONTEXT_ASSEMBLY_VERSION,
        continuity_context=continuity_context,
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
            contradiction_ids=d.get('contradiction_ids', []),
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
        contradiction_pairs=[
            ConflictingPair.from_dict(p)
            for p in context_dict.get('contradiction_pairs', [])
        ],
        assembly_version=context_dict.get('assembly_version', 'unknown'),
        # continuity_context: default [] for backward compat with pre-v1.2.0 snapshots
        continuity_context=[
            ContinuityArtifactEntry.from_dict(e)
            for e in context_dict.get('continuity_context', [])
        ],
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

    stored_snapshot = _json.loads(row['assembly_snapshot_json'])

    # Detect continuity artifact drift: check which referenced artifact_ids are no
    # longer 'active' in the current DB. Do this before reconstruct() so drift is
    # reported even when reconstruct() would raise ContinuityGovernanceError.
    stored_continuity = stored_snapshot.get('continuity_context', [])
    stored_artifact_ids = [e['artifact_id'] for e in stored_continuity]
    continuity_artifacts_changed: List[int] = []
    if stored_artifact_ids:
        conn2 = _mem_connect(db_path)
        try:
            for artifact_id in stored_artifact_ids:
                art_row = conn2.execute(
                    "SELECT status FROM compression_artifacts WHERE id = ?",
                    (artifact_id,),
                ).fetchone()
                if art_row is None or art_row['status'] != 'active':
                    continuity_artifacts_changed.append(artifact_id)
        finally:
            conn2.close()

    # Re-run reconstruction with continuity artifacts stripped from the policy
    # (so drift in artifact status doesn't prevent memory-event divergence checks).
    policy_dict = _json.loads(row['policy_json'])
    policy_dict['compression_artifact_ids'] = []
    policy = ContextActivationPolicy.from_dict(policy_dict)

    current_recon = reconstruct(db_path, policy)
    current_ctx = current_recon.context

    def _items_from_snapshot(d: dict) -> Dict[int, dict]:
        items: Dict[int, dict] = {}
        for key in ('governance_context', 'unresolved_items', 'active_investigations', 'relevant_memory'):
            for item in d.get(key, []):
                items[item['memory_id']] = item
        return items

    def _items_from_ctx(ctx: SessionContext) -> Dict[int, 'ActivatedMemory']:
        from .models import ActivatedMemory
        items: Dict[int, ActivatedMemory] = {}
        for section in (ctx.governance_context, ctx.unresolved_items,
                        ctx.active_investigations, ctx.relevant_memory):
            for item in section:
                items[item.memory_id] = item
        return items

    stored_items = _items_from_snapshot(stored_snapshot)
    current_items = _items_from_ctx(current_ctx)

    stored_ids = set(stored_items.keys())
    current_ids = set(current_items.keys())

    added = sorted(current_ids - stored_ids)
    removed = sorted(stored_ids - current_ids)

    rescored: List[int] = []
    for mid in sorted(stored_ids & current_ids):
        snap_conf = stored_items[mid].get('confidence')
        curr_conf = current_items[mid].confidence
        if snap_conf != curr_conf:
            rescored.append(mid)

    # Contradiction divergence: compare stored contradiction link_ids against current
    stored_link_ids = {
        p['link_id']
        for p in stored_snapshot.get('contradiction_pairs', [])
    }
    current_pairs = resolve_assembly_contradictions(db_path, list(current_ids))
    current_link_ids = {p.link_id for p in current_pairs}

    contradictions_added = sorted(current_link_ids - stored_link_ids)
    contradictions_retracted = sorted(stored_link_ids - current_link_ids)

    diverged = bool(
        added or removed or rescored
        or contradictions_added or contradictions_retracted
        or continuity_artifacts_changed
    )

    return AssemblyDivergenceReport(
        assembly_id=assembly_id,
        assembly_hash=row['assembly_hash'],
        diverged=diverged,
        events_added_since_assembly=added,
        events_removed_since_assembly=removed,
        events_rescored_since_assembly=rescored,
        contradictions_added_since_assembly=contradictions_added,
        contradictions_retracted_since_assembly=contradictions_retracted,
        continuity_artifacts_changed=continuity_artifacts_changed,
    )


# ---------------------------------------------------------------------------
# Cognition session lifecycle
# ---------------------------------------------------------------------------

def open_cognition_session(
    db_path: str,
    policy: ContextActivationPolicy,
    triggered_by: str,
    *,
    metadata: Optional[dict] = None,
) -> CognitionSession:
    """
    Open a new cognition session for the given activation policy.

    Creates a cognition_session row with status='active' and assembly_count=0.
    The session_key is the policy fingerprint (same derivation as session_id in
    context_assembly_log). Multiple active sessions with the same session_key
    are permitted; governance detects duplicates via detect_duplicate_active_sessions().

    Raises ValueError if triggered_by is empty.
    """
    if not triggered_by or not triggered_by.strip():
        raise ValueError("'triggered_by' must not be empty")

    session_key = _make_session_id(db_path, policy)
    now = _now_utc()

    policy_fingerprint = {
        'tags': sorted(policy.tags),
        'min_confidence': policy.min_confidence,
        'compression_mode': policy.compression_mode,
    }
    policy_fingerprint_json = _json.dumps(policy_fingerprint, sort_keys=True)
    metadata_json = _json.dumps(metadata, sort_keys=True) if metadata is not None else None

    conn = _mem_connect(db_path)
    try:
        with conn:
            cur = conn.execute(
                """INSERT INTO cognition_session
                   (session_key, status, started_at, db_path,
                    policy_fingerprint_json, metadata_json)
                   VALUES (?,?,?,?,?,?)""",
                (session_key, 'active', now, db_path,
                 policy_fingerprint_json, metadata_json),
            )
            row = conn.execute(
                'SELECT * FROM cognition_session WHERE id = ?', (cur.lastrowid,)
            ).fetchone()
            return CognitionSession.from_row(row)
    finally:
        conn.close()


def log_assembly_transition(
    db_path: str,
    cognition_session_id: int,
    to_assembly_id: int,
    transition_type: str,
    triggered_by: str,
    reason: str,
    *,
    from_assembly_id: Optional[int] = None,
    triggering_retrieval_ids: Optional[List[int]] = None,
    triggering_confidence_revision_ids: Optional[List[int]] = None,
    triggering_contradiction_link_ids: Optional[List[int]] = None,
    provenance: Optional[dict] = None,
) -> AssemblyTransition:
    """
    Record an assembly transition in the chronological log.

    Appends to assembly_transition_log and updates cognition_session
    (assembly_count, latest_assembly_id, initial_assembly_id on first append).

    from_assembly_id is inferred from session.latest_assembly_id if not provided
    and the session already has assemblies. Pass from_assembly_id=None explicitly
    for session_start transitions.

    Provenance id arrays are sorted for determinism. Empty arrays are stored as NULL.

    Raises ValueError if:
    - cognition_session_id not found
    - session is not 'active'
    - to_assembly_id not found in context_assembly_log
    - transition_type is not a valid VALID_TRANSITION_TYPES member
    - triggered_by or reason is empty
    """
    if transition_type not in VALID_TRANSITION_TYPES:
        raise ValueError(
            f"Invalid transition_type '{transition_type}'. "
            f"Valid: {sorted(VALID_TRANSITION_TYPES)}"
        )
    if not triggered_by or not triggered_by.strip():
        raise ValueError("'triggered_by' must not be empty")
    if not reason or not reason.strip():
        raise ValueError("'reason' must not be empty")

    now = _now_utc()

    conn = _mem_connect(db_path)
    try:
        with conn:
            sess_row = conn.execute(
                'SELECT * FROM cognition_session WHERE id = ?', (cognition_session_id,)
            ).fetchone()
            if sess_row is None:
                raise ValueError(f"Cognition session {cognition_session_id} not found")
            if sess_row['status'] != 'active':
                raise ValueError(
                    f"Cognition session {cognition_session_id} has status "
                    f"'{sess_row['status']}'; only 'active' sessions accept new transitions"
                )

            asm_row = conn.execute(
                'SELECT id FROM context_assembly_log WHERE id = ?', (to_assembly_id,)
            ).fetchone()
            if asm_row is None:
                raise ValueError(
                    f"Assembly {to_assembly_id} not found in context_assembly_log"
                )

            # sequence_index: one beyond current max, or 0 for first
            seq_row = conn.execute(
                'SELECT MAX(sequence_index) FROM assembly_transition_log '
                'WHERE cognition_session_id = ?',
                (cognition_session_id,),
            ).fetchone()
            max_seq = seq_row[0]
            sequence_index = 0 if max_seq is None else max_seq + 1

            # Auto-infer from_assembly_id from session state when not provided
            if from_assembly_id is None and sess_row['latest_assembly_id'] is not None:
                from_assembly_id = sess_row['latest_assembly_id']

            # Normalize provenance arrays — NULL when empty
            retr_json = (
                _json.dumps(sorted(triggering_retrieval_ids))
                if triggering_retrieval_ids else None
            )
            conf_json = (
                _json.dumps(sorted(triggering_confidence_revision_ids))
                if triggering_confidence_revision_ids else None
            )
            contra_json = (
                _json.dumps(sorted(triggering_contradiction_link_ids))
                if triggering_contradiction_link_ids else None
            )
            prov_json = (
                _json.dumps(provenance, sort_keys=True)
                if provenance is not None else None
            )

            cur = conn.execute(
                """INSERT INTO assembly_transition_log
                   (cognition_session_id, sequence_index, from_assembly_id,
                    to_assembly_id, transition_type, transition_reason,
                    triggered_by, transitioned_at,
                    triggering_retrieval_ids_json,
                    triggering_confidence_revision_ids_json,
                    triggering_contradiction_link_ids_json,
                    provenance_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (cognition_session_id, sequence_index, from_assembly_id,
                 to_assembly_id, transition_type, reason, triggered_by, now,
                 retr_json, conf_json, contra_json, prov_json),
            )

            # Update session: increment count and pointers
            new_count = sess_row['assembly_count'] + 1
            if sess_row['initial_assembly_id'] is None:
                conn.execute(
                    """UPDATE cognition_session
                       SET assembly_count = ?,
                           latest_assembly_id = ?,
                           initial_assembly_id = ?
                       WHERE id = ?""",
                    (new_count, to_assembly_id, to_assembly_id, cognition_session_id),
                )
            else:
                conn.execute(
                    """UPDATE cognition_session
                       SET assembly_count = ?, latest_assembly_id = ?
                       WHERE id = ?""",
                    (new_count, to_assembly_id, cognition_session_id),
                )

            row = conn.execute(
                'SELECT * FROM assembly_transition_log WHERE id = ?', (cur.lastrowid,)
            ).fetchone()
            return AssemblyTransition.from_row(row)
    finally:
        conn.close()


def close_cognition_session(
    db_path: str,
    cognition_session_id: int,
    reason: str,
    triggered_by: str,
) -> CognitionSession:
    """
    Close an active cognition session.

    Sets status='closed', closed_at, closed_reason. After close, no new
    transitions can be appended. Existing rows in assembly_transition_log
    are not affected.

    Raises ValueError if session not found or status is not 'active'.
    """
    if not triggered_by or not triggered_by.strip():
        raise ValueError("'triggered_by' must not be empty")
    if not reason or not reason.strip():
        raise ValueError("'reason' must not be empty")

    now = _now_utc()

    conn = _mem_connect(db_path)
    try:
        with conn:
            row = conn.execute(
                'SELECT * FROM cognition_session WHERE id = ?', (cognition_session_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"Cognition session {cognition_session_id} not found")
            if row['status'] != 'active':
                raise ValueError(
                    f"Cognition session {cognition_session_id} has status "
                    f"'{row['status']}'; only 'active' sessions can be closed"
                )

            conn.execute(
                """UPDATE cognition_session
                   SET status = 'closed', closed_at = ?, closed_reason = ?
                   WHERE id = ?""",
                (now, reason, cognition_session_id),
            )
            row = conn.execute(
                'SELECT * FROM cognition_session WHERE id = ?', (cognition_session_id,)
            ).fetchone()
            return CognitionSession.from_row(row)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Cognition session read and replay
# ---------------------------------------------------------------------------

def list_cognition_sessions(
    db_path: str,
    status: Optional[str] = None,
    limit: int = 50,
) -> List[CognitionSession]:
    """List cognition sessions ordered by id ASC, optionally filtered by status."""
    clauses: List[str] = []
    params: list = []
    if status is not None:
        clauses.append('status = ?')
        params.append(status)
    where = f'WHERE {" AND ".join(clauses)}' if clauses else ''
    params.append(limit)

    conn = _mem_connect(db_path)
    try:
        rows = conn.execute(
            f'SELECT * FROM cognition_session {where} ORDER BY id ASC LIMIT ?',
            params,
        ).fetchall()
        return [CognitionSession.from_row(r) for r in rows]
    finally:
        conn.close()


def get_cognition_session(db_path: str, cognition_session_id: int) -> CognitionSession:
    """Fetch one cognition session by id. Raises ValueError if not found."""
    conn = _mem_connect(db_path)
    try:
        row = conn.execute(
            'SELECT * FROM cognition_session WHERE id = ?', (cognition_session_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"Cognition session {cognition_session_id} not found")
        return CognitionSession.from_row(row)
    finally:
        conn.close()


def replay_session_timeline(
    cognition_session_id: int,
    db_path: str,
) -> List[SessionReconstruction]:
    """
    Replay all assemblies in a cognition session in chronological order.

    Pure snapshot replay: loads assembly_snapshot_json for each transition's
    to_assembly_id via replay_assembly(). Does not call reconstruct(), activate_memory(),
    or any retrieval function. Returns List[SessionReconstruction] with replayed=True.

    Raises ValueError if cognition_session_id not found.
    """
    conn = _mem_connect(db_path)
    try:
        row = conn.execute(
            'SELECT id FROM cognition_session WHERE id = ?', (cognition_session_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"Cognition session {cognition_session_id} not found")

        transitions = conn.execute(
            """SELECT to_assembly_id FROM assembly_transition_log
               WHERE cognition_session_id = ?
               ORDER BY sequence_index ASC""",
            (cognition_session_id,),
        ).fetchall()
    finally:
        conn.close()

    return [replay_assembly(t['to_assembly_id'], db_path) for t in transitions]


def get_session_assemblies(
    cognition_session_id: int,
    db_path: str,
) -> List[dict]:
    """
    Return transition rows enriched with assembly fields, ordered by sequence_index ASC.

    Pure read — no reconstruction. Returns plain dicts with all assembly_transition_log
    columns plus assembly_hash, assembly_version, assembled_at, entries_accepted,
    char_budget_used, char_budget_limit, assembly_status from context_assembly_log.

    Raises ValueError if cognition_session_id not found.
    """
    conn = _mem_connect(db_path)
    try:
        sess_row = conn.execute(
            'SELECT id FROM cognition_session WHERE id = ?', (cognition_session_id,)
        ).fetchone()
        if sess_row is None:
            raise ValueError(f"Cognition session {cognition_session_id} not found")

        rows = conn.execute(
            """SELECT
                   atl.*,
                   cal.assembly_hash,
                   cal.assembly_version,
                   cal.assembled_at,
                   cal.entries_accepted,
                   cal.char_budget_used,
                   cal.char_budget_limit,
                   cal.status AS assembly_status
               FROM assembly_transition_log atl
               JOIN context_assembly_log cal ON cal.id = atl.to_assembly_id
               WHERE atl.cognition_session_id = ?
               ORDER BY atl.sequence_index ASC""",
            (cognition_session_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def verify_session_timeline(
    cognition_session_id: int,
    db_path: str,
) -> SessionTimelineDivergenceReport:
    """
    Run verify_assembly_against_current_db() for each assembly in the session.

    Returns a SessionTimelineDivergenceReport ordered by sequence_index ASC.
    diverged=True if any individual assembly report has diverged=True.

    This re-runs reconstruct() for each assembly — it is verification, not replay.
    Raises ValueError if cognition_session_id not found.
    """
    conn = _mem_connect(db_path)
    try:
        sess_row = conn.execute(
            'SELECT id FROM cognition_session WHERE id = ?', (cognition_session_id,)
        ).fetchone()
        if sess_row is None:
            raise ValueError(f"Cognition session {cognition_session_id} not found")

        transitions = conn.execute(
            """SELECT to_assembly_id FROM assembly_transition_log
               WHERE cognition_session_id = ?
               ORDER BY sequence_index ASC""",
            (cognition_session_id,),
        ).fetchall()
    finally:
        conn.close()

    reports = [
        verify_assembly_against_current_db(t['to_assembly_id'], db_path)
        for t in transitions
    ]

    return SessionTimelineDivergenceReport(
        cognition_session_id=cognition_session_id,
        diverged=any(r.diverged for r in reports),
        assembly_reports=reports,
    )

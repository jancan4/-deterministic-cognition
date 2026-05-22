"""
Deterministic workflow execution state engine.

Pure functions only — no database, no network, no randomization. Every
function receives a WorkflowExecution and returns a NEW instance alongside
a list of WorkflowExecutionLineageEvents. Callers own persistence.

Design invariants:
- WorkflowExecution is treated as immutable; dataclasses.replace produces
  updated copies.
- Lineage events are always returned alongside state changes — callers must
  not discard them.
- Stage advancement is automatic inside record_node_completed when a stage
  fully completes.
- 'blocked' outcome = no nodes can make progress; some failed, others depend
  on them. The execution is stuck pending operator intervention.
- Retry tracking is in node_attempts; a node with retries remaining is NOT
  added to failed_node_ids and remains logically available for re-submission.
"""
import dataclasses
from datetime import datetime, timezone
from typing import Dict, FrozenSet, List, Optional, Set, Tuple

from .models import WorkflowDefinition, WorkflowExecutionPlan
from .state import (
    EVENT_NODE_COMPLETED,
    EVENT_NODE_FAILED,
    EVENT_NODE_RETRY,
    EVENT_STAGE_ADVANCED,
    EVENT_STATE_TRANSITION,
    WorkflowExecution,
    WorkflowExecutionLineageEvent,
    WorkflowStageExecution,
    make_execution_id,
    validate_execution_transition,
)


def _now() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _transition(
    execution: WorkflowExecution,
    new_state: str,
    reason: str,
) -> Tuple[WorkflowExecution, WorkflowExecutionLineageEvent]:
    """Apply a validated state transition; return new execution + lineage event."""
    validate_execution_transition(execution.state, new_state)
    old_state = execution.state
    now = _now()
    updated = dataclasses.replace(
        execution,
        state=new_state,
        updated_at=now,
        version=execution.version + 1,
    )
    evt = WorkflowExecutionLineageEvent(
        execution_id=execution.execution_id,
        event_type=EVENT_STATE_TRANSITION,
        old_state=old_state,
        new_state=new_state,
        node_id=None,
        stage_index=execution.active_stage_index,
        reason=reason,
        created_at=now,
    )
    return updated, evt


def _compute_downstream_blocked(
    failed_node_ids: FrozenSet[str],
    plan: WorkflowExecutionPlan,
) -> Set[str]:
    """
    BFS over the reverse dependency graph from failed nodes.
    Returns the set of node_ids that are permanently blocked because at least
    one ancestor is in failed_node_ids.
    """
    # Build reverse adjacency: dep_id → sorted list of nodes that depend on it.
    reverse_adj: Dict[str, List[str]] = {}
    for nid, deps in plan.dependency_snapshot.items():
        for dep_id in deps:
            reverse_adj.setdefault(dep_id, []).append(nid)

    blocked: Set[str] = set()
    queue = sorted(failed_node_ids)  # sorted for determinism
    visited: Set[str] = set(failed_node_ids)

    while queue:
        nid = queue.pop(0)
        for downstream in sorted(reverse_adj.get(nid, [])):
            if downstream not in visited:
                visited.add(downstream)
                blocked.add(downstream)
                queue.append(downstream)

    return blocked


def detect_outcome(
    execution: WorkflowExecution,
    plan: WorkflowExecutionPlan,
) -> str:
    """
    Determine the logical progress state of the execution:

      'completed' — every node in the plan is in completed_node_ids
      'executing' — at least one node can still make progress
      'blocked'   — no nodes can make progress; some failed and everything
                    remaining depends (directly or transitively) on them
      'failed'    — execution is already in the 'failed' terminal state
      'cancelled' — execution is already in the 'cancelled' terminal state

    This is read-only — it never mutates the execution.
    """
    if execution.state in ('failed', 'cancelled', 'completed'):
        return execution.state

    all_node_ids = [n for stage in plan.stages for n in stage.node_ids]

    if all(n in execution.completed_node_ids for n in all_node_ids):
        return 'completed'

    if not execution.failed_node_ids:
        return 'executing'

    downstream_blocked = _compute_downstream_blocked(
        frozenset(execution.failed_node_ids), plan
    )
    completed = frozenset(execution.completed_node_ids)
    failed = frozenset(execution.failed_node_ids)

    still_runnable = [
        n for n in all_node_ids
        if n not in completed and n not in failed and n not in downstream_blocked
    ]
    return 'executing' if still_runnable else 'blocked'


def initialize_execution(
    plan: WorkflowExecutionPlan,
) -> Tuple[WorkflowExecution, WorkflowExecutionLineageEvent]:
    """
    Create a WorkflowExecution in 'initialized' state from an execution plan.
    Returns (execution, initial_lineage_event).
    """
    now = _now()
    execution_id = make_execution_id(plan.plan_id, now)
    execution = WorkflowExecution(
        execution_id=execution_id,
        workflow_id=plan.workflow_id,
        plan_id=plan.plan_id,
        state='initialized',
        active_stage_index=0,
        completed_node_ids=[],
        failed_node_ids=[],
        node_attempts={},
        created_at=now,
        updated_at=now,
        version=1,
    )
    evt = WorkflowExecutionLineageEvent(
        execution_id=execution_id,
        event_type=EVENT_STATE_TRANSITION,
        old_state=None,
        new_state='initialized',
        node_id=None,
        stage_index=0,
        reason='Execution initialized from plan',
        created_at=now,
        # Embed identity so that lineage is self-contained: pure replay can recover
        # workflow_id and plan_id without relying on the mutable state row.
        metadata={'workflow_id': plan.workflow_id, 'plan_id': plan.plan_id},
    )
    return execution, evt


def start_execution(
    execution: WorkflowExecution,
) -> Tuple[WorkflowExecution, List[WorkflowExecutionLineageEvent]]:
    """
    Transition initialized → ready → executing.
    Returns updated execution and the two transition events.
    """
    ready_exec, evt1 = _transition(execution, 'ready', 'Execution starting')
    exec_exec, evt2 = _transition(ready_exec, 'executing', 'Execution in progress')
    return exec_exec, [evt1, evt2]


def get_ready_node_ids(
    execution: WorkflowExecution,
    plan: WorkflowExecutionPlan,
) -> List[str]:
    """
    Return node_ids whose dependencies are all satisfied and that have not
    yet completed or permanently failed. Traverses stages in order so results
    preserve plan execution sequencing.

    Nodes with retries remaining (not in failed_node_ids) are returned here —
    the coordination layer is responsible for not double-submitting them if
    they already have an active task.
    """
    completed = frozenset(execution.completed_node_ids)
    failed = frozenset(execution.failed_node_ids)
    ready = []
    for stage in plan.stages:
        for nid in stage.node_ids:
            if nid in completed or nid in failed:
                continue
            deps = plan.dependency_snapshot.get(nid, [])
            if all(dep in completed for dep in deps):
                ready.append(nid)
    return ready


def get_blocked_node_ids(
    execution: WorkflowExecution,
    plan: WorkflowExecutionPlan,
) -> List[str]:
    """
    Return node_ids that are permanently blocked (an ancestor is in
    failed_node_ids). Does not include already-completed or failed nodes.
    """
    if not execution.failed_node_ids:
        return []
    blocked = _compute_downstream_blocked(frozenset(execution.failed_node_ids), plan)
    completed = frozenset(execution.completed_node_ids)
    failed = frozenset(execution.failed_node_ids)
    return [
        nid
        for stage in plan.stages
        for nid in stage.node_ids
        if nid in blocked and nid not in completed and nid not in failed
    ]


def compute_stage_execution(
    execution: WorkflowExecution,
    plan: WorkflowExecutionPlan,
    stage_index: int,
) -> WorkflowStageExecution:
    """Compute a read-only progress snapshot for one stage."""
    stage = plan.stages[stage_index]
    node_ids = list(stage.node_ids)
    completed = [n for n in node_ids if n in execution.completed_node_ids]
    failed = [n for n in node_ids if n in execution.failed_node_ids]
    pending = [n for n in node_ids if n not in execution.completed_node_ids and n not in execution.failed_node_ids]
    return WorkflowStageExecution(
        stage_index=stage_index,
        node_ids=node_ids,
        completed_node_ids=completed,
        failed_node_ids=failed,
        pending_node_ids=pending,
        is_complete=len(completed) == len(node_ids),
        has_failures=len(failed) > 0,
    )


def _advance_stage_if_needed(
    execution: WorkflowExecution,
    plan: WorkflowExecutionPlan,
) -> Tuple[WorkflowExecution, List[WorkflowExecutionLineageEvent]]:
    """
    Advance active_stage_index while the current stage is fully complete.
    Stage advancement only happens when every node in the stage is in
    completed_node_ids — a stage with any failures does not advance.
    """
    events: List[WorkflowExecutionLineageEvent] = []
    exec_ = execution

    while exec_.active_stage_index < len(plan.stages):
        stage = plan.stages[exec_.active_stage_index]
        if not all(n in exec_.completed_node_ids for n in stage.node_ids):
            break
        next_index = exec_.active_stage_index + 1
        now = _now()
        evt = WorkflowExecutionLineageEvent(
            execution_id=exec_.execution_id,
            event_type=EVENT_STAGE_ADVANCED,
            old_state=None,
            new_state=None,
            node_id=None,
            stage_index=exec_.active_stage_index,
            reason=f'Stage {exec_.active_stage_index} complete; advancing to {next_index}',
            created_at=now,
        )
        events.append(evt)
        exec_ = dataclasses.replace(
            exec_,
            active_stage_index=next_index,
            updated_at=now,
            version=exec_.version + 1,
        )

    return exec_, events


def record_node_completed(
    execution: WorkflowExecution,
    plan: WorkflowExecutionPlan,
    node_id: str,
    reason: str = 'Node completed successfully',
) -> Tuple[WorkflowExecution, List[WorkflowExecutionLineageEvent]]:
    """
    Mark node_id as completed. Automatically advances active_stage_index when
    a stage finishes. Transitions execution to 'completed' if all nodes are done,
    or 'blocked' if the remaining nodes are permanently blocked by failures.
    """
    events: List[WorkflowExecutionLineageEvent] = []
    now = _now()

    new_completed = sorted(set(execution.completed_node_ids) | {node_id})
    exec_ = dataclasses.replace(
        execution,
        completed_node_ids=new_completed,
        updated_at=now,
        version=execution.version + 1,
    )
    events.append(WorkflowExecutionLineageEvent(
        execution_id=exec_.execution_id,
        event_type=EVENT_NODE_COMPLETED,
        old_state=None,
        new_state=None,
        node_id=node_id,
        stage_index=exec_.active_stage_index,
        reason=reason,
        created_at=now,
    ))

    exec_, stage_events = _advance_stage_if_needed(exec_, plan)
    events.extend(stage_events)

    outcome = detect_outcome(exec_, plan)
    if outcome == 'completed':
        exec_, t_evt = _transition(exec_, 'completed', 'All workflow nodes completed successfully')
        events.append(t_evt)
    elif outcome == 'blocked':
        exec_, t_evt = _transition(exec_, 'blocked', 'Remaining nodes blocked by prior failures')
        events.append(t_evt)

    return exec_, events


def record_node_failed(
    execution: WorkflowExecution,
    plan: WorkflowExecutionPlan,
    definition: WorkflowDefinition,
    node_id: str,
    reason: str = 'Node failed',
) -> Tuple[WorkflowExecution, List[WorkflowExecutionLineageEvent]]:
    """
    Record a node failure.

    If the node's retry_policy permits another attempt, increment node_attempts
    and emit a 'node_retry' event. The node is NOT added to failed_node_ids and
    remains logically ready for re-submission.

    If retries are exhausted, add to failed_node_ids and emit 'node_failed'.
    Transition the execution to 'blocked' if no progress is now possible, or
    leave it 'executing' if independent branches remain.
    """
    events: List[WorkflowExecutionLineageEvent] = []
    now = _now()

    node_map = {n.node_id: n for n in definition.nodes}
    max_attempts = node_map[node_id].retry_policy.max_attempts
    attempts_made = execution.node_attempts.get(node_id, 0) + 1
    new_attempts = {**execution.node_attempts, node_id: attempts_made}

    if attempts_made < max_attempts:
        # Retry available — node remains "pending retry"; not added to failed_node_ids.
        exec_ = dataclasses.replace(
            execution,
            node_attempts=new_attempts,
            updated_at=now,
            version=execution.version + 1,
        )
        events.append(WorkflowExecutionLineageEvent(
            execution_id=exec_.execution_id,
            event_type=EVENT_NODE_RETRY,
            old_state=None,
            new_state=None,
            node_id=node_id,
            stage_index=exec_.active_stage_index,
            reason=f'Attempt {attempts_made}/{max_attempts}: {reason}',
            created_at=now,
        ))
        return exec_, events

    # Retries exhausted — add to failed_node_ids.
    new_failed = sorted(set(execution.failed_node_ids) | {node_id})
    exec_ = dataclasses.replace(
        execution,
        failed_node_ids=new_failed,
        node_attempts=new_attempts,
        updated_at=now,
        version=execution.version + 1,
    )
    events.append(WorkflowExecutionLineageEvent(
        execution_id=exec_.execution_id,
        event_type=EVENT_NODE_FAILED,
        old_state=None,
        new_state=None,
        node_id=node_id,
        stage_index=exec_.active_stage_index,
        reason=f'All {attempts_made}/{max_attempts} attempts exhausted: {reason}',
        created_at=now,
    ))

    outcome = detect_outcome(exec_, plan)
    if outcome == 'blocked':
        exec_, t_evt = _transition(exec_, 'blocked', f'Node {node_id} failed; no progress possible')
        events.append(t_evt)

    return exec_, events

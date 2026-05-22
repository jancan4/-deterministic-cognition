"""
Workflow-to-orchestration coordination layer.

Bridges workflow execution state to the task orchestration system. Creates
orchestration tasks for ready workflow nodes, prevents double-submission by
checking for active tasks, and reacts to task outcomes.

Governance constraint: no autonomous replanning. Every submission is
deterministic given the current execution state and plan.

Integration notes:
- Workflow node task_type must be a value from orchestration.models.VALID_TASK_TYPES.
- Workflow context (execution_id, node_id) is stored in the orchestration task's
  metadata dict for full lineage traceability.
- Orchestration task priority is fixed at 3 (middle) for all workflow-submitted
  tasks; within-stage ordering is determined by the planner, not the orchestration
  priority queue.
"""
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from orchestration.service import create_task, list_tasks, transition_task

from .executor import (
    detect_outcome,
    get_ready_node_ids,
    record_node_completed,
    record_node_failed,
)
from .models import WorkflowDefinition, WorkflowExecutionPlan
from .state import (
    EVENT_NODE_SUBMITTED,
    WorkflowExecution,
    WorkflowExecutionLineageEvent,
)

_EXEC_ID_KEY = 'workflow_execution_id'
_NODE_ID_KEY = 'workflow_node_id'
_ACTIVE_TASK_STATES = frozenset({'pending', 'ready', 'running'})

# Default orchestration priority for all workflow-submitted tasks.
_TASK_PRIORITY = 3


def _now() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def build_task_metadata(execution_id: str, node_id: str) -> Dict[str, Any]:
    """Deterministic metadata dict linking an orchestration task to a workflow node."""
    return {_EXEC_ID_KEY: execution_id, _NODE_ID_KEY: node_id}


def extract_node_id(task) -> Optional[str]:
    """Extract the workflow node_id from a task's metadata dict. None if absent."""
    return task.metadata.get(_NODE_ID_KEY)


def extract_execution_id(task) -> Optional[str]:
    """Extract the workflow execution_id from a task's metadata dict. None if absent."""
    return task.metadata.get(_EXEC_ID_KEY)


def find_submitted_node_ids(
    orchestration_db: str,
    execution_id: str,
) -> Set[str]:
    """
    Return node_ids that already have active (pending/ready/running) orchestration
    tasks for this execution. Used to prevent double-submission.

    Queries metadata_json directly to avoid the list_tasks limit ceiling.
    """
    conn = sqlite3.connect(orchestration_db)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    try:
        rows = conn.execute(
            "SELECT metadata_json, state FROM tasks"
            " WHERE state IN ('pending','ready','running')"
        ).fetchall()
    finally:
        conn.close()

    submitted: Set[str] = set()
    for row in rows:
        try:
            meta = json.loads(row['metadata_json'] or '{}')
        except (json.JSONDecodeError, ValueError):
            continue
        if meta.get(_EXEC_ID_KEY) == execution_id:
            nid = meta.get(_NODE_ID_KEY)
            if nid:
                submitted.add(nid)
    return submitted


def submit_ready_nodes(
    orchestration_db: str,
    execution: WorkflowExecution,
    plan: WorkflowExecutionPlan,
    definition: WorkflowDefinition,
    actor: str,
) -> Tuple[List, List[WorkflowExecutionLineageEvent]]:
    """
    For each ready workflow node that does not already have an active orchestration
    task, create a task and immediately transition it pending → ready.

    Submission order follows the plan's stage + priority ordering — deterministic.
    Returns (submitted_tasks, lineage_events).
    """
    node_map = {n.node_id: n for n in definition.nodes}
    ready_ids = get_ready_node_ids(execution, plan)
    already_submitted = find_submitted_node_ids(orchestration_db, execution.execution_id)

    submitted_tasks: List = []
    events: List[WorkflowExecutionLineageEvent] = []

    for node_id in ready_ids:
        if node_id in already_submitted:
            continue
        node = node_map[node_id]
        metadata = build_task_metadata(execution.execution_id, node_id)

        task = create_task(
            orchestration_db,
            title=f'wf:{execution.workflow_id}:{node_id}',
            task_type=node.task_type,
            actor=actor,
            priority=_TASK_PRIORITY,
            metadata=metadata,
            reason=f'Workflow coordinator: submitting node {node_id}',
        )
        task = transition_task(
            orchestration_db, task.id, 'ready',
            reason='Workflow coordinator: node ready for execution',
            actor=actor,
        )
        submitted_tasks.append(task)

        events.append(WorkflowExecutionLineageEvent(
            execution_id=execution.execution_id,
            event_type=EVENT_NODE_SUBMITTED,
            old_state=None,
            new_state=None,
            node_id=node_id,
            stage_index=execution.active_stage_index,
            reason=f'Submitted as orchestration task {task.id}',
            created_at=task.updated_at,
        ))

    return submitted_tasks, events


def handle_task_result(
    execution: WorkflowExecution,
    plan: WorkflowExecutionPlan,
    definition: WorkflowDefinition,
    node_id: str,
    success: bool,
    reason: str = '',
) -> Tuple[WorkflowExecution, List[WorkflowExecutionLineageEvent]]:
    """
    React to a task outcome (completion or failure). Delegates to the executor
    layer for state transitions and lineage. Returns updated execution + events.
    """
    if success:
        return record_node_completed(
            execution, plan, node_id,
            reason=reason or 'Task completed successfully',
        )
    else:
        return record_node_failed(
            execution, plan, definition, node_id,
            reason=reason or 'Task failed',
        )


def step_execution(
    orchestration_db: str,
    execution: WorkflowExecution,
    plan: WorkflowExecutionPlan,
    definition: WorkflowDefinition,
    actor: str,
) -> Tuple[WorkflowExecution, List, List[WorkflowExecutionLineageEvent]]:
    """
    One coordination cycle: submit all ready nodes that have no active tasks.
    Does not block or wait — the caller drives the execution loop by calling
    step_execution and then handle_task_result as tasks complete.

    Returns (execution, submitted_tasks, lineage_events).
    The execution state is unchanged by this call; only node submissions occur.
    """
    submitted, events = submit_ready_nodes(
        orchestration_db, execution, plan, definition, actor
    )
    return execution, submitted, events

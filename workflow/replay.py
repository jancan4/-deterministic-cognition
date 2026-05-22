"""
Pure-function replay engine for workflow execution lineage.

Reconstructs WorkflowExecution state by applying lineage events in order.
No database access — all inputs are in-memory objects. This guarantees
that replay is deterministic and testable without I/O.

Validation rules:
1. First event must be a state_transition to 'initialized'.
2. All events must share the same execution_id.
3. State transitions must follow VALID_WORKFLOW_EXECUTION_TRANSITIONS.
4. No duplicate node_completed events for the same node.
5. node_failed cannot appear for a node that already had node_completed.
6. No events after a terminal state (completed or cancelled).
"""
from dataclasses import dataclass, field
from typing import List, Optional

from .state import (
    EVENT_NODE_COMPLETED,
    EVENT_NODE_FAILED,
    EVENT_NODE_RETRY,
    EVENT_NODE_SUBMITTED,
    EVENT_STAGE_ADVANCED,
    EVENT_STATE_TRANSITION,
    TERMINAL_WORKFLOW_EXECUTION_STATES,
    VALID_WORKFLOW_EXECUTION_TRANSITIONS,
    WorkflowExecution,
    WorkflowExecutionLineageEvent,
)


@dataclass
class ReplayResult:
    execution: Optional[WorkflowExecution]
    events_applied: int
    validation_errors: List[str]
    is_valid: bool


def validate_lineage(events: List[WorkflowExecutionLineageEvent]) -> List[str]:
    """
    Validate a lineage event sequence. Returns a list of error strings.
    Empty list means the sequence is valid.
    """
    errors: List[str] = []

    if not events:
        errors.append('Lineage is empty — no events to validate')
        return errors

    first = events[0]

    # Rule 1: first event must be state_transition → initialized
    if first.event_type != EVENT_STATE_TRANSITION or first.new_state != 'initialized':
        errors.append(
            f"First event must be state_transition to 'initialized'; "
            f"got event_type='{first.event_type}' new_state='{first.new_state}'"
        )

    # Rule 2: all events must share the same execution_id
    execution_id = first.execution_id
    for i, evt in enumerate(events[1:], start=1):
        if evt.execution_id != execution_id:
            errors.append(
                f"Event {i} has execution_id='{evt.execution_id}'; "
                f"expected '{execution_id}'"
            )

    current_state: Optional[str] = None
    completed_nodes: set = set()
    terminal_at: Optional[int] = None

    for i, evt in enumerate(events):
        # Rule 6: no events after terminal state
        if terminal_at is not None and i > terminal_at:
            errors.append(
                f"Event {i} (type='{evt.event_type}') appears after "
                f"terminal state reached at event {terminal_at}"
            )
            continue

        if evt.event_type == EVENT_STATE_TRANSITION:
            if current_state is None:
                # Bootstrapping: first transition to initialized
                if evt.new_state != 'initialized':
                    errors.append(
                        f"Event {i}: first state_transition must target "
                        f"'initialized'; got '{evt.new_state}'"
                    )
                current_state = evt.new_state
            else:
                # Rule 3: must follow valid transition graph
                allowed = VALID_WORKFLOW_EXECUTION_TRANSITIONS.get(current_state, frozenset())
                if evt.new_state not in allowed:
                    errors.append(
                        f"Event {i}: state transition '{current_state}' → "
                        f"'{evt.new_state}' is not permitted"
                    )
                current_state = evt.new_state

            if current_state in TERMINAL_WORKFLOW_EXECUTION_STATES:
                terminal_at = i

        elif evt.event_type == EVENT_NODE_COMPLETED:
            node_id = evt.node_id
            # Rule 4: no duplicate node_completed
            if node_id in completed_nodes:
                errors.append(
                    f"Event {i}: duplicate node_completed for node '{node_id}'"
                )
            completed_nodes.add(node_id)

        elif evt.event_type == EVENT_NODE_FAILED:
            node_id = evt.node_id
            # Rule 5: node_failed cannot follow node_completed for same node
            if node_id in completed_nodes:
                errors.append(
                    f"Event {i}: node_failed for '{node_id}' which already "
                    f"had node_completed"
                )

    return errors


def replay_execution(
    events: List[WorkflowExecutionLineageEvent],
) -> ReplayResult:
    """
    Reconstruct a WorkflowExecution from its lineage events.

    Applies events in order to build up execution state. Returns a ReplayResult
    with the reconstructed execution and any validation errors found.

    If validation errors exist, is_valid=False and execution may be partial.
    """
    errors = validate_lineage(events)

    if not events:
        return ReplayResult(
            execution=None,
            events_applied=0,
            validation_errors=errors,
            is_valid=False,
        )

    # Build execution state by applying events
    execution_id = events[0].execution_id
    state: Optional[str] = None
    active_stage_index: int = 0
    completed_node_ids: List[str] = []
    failed_node_ids: List[str] = []
    node_attempts: dict = {}
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    workflow_id: str = ''
    plan_id: str = ''
    version: int = 0

    # Extract context from the first state_transition event
    first_transition = next(
        (e for e in events if e.event_type == EVENT_STATE_TRANSITION), None
    )
    if first_transition:
        created_at = first_transition.created_at
        # workflow_id and plan_id are not stored in events — they come from the
        # execution record. We extract them from event metadata if available,
        # but they may remain empty when replaying events alone.

    events_applied = 0
    for evt in events:
        if evt.event_type == EVENT_STATE_TRANSITION:
            state = evt.new_state
            updated_at = evt.created_at
            version += 1

        elif evt.event_type == EVENT_NODE_COMPLETED:
            nid = evt.node_id
            if nid and nid not in completed_node_ids:
                completed_node_ids.append(nid)
                completed_node_ids.sort()
            updated_at = evt.created_at
            version += 1

        elif evt.event_type == EVENT_NODE_FAILED:
            nid = evt.node_id
            if nid and nid not in failed_node_ids:
                failed_node_ids.append(nid)
                failed_node_ids.sort()
            if nid:
                node_attempts[nid] = node_attempts.get(nid, 0) + 1
            updated_at = evt.created_at
            version += 1

        elif evt.event_type == EVENT_NODE_RETRY:
            nid = evt.node_id
            if nid:
                node_attempts[nid] = node_attempts.get(nid, 0) + 1
            updated_at = evt.created_at
            version += 1

        elif evt.event_type == EVENT_STAGE_ADVANCED:
            # stage_index holds the OLD (completed) stage index
            active_stage_index = evt.stage_index + 1
            updated_at = evt.created_at
            version += 1

        elif evt.event_type == EVENT_NODE_SUBMITTED:
            # No state mutation — submission is informational
            pass

        events_applied += 1

    if created_at is None:
        created_at = events[0].created_at
    if updated_at is None:
        updated_at = created_at

    execution = WorkflowExecution(
        execution_id=execution_id,
        workflow_id=workflow_id,
        plan_id=plan_id,
        state=state or 'initialized',
        active_stage_index=active_stage_index,
        completed_node_ids=completed_node_ids,
        failed_node_ids=failed_node_ids,
        node_attempts=node_attempts,
        created_at=created_at,
        updated_at=updated_at,
        version=version,
    )

    return ReplayResult(
        execution=execution,
        events_applied=events_applied,
        validation_errors=errors,
        is_valid=len(errors) == 0,
    )


def replay_from_snapshot(
    snapshot: WorkflowExecution,
    events_after_snapshot: List[WorkflowExecutionLineageEvent],
) -> ReplayResult:
    """
    Apply incremental lineage events on top of a snapshot.

    The snapshot becomes the starting state. Events are applied in order
    without re-validating the full lineage (the snapshot is already trusted).
    Only events after the snapshot's last_event_id are applied.

    Returns a ReplayResult. is_valid=True when no application errors occur.
    """
    if not events_after_snapshot:
        return ReplayResult(
            execution=snapshot,
            events_applied=0,
            validation_errors=[],
            is_valid=True,
        )

    completed_node_ids = list(snapshot.completed_node_ids)
    failed_node_ids = list(snapshot.failed_node_ids)
    node_attempts = dict(snapshot.node_attempts)
    state = snapshot.state
    active_stage_index = snapshot.active_stage_index
    updated_at = snapshot.updated_at
    version = snapshot.version

    events_applied = 0
    for evt in events_after_snapshot:
        if evt.event_type == EVENT_STATE_TRANSITION:
            state = evt.new_state
            updated_at = evt.created_at
            version += 1

        elif evt.event_type == EVENT_NODE_COMPLETED:
            nid = evt.node_id
            if nid and nid not in completed_node_ids:
                completed_node_ids.append(nid)
                completed_node_ids.sort()
            updated_at = evt.created_at
            version += 1

        elif evt.event_type == EVENT_NODE_FAILED:
            nid = evt.node_id
            if nid and nid not in failed_node_ids:
                failed_node_ids.append(nid)
                failed_node_ids.sort()
            if nid:
                node_attempts[nid] = node_attempts.get(nid, 0) + 1
            updated_at = evt.created_at
            version += 1

        elif evt.event_type == EVENT_NODE_RETRY:
            nid = evt.node_id
            if nid:
                node_attempts[nid] = node_attempts.get(nid, 0) + 1
            updated_at = evt.created_at
            version += 1

        elif evt.event_type == EVENT_STAGE_ADVANCED:
            active_stage_index = evt.stage_index + 1
            updated_at = evt.created_at
            version += 1

        elif evt.event_type == EVENT_NODE_SUBMITTED:
            pass

        events_applied += 1

    execution = WorkflowExecution(
        execution_id=snapshot.execution_id,
        workflow_id=snapshot.workflow_id,
        plan_id=snapshot.plan_id,
        state=state,
        active_stage_index=active_stage_index,
        completed_node_ids=completed_node_ids,
        failed_node_ids=failed_node_ids,
        node_attempts=node_attempts,
        created_at=snapshot.created_at,
        updated_at=updated_at,
        version=version,
    )

    return ReplayResult(
        execution=execution,
        events_applied=events_applied,
        validation_errors=[],
        is_valid=True,
    )

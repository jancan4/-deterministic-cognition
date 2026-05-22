"""
Execution lineage inspector.

Replays a workflow execution's event log — optionally to a specific event
index — and surfaces the reconstructed state alongside any divergence from
the mutable state row.

All operations are read-only. The inspector never mutates stored state.
"""
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .replay import replay_execution
from .state import WorkflowExecution, WorkflowExecutionLineageEvent
from .storage import load_execution, load_execution_events


@dataclass
class InspectionResult:
    """
    The result of replaying an execution's lineage, optionally to a
    specific event index.
    """
    execution_id: str
    total_events: int                    # total events in the lineage
    replayed_to_event_index: Optional[int]  # None = full replay; N = events[:N]
    state: str                           # reconstructed execution state
    active_stage_index: int
    completed_node_ids: List[str]
    failed_node_ids: List[str]
    node_attempts: Dict[str, int]
    lineage_valid: bool                  # False if validate_lineage found errors
    validation_errors: List[str]
    diverged_from_stored: bool           # True if replayed state differs from mutable row
    divergence_details: List[str]        # per-field divergence descriptions
    events_applied: int                  # how many events were actually replayed


def _field_divergences(
    stored: Optional[WorkflowExecution],
    replayed: WorkflowExecution,
) -> List[str]:
    if stored is None:
        return ['No stored execution row to compare against']
    details = []
    if stored.state != replayed.state:
        details.append(f"state: stored='{stored.state}' replayed='{replayed.state}'")
    if stored.active_stage_index != replayed.active_stage_index:
        details.append(
            f"active_stage_index: stored={stored.active_stage_index} "
            f"replayed={replayed.active_stage_index}"
        )
    if sorted(stored.completed_node_ids) != sorted(replayed.completed_node_ids):
        details.append(
            f"completed_node_ids: stored={sorted(stored.completed_node_ids)} "
            f"replayed={sorted(replayed.completed_node_ids)}"
        )
    if sorted(stored.failed_node_ids) != sorted(replayed.failed_node_ids):
        details.append(
            f"failed_node_ids: stored={sorted(stored.failed_node_ids)} "
            f"replayed={sorted(replayed.failed_node_ids)}"
        )
    if stored.node_attempts != replayed.node_attempts:
        details.append(
            f"node_attempts: stored={stored.node_attempts} "
            f"replayed={replayed.node_attempts}"
        )
    return details


def inspect_execution(
    db_path: str,
    execution_id: str,
    up_to_event_index: Optional[int] = None,
) -> InspectionResult:
    """
    Replay and inspect a workflow execution.

    up_to_event_index: if provided, replay only events[:up_to_event_index]
        (0-based count). Allows reconstruction of state at any historical
        point in the lineage. None replays the full event log.

    Divergence comparison against the mutable state row is only performed
    on a full replay (up_to_event_index=None), since partial replays are
    intentionally historical snapshots.

    The operation is read-only. No state is modified.
    """
    stored = load_execution(db_path, execution_id)
    all_events = load_execution_events(db_path, execution_id)
    total_events = len(all_events)

    if up_to_event_index is not None:
        events_to_replay = all_events[:up_to_event_index]
    else:
        events_to_replay = all_events

    result = replay_execution(events_to_replay)

    if result.execution is None:
        return InspectionResult(
            execution_id=execution_id,
            total_events=total_events,
            replayed_to_event_index=up_to_event_index,
            state='unknown',
            active_stage_index=0,
            completed_node_ids=[],
            failed_node_ids=[],
            node_attempts={},
            lineage_valid=False,
            validation_errors=result.validation_errors or ['No events to replay'],
            diverged_from_stored=stored is not None,
            divergence_details=['Empty lineage; cannot reconstruct state'],
            events_applied=0,
        )

    exec_ = result.execution

    # Only compare against stored row on full replay.
    is_full_replay = up_to_event_index is None
    if is_full_replay:
        divergence_details = _field_divergences(stored, exec_)
    else:
        divergence_details = []

    return InspectionResult(
        execution_id=execution_id,
        total_events=total_events,
        replayed_to_event_index=up_to_event_index,
        state=exec_.state,
        active_stage_index=exec_.active_stage_index,
        completed_node_ids=exec_.completed_node_ids,
        failed_node_ids=exec_.failed_node_ids,
        node_attempts=exec_.node_attempts,
        lineage_valid=result.is_valid,
        validation_errors=result.validation_errors,
        diverged_from_stored=bool(divergence_details),
        divergence_details=divergence_details,
        events_applied=result.events_applied,
    )

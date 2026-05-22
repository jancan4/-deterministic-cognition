"""
Deterministic snapshot policy for workflow execution persistence.

Snapshots are cache-only optimizations — lineage is always canonical truth.
A snapshot allows delta-replay to skip re-applying the full event log.

Two independent trigger conditions:
  1. Stage boundary: snapshot whenever active_stage_index advances.
  2. Event count: snapshot every N events appended since the last snapshot.

Either condition independently fires a snapshot. Both can be active simultaneously.
Setting events_per_snapshot=0 disables the event-count trigger.
Setting snapshot_on_stage_advance=False disables the stage-boundary trigger.
"""
import sqlite3
from dataclasses import dataclass
from typing import List, Optional

from .state import EVENT_STAGE_ADVANCED, WorkflowExecution, WorkflowExecutionLineageEvent
from .storage import persist_snapshot


@dataclass
class SnapshotPolicy:
    """
    Controls when automatic snapshots are taken.

    events_per_snapshot: Take a snapshot every N total events since the last
        snapshot. 0 disables the event-count trigger.
    snapshot_on_stage_advance: Take a snapshot whenever a stage_advanced event
        appears in the batch being persisted.
    """
    events_per_snapshot: int = 50
    snapshot_on_stage_advance: bool = True


def should_snapshot(
    policy: SnapshotPolicy,
    events_since_last_snapshot: int,
    stage_just_advanced: bool = False,
) -> bool:
    """
    Return True if the policy mandates a snapshot given the current counters.
    Pure function — no I/O.
    """
    if policy.snapshot_on_stage_advance and stage_just_advanced:
        return True
    if policy.events_per_snapshot > 0 and events_since_last_snapshot >= policy.events_per_snapshot:
        return True
    return False


def _count_events_since(db_path: str, execution_id: str, after_event_id: int) -> int:
    """Return the count of lineage events with id > after_event_id for this execution."""
    conn = sqlite3.connect(db_path)
    conn.execute('PRAGMA journal_mode=WAL')
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM workflow_execution_events "
            "WHERE execution_id = ? AND id > ?",
            (execution_id, after_event_id),
        ).fetchone()
    finally:
        conn.close()
    return row[0] if row else 0


def apply_snapshot_policy(
    db_path: str,
    policy: SnapshotPolicy,
    execution: WorkflowExecution,
    new_event_ids: List[int],
    new_events: List[WorkflowExecutionLineageEvent],
    last_snapshot_event_id: int = 0,
) -> Optional[int]:
    """
    Evaluate the snapshot policy after appending a batch of events.

    Counts all events since last_snapshot_event_id (not just the current batch)
    so the N-event trigger accumulates correctly across multiple persist calls.

    Returns the snapshot row_id if a snapshot was taken, None otherwise.
    The caller should update their last_snapshot_event_id to max(new_event_ids)
    when a snapshot is taken.

    Does nothing and returns None if new_event_ids is empty.
    """
    if not new_event_ids:
        return None

    events_since = _count_events_since(
        db_path, execution.execution_id, last_snapshot_event_id
    )
    stage_just_advanced = any(e.event_type == EVENT_STAGE_ADVANCED for e in new_events)

    if should_snapshot(policy, events_since, stage_just_advanced):
        last_event_id = max(new_event_ids)
        return persist_snapshot(db_path, execution, last_event_id)

    return None

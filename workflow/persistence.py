"""
High-level workflow persistence API.

Combines storage (SQLite CRUD) and replay (pure functions) into a single
interface. Callers use this module rather than importing storage or replay
directly.

Persistence contract:
- save_execution writes (or upserts) the mutable state row.
- append_execution_events appends lineage events atomically.
- persist_execution saves both the state row and all events in one call.
- replay_execution_from_storage replays from all stored events (full replay).
- replay_execution_from_snapshot replays from the latest snapshot + delta events.

Lineage is canonical. The mutable state row is a cache. If they diverge,
replay from lineage is authoritative.
"""
from typing import List, Optional, Tuple

from .replay import ReplayResult, replay_execution, replay_from_snapshot
from .state import WorkflowExecution, WorkflowExecutionLineageEvent
from .storage import (
    append_execution_events as _append_events,
    load_execution,
    load_execution_events,
    load_latest_snapshot,
    persist_snapshot,
    save_execution,
)


def persist_execution(
    db_path: str,
    execution: WorkflowExecution,
    events: List[WorkflowExecutionLineageEvent],
) -> None:
    """
    Persist execution state and its lineage events atomically.

    Writes the mutable state row first, then appends events. Callers should
    call this after every state transition to keep storage in sync.
    """
    save_execution(db_path, execution)
    if events:
        _append_events(db_path, events)


def append_execution_events(
    db_path: str,
    events: List[WorkflowExecutionLineageEvent],
) -> List[int]:
    """
    Append lineage events without updating the mutable state row.

    Use when events have already been applied in memory and the caller will
    separately call save_execution to sync the state row.
    Returns the row ids of the appended events.
    """
    return _append_events(db_path, events)


def replay_execution_from_storage(
    db_path: str,
    execution_id: str,
) -> ReplayResult:
    """
    Full replay: load all lineage events and reconstruct execution state.

    This is the authoritative recovery path. The resulting execution reflects
    exactly what the lineage records — independent of the mutable state row.
    """
    events = load_execution_events(db_path, execution_id)
    result = replay_execution(events)

    # Restore workflow_id and plan_id from the stored state row if available,
    # since these fields are not embedded in lineage events.
    if result.execution is not None:
        stored = load_execution(db_path, execution_id)
        if stored is not None:
            from dataclasses import replace
            result = ReplayResult(
                execution=replace(
                    result.execution,
                    workflow_id=stored.workflow_id,
                    plan_id=stored.plan_id,
                ),
                events_applied=result.events_applied,
                validation_errors=result.validation_errors,
                is_valid=result.is_valid,
            )

    return result


def replay_execution_from_snapshot(
    db_path: str,
    execution_id: str,
) -> ReplayResult:
    """
    Snapshot-accelerated replay: start from the latest snapshot, apply delta events.

    Falls back to full replay if no snapshot exists. The result is identical
    to replay_execution_from_storage when the snapshot is consistent.
    """
    snapshot_data = load_latest_snapshot(db_path, execution_id)
    if snapshot_data is None:
        return replay_execution_from_storage(db_path, execution_id)

    snapshot, last_event_id = snapshot_data
    delta_events = load_execution_events(db_path, execution_id, after_event_id=last_event_id)
    return replay_from_snapshot(snapshot, delta_events)


def take_snapshot(
    db_path: str,
    execution: WorkflowExecution,
    last_event_id: int,
) -> int:
    """
    Persist a snapshot checkpoint for the given execution.

    The snapshot is tied to last_event_id so that delta-replay knows which
    events to apply on top. Returns the snapshot row id.
    """
    return persist_snapshot(db_path, execution, last_event_id)

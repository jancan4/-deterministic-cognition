"""
Startup recovery loop for workflow executions.

Finds persisted non-terminal executions, replays them from lineage,
compares the replayed state against the mutable state row, and reports
any divergence. Mutation only happens when the caller explicitly requests
it with apply=True.

Lineage is always canonical. The mutable row is always a cache.
Recovery never invents or mutates state beyond what the lineage records.
"""
import sqlite3
from dataclasses import dataclass, field
from typing import List, Optional

from .persistence import replay_execution_from_storage
from .replay import replay_execution
from .state import TERMINAL_WORKFLOW_EXECUTION_STATES, WorkflowExecution
from .storage import load_execution, load_execution_events, save_execution


@dataclass
class RecoveryReport:
    """Result of inspecting one execution for lineage/state consistency."""
    execution_id: str
    stored_state: Optional[str]       # state in the mutable row (None if row absent)
    replayed_state: Optional[str]     # state reconstructed from lineage
    diverged: bool                    # True if stored != replayed on any field
    divergence_details: List[str]     # human-readable descriptions of each divergence
    is_recoverable: bool              # True if lineage is valid and replay succeeded
    events_applied: int               # number of lineage events replayed
    lineage_valid: bool               # True if validate_lineage found no errors


def find_non_terminal_execution_ids(db_path: str) -> List[str]:
    """
    Return execution_ids for all executions whose mutable state row is not
    in a terminal state (completed or cancelled).

    These are candidates for recovery inspection — their lineage may be
    ahead of or consistent with the stored row.
    """
    terminal = tuple(TERMINAL_WORKFLOW_EXECUTION_STATES)
    placeholders = ','.join('?' * len(terminal))
    conn = sqlite3.connect(db_path)
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


def _compare_executions(
    stored: Optional[WorkflowExecution],
    replayed: WorkflowExecution,
) -> List[str]:
    """
    Compare stored and replayed executions field by field.
    Returns a list of divergence descriptions (empty = consistent).
    """
    if stored is None:
        return ['Stored execution row absent; identity restored from lineage metadata']

    details = []
    if stored.state != replayed.state:
        details.append(
            f"state: stored='{stored.state}' replayed='{replayed.state}'"
        )
    if stored.active_stage_index != replayed.active_stage_index:
        details.append(
            f"active_stage_index: stored={stored.active_stage_index} "
            f"replayed={replayed.active_stage_index}"
        )
    stored_completed = sorted(stored.completed_node_ids)
    replayed_completed = sorted(replayed.completed_node_ids)
    if stored_completed != replayed_completed:
        details.append(
            f"completed_node_ids: stored={stored_completed} "
            f"replayed={replayed_completed}"
        )
    stored_failed = sorted(stored.failed_node_ids)
    replayed_failed = sorted(replayed.failed_node_ids)
    if stored_failed != replayed_failed:
        details.append(
            f"failed_node_ids: stored={stored_failed} "
            f"replayed={replayed_failed}"
        )
    if stored.node_attempts != replayed.node_attempts:
        details.append(
            f"node_attempts: stored={stored.node_attempts} "
            f"replayed={replayed.node_attempts}"
        )
    return details


def recover_execution(db_path: str, execution_id: str) -> RecoveryReport:
    """
    Dry-run recovery: replay from lineage, compare against stored state.
    Does not modify any state regardless of divergence.

    Returns a RecoveryReport describing the outcome.
    """
    stored = load_execution(db_path, execution_id)
    events = load_execution_events(db_path, execution_id)
    result = replay_execution(events)

    if result.execution is None:
        return RecoveryReport(
            execution_id=execution_id,
            stored_state=stored.state if stored else None,
            replayed_state=None,
            diverged=True,
            divergence_details=['Lineage is empty; cannot reconstruct state'],
            is_recoverable=False,
            events_applied=0,
            lineage_valid=False,
        )

    divergence_details: List[str] = []
    if not result.is_valid:
        divergence_details.extend(
            f"Lineage error: {e}" for e in result.validation_errors
        )
    divergence_details.extend(_compare_executions(stored, result.execution))

    # Patch identity from stored row if available (stored row is still the
    # authority for workflow_id/plan_id when lineage metadata is absent).
    replayed_state = result.execution.state

    return RecoveryReport(
        execution_id=execution_id,
        stored_state=stored.state if stored else None,
        replayed_state=replayed_state,
        diverged=bool(divergence_details),
        divergence_details=divergence_details,
        is_recoverable=result.is_valid,
        events_applied=result.events_applied,
        lineage_valid=result.is_valid,
    )


def apply_recovery(db_path: str, execution_id: str) -> RecoveryReport:
    """
    Apply recovery: replay from lineage and write the reconstructed state
    back to the mutable execution row.

    Only writes if lineage is valid (is_recoverable=True). The mutable row
    after apply reflects exactly what the lineage records — no additional
    mutation occurs.

    Returns the RecoveryReport describing what was (or was not) applied.
    """
    report = recover_execution(db_path, execution_id)
    if not report.is_recoverable:
        return report

    # replay_execution_from_storage patches workflow_id/plan_id from the mutable row.
    result = replay_execution_from_storage(db_path, execution_id)
    if result.execution is not None and result.is_valid:
        save_execution(db_path, result.execution)

    return report


def recover_all(
    db_path: str,
    apply: bool = False,
) -> List[RecoveryReport]:
    """
    Scan all non-terminal executions and produce RecoveryReports.

    apply=False (default): dry run — reports divergence without mutating state.
    apply=True: write recovered state back to mutable rows for valid lineages.

    Terminal executions (completed/cancelled) are intentionally excluded —
    their state is immutable and recovery is a no-op.
    """
    execution_ids = find_non_terminal_execution_ids(db_path)
    reports = []
    for eid in execution_ids:
        if apply:
            report = apply_recovery(db_path, eid)
        else:
            report = recover_execution(db_path, eid)
        reports.append(report)
    return reports

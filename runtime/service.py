"""
Orchestration integration for the runtime supervisor.

Bridges the runtime layer to the task orchestration layer. All reads and
writes to the orchestration database go through these functions so the runner
has a single, testable seam.
"""
import sqlite3
from typing import List, Optional

from orchestration.models import Task
from orchestration.service import (
    NotFoundError,
    ValidationError,
    list_tasks,
    transition_task,
)

from .handlers import TaskHandlerRegistry, execute_handler


def poll_ready_tasks(orchestration_db: str) -> List[Task]:
    """Return all tasks in the 'ready' state, ordered by priority then id."""
    return list_tasks(orchestration_db, state='ready')


def execute_task(
    orchestration_db: str,
    task_id: int,
    actor: str,
    registry: Optional[TaskHandlerRegistry] = None,
) -> Task:
    """
    Transition a task from ready → running, then dispatch to a registered handler.

    On handler success:
        running → completed (reason: 'Handler succeeded: {task_type}')
    On handler failure or exception:
        running → failed   (reason: 'Handler failed: {error}')
    If no handler is registered (registry is None or task_type unregistered):
        running → failed   (reason: 'missing_handler:{task_type}')

    No handler exception propagates to the caller. All outcomes produce a
    deterministic task lineage record and a returned Task in its final state.
    """
    running_task = transition_task(
        orchestration_db, task_id, 'running',
        reason='Runtime: task execution started', actor=actor,
    )

    if registry is None or not registry.has(running_task.task_type):
        return transition_task(
            orchestration_db, task_id, 'failed',
            reason=f'missing_handler:{running_task.task_type}', actor=actor,
        )

    result = execute_handler(registry, running_task)

    if result.success:
        return transition_task(
            orchestration_db, task_id, 'completed',
            reason=f'Handler succeeded: {running_task.task_type}', actor=actor,
        )
    else:
        return transition_task(
            orchestration_db, task_id, 'failed',
            reason=f'Handler failed: {result.error}', actor=actor,
        )


def count_task_retries(orchestration_db: str, task_id: int) -> int:
    """Count how many times this task has been retried (failed → ready transitions)."""
    conn = sqlite3.connect(orchestration_db)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA foreign_keys=ON')
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM task_lineage"
            " WHERE task_id = ? AND old_state = 'failed' AND new_state = 'ready'",
            (task_id,),
        ).fetchone()
        return row['cnt']
    finally:
        conn.close()

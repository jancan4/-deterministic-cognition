"""
Orchestration integration for the runtime supervisor.

Bridges the runtime layer to the task orchestration layer. All reads and
writes to the orchestration database go through these functions so the runner
has a single, testable seam.
"""
import sqlite3
from typing import List

from orchestration.models import Task
from orchestration.service import (
    NotFoundError,
    ValidationError,
    list_tasks,
    transition_task,
)


def poll_ready_tasks(orchestration_db: str) -> List[Task]:
    """Return all tasks in the 'ready' state, ordered by priority then id."""
    return list_tasks(orchestration_db, state='ready')


def execute_task(orchestration_db: str, task_id: int, actor: str) -> Task:
    """
    Advance a task from ready → running → completed.

    In a production runtime each task type would dispatch to a real handler.
    Here the transition pair is the stub: it records execution lineage without
    doing any domain-specific work.
    """
    transition_task(orchestration_db, task_id, 'running',
                    reason='Runtime: task execution started', actor=actor)
    return transition_task(orchestration_db, task_id, 'completed',
                           reason='Runtime: task execution completed', actor=actor)


def count_task_retries(orchestration_db: str, task_id: int) -> int:
    """Count how many times this task has been retried (failed → ready transitions)."""
    conn = sqlite3.connect(orchestration_db)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM task_lineage"
            " WHERE task_id = ? AND old_state = 'failed' AND new_state = 'ready'",
            (task_id,),
        ).fetchone()
        return row['cnt']
    finally:
        conn.close()

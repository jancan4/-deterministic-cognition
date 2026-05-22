import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .models import (
    PRIORITY_MAX, PRIORITY_MIN,
    VALID_DEPENDENCY_TYPES, VALID_STATES, VALID_TASK_TYPES,
    Task, TaskDependency, TaskLineageEvent,
)
from .transitions import TransitionError, validate_transition

_SCHEMA = Path(__file__).parent / 'schema.sql'


class ValidationError(ValueError):
    pass


class NotFoundError(KeyError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA foreign_keys=ON')
    return conn


def _dep_snapshot(conn: sqlite3.Connection, task_id: int) -> List[int]:
    """Sorted list of dependency IDs for task_id at the moment of a transition."""
    rows = conn.execute(
        'SELECT depends_on_id FROM task_dependencies WHERE task_id = ?'
        ' ORDER BY depends_on_id ASC',
        (task_id,),
    ).fetchall()
    return [r['depends_on_id'] for r in rows]


def _validate_priority(priority: int) -> None:
    if isinstance(priority, bool) or not isinstance(priority, int):
        raise ValidationError(f"priority must be an integer, got {type(priority).__name__}")
    if not PRIORITY_MIN <= priority <= PRIORITY_MAX:
        raise ValidationError(f"priority must be {PRIORITY_MIN}–{PRIORITY_MAX}, got {priority}")


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def init_db(db_path: str) -> None:
    with _connect(db_path) as conn:
        conn.executescript(_SCHEMA.read_text())


# ---------------------------------------------------------------------------
# Task creation
# ---------------------------------------------------------------------------

def create_task(
    db_path: str,
    title: str,
    task_type: str,
    actor: str,
    description: Optional[str] = None,
    priority: int = 3,
    tags: Optional[List[str]] = None,
    metadata: Optional[Dict[str, Any]] = None,
    reason: str = 'Task created',
) -> Task:
    if not title or not title.strip():
        raise ValidationError("'title' must not be empty")
    if task_type not in VALID_TASK_TYPES:
        raise ValidationError(f"Invalid task_type '{task_type}'. Valid: {VALID_TASK_TYPES}")
    if not actor or not actor.strip():
        raise ValidationError("'actor' must not be empty")
    _validate_priority(priority)
    if not reason or not reason.strip():
        raise ValidationError("'reason' must not be empty")

    now = _now()
    tags_json = json.dumps(sorted(tags or []))
    metadata_json = json.dumps(metadata or {}, sort_keys=True)

    with _connect(db_path) as conn:
        cur = conn.execute(
            'INSERT INTO tasks'
            ' (title, description, task_type, state, priority, actor,'
            '  tags_json, metadata_json, created_at, updated_at, version)'
            ' VALUES (?,?,?,?,?,?,?,?,?,?,1)',
            (title, description, task_type, 'pending', priority, actor,
             tags_json, metadata_json, now, now),
        )
        task_id = cur.lastrowid
        conn.execute(
            'INSERT INTO task_lineage'
            ' (task_id, old_state, new_state, reason, actor,'
            '  dependency_snapshot, metadata_json, created_at)'
            ' VALUES (?,?,?,?,?,?,?,?)',
            (task_id, None, 'pending', reason, actor, '[]', '{}', now),
        )
        row = conn.execute('SELECT * FROM tasks WHERE id = ?', (task_id,)).fetchone()
        return Task.from_row(row)


# ---------------------------------------------------------------------------
# Query
# ---------------------------------------------------------------------------

def get_task(
    db_path: str,
    task_id: int,
) -> Tuple[Task, List[TaskLineageEvent], List[TaskDependency]]:
    with _connect(db_path) as conn:
        row = conn.execute('SELECT * FROM tasks WHERE id = ?', (task_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"Task {task_id} not found")
        task = Task.from_row(row)
        lineage = [
            TaskLineageEvent.from_row(r)
            for r in conn.execute(
                'SELECT * FROM task_lineage WHERE task_id = ? ORDER BY id ASC',
                (task_id,),
            ).fetchall()
        ]
        deps = [
            TaskDependency.from_row(r)
            for r in conn.execute(
                'SELECT * FROM task_dependencies WHERE task_id = ? ORDER BY id ASC',
                (task_id,),
            ).fetchall()
        ]
        return task, lineage, deps


def list_tasks(
    db_path: str,
    state: Optional[str] = None,
    task_type: Optional[str] = None,
    actor: Optional[str] = None,
    limit: int = 50,
) -> List[Task]:
    if state and state not in VALID_STATES:
        raise ValidationError(f"Invalid state '{state}'")
    if task_type and task_type not in VALID_TASK_TYPES:
        raise ValidationError(f"Invalid task_type '{task_type}'")

    clauses: List[str] = []
    params: list = []
    if state:
        clauses.append('state = ?')
        params.append(state)
    if task_type:
        clauses.append('task_type = ?')
        params.append(task_type)
    if actor:
        clauses.append('actor = ?')
        params.append(actor)

    where = f'WHERE {" AND ".join(clauses)}' if clauses else ''
    params.append(limit)

    with _connect(db_path) as conn:
        rows = conn.execute(
            f'SELECT * FROM tasks {where} ORDER BY priority ASC, id ASC LIMIT ?',
            params,
        ).fetchall()
        return [Task.from_row(r) for r in rows]


# ---------------------------------------------------------------------------
# State transitions
# ---------------------------------------------------------------------------

def transition_task(
    db_path: str,
    task_id: int,
    new_state: str,
    reason: str,
    actor: str,
    metadata: Optional[Dict[str, Any]] = None,
) -> Task:
    if not reason or not reason.strip():
        raise ValidationError("'reason' must not be empty")
    if not actor or not actor.strip():
        raise ValidationError("'actor' must not be empty")

    now = _now()
    with _connect(db_path) as conn:
        row = conn.execute('SELECT * FROM tasks WHERE id = ?', (task_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"Task {task_id} not found")

        old_state = row['state']
        old_version = row['version']

        # Raises TransitionError on invalid transition — propagates to caller.
        validate_transition(old_state, new_state)

        new_version = old_version + 1
        dep_snapshot = _dep_snapshot(conn, task_id)
        meta_json = json.dumps(metadata or {}, sort_keys=True)

        conn.execute(
            'UPDATE tasks SET state = ?, version = ?, updated_at = ? WHERE id = ?',
            (new_state, new_version, now, task_id),
        )
        conn.execute(
            'INSERT INTO task_lineage'
            ' (task_id, old_state, new_state, reason, actor,'
            '  dependency_snapshot, metadata_json, created_at)'
            ' VALUES (?,?,?,?,?,?,?,?)',
            (task_id, old_state, new_state, reason, actor,
             json.dumps(dep_snapshot), meta_json, now),
        )
        row = conn.execute('SELECT * FROM tasks WHERE id = ?', (task_id,)).fetchone()
        return Task.from_row(row)


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------

def add_dependency(
    db_path: str,
    task_id: int,
    depends_on_id: int,
    dependency_type: str,
) -> TaskDependency:
    if dependency_type not in VALID_DEPENDENCY_TYPES:
        raise ValidationError(
            f"Invalid dependency_type '{dependency_type}'. Valid: {VALID_DEPENDENCY_TYPES}"
        )
    if task_id == depends_on_id:
        raise ValidationError("A task cannot depend on itself")

    now = _now()
    with _connect(db_path) as conn:
        for mid in (task_id, depends_on_id):
            if conn.execute('SELECT id FROM tasks WHERE id = ?', (mid,)).fetchone() is None:
                raise NotFoundError(f"Task {mid} not found")
        try:
            cur = conn.execute(
                'INSERT INTO task_dependencies'
                ' (task_id, depends_on_id, dependency_type, created_at)'
                ' VALUES (?,?,?,?)',
                (task_id, depends_on_id, dependency_type, now),
            )
        except sqlite3.IntegrityError:
            raise ValidationError(
                f"Dependency ({task_id} → {depends_on_id}) already exists"
            )
        row = conn.execute(
            'SELECT * FROM task_dependencies WHERE id = ?', (cur.lastrowid,)
        ).fetchone()
        return TaskDependency.from_row(row)


def get_lineage(db_path: str, task_id: int) -> List[TaskLineageEvent]:
    """Full lineage for one task, ordered by lineage id ascending (chronological)."""
    with _connect(db_path) as conn:
        if conn.execute('SELECT id FROM tasks WHERE id = ?', (task_id,)).fetchone() is None:
            raise NotFoundError(f"Task {task_id} not found")
        rows = conn.execute(
            'SELECT * FROM task_lineage WHERE task_id = ? ORDER BY id ASC',
            (task_id,),
        ).fetchall()
        return [TaskLineageEvent.from_row(r) for r in rows]


def get_blocking_dependencies(db_path: str, task_id: int) -> List[TaskDependency]:
    """Dependencies whose upstream task is not yet in completed state."""
    with _connect(db_path) as conn:
        if conn.execute('SELECT id FROM tasks WHERE id = ?', (task_id,)).fetchone() is None:
            raise NotFoundError(f"Task {task_id} not found")
        rows = conn.execute(
            """
            SELECT td.*
            FROM task_dependencies td
            JOIN tasks t ON t.id = td.depends_on_id
            WHERE td.task_id = ?
              AND t.state != 'completed'
            ORDER BY td.id ASC
            """,
            (task_id,),
        ).fetchall()
        return [TaskDependency.from_row(r) for r in rows]


def check_and_unblock(
    db_path: str,
    task_id: int,
    actor: str = 'system',
) -> bool:
    """
    If task_id is blocked and all its dependencies are completed,
    transition it to ready. Returns True if unblocked, False if still blocked.

    This is a deterministic, explicitly-triggered check — not a background
    daemon. The caller controls when it runs.
    """
    with _connect(db_path) as conn:
        row = conn.execute('SELECT * FROM tasks WHERE id = ?', (task_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"Task {task_id} not found")
        if row['state'] != 'blocked':
            return False

    if get_blocking_dependencies(db_path, task_id):
        return False

    transition_task(
        db_path, task_id, 'ready',
        reason='All dependencies resolved — task unblocked',
        actor=actor,
    )
    return True


# ---------------------------------------------------------------------------
# Execution history and export
# ---------------------------------------------------------------------------

def get_execution_history(db_path: str) -> List[TaskLineageEvent]:
    """All lineage events across all tasks, ordered by lineage id ascending."""
    with _connect(db_path) as conn:
        rows = conn.execute(
            'SELECT * FROM task_lineage ORDER BY id ASC'
        ).fetchall()
        return [TaskLineageEvent.from_row(r) for r in rows]


def export_lineage(db_path: str) -> dict:
    """Deterministic JSON-serialisable snapshot of all three tables, ordered by id ASC."""
    with _connect(db_path) as conn:
        tasks = [
            Task.from_row(r)
            for r in conn.execute('SELECT * FROM tasks ORDER BY id ASC').fetchall()
        ]
        lineage = [
            TaskLineageEvent.from_row(r)
            for r in conn.execute('SELECT * FROM task_lineage ORDER BY id ASC').fetchall()
        ]
        deps = [
            TaskDependency.from_row(r)
            for r in conn.execute(
                'SELECT * FROM task_dependencies ORDER BY id ASC'
            ).fetchall()
        ]
    return {
        'schema_version': 1,
        'tasks': [t.to_dict() for t in tasks],
        'task_lineage': [e.to_dict() for e in lineage],
        'task_dependencies': [d.to_dict() for d in deps],
    }

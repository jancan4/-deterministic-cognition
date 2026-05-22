"""
SQLite persistence layer for workflow executions.

Schema is append-only for lineage events; workflow_executions is upsertable
mutable state (cache). Snapshots are optimization-only — lineage is canonical.

Schema version 1.
"""
import json
import sqlite3
from typing import List, Optional

from .state import WorkflowExecution, WorkflowExecutionLineageEvent

_SCHEMA_VERSION = 1


def init_db(db_path: str) -> None:
    """Create workflow persistence tables if they do not exist."""
    conn = sqlite3.connect(db_path)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA foreign_keys=ON')
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS workflow_schema_version (
            version INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS workflow_executions (
            execution_id              TEXT PRIMARY KEY,
            workflow_id               TEXT NOT NULL,
            plan_id                   TEXT NOT NULL,
            state                     TEXT NOT NULL,
            active_stage_index        INTEGER NOT NULL,
            completed_node_ids_json   TEXT NOT NULL DEFAULT '[]',
            failed_node_ids_json      TEXT NOT NULL DEFAULT '[]',
            node_attempts_json        TEXT NOT NULL DEFAULT '{}',
            created_at                TEXT NOT NULL,
            updated_at                TEXT NOT NULL,
            version                   INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS workflow_execution_events (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            execution_id   TEXT NOT NULL,
            event_type     TEXT NOT NULL,
            old_state      TEXT,
            new_state      TEXT,
            node_id        TEXT,
            stage_index    INTEGER NOT NULL DEFAULT 0,
            reason         TEXT NOT NULL,
            created_at     TEXT NOT NULL,
            FOREIGN KEY (execution_id) REFERENCES workflow_executions(execution_id)
        );

        CREATE TABLE IF NOT EXISTS workflow_snapshots (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            execution_id   TEXT NOT NULL,
            snapshot_json  TEXT NOT NULL,
            last_event_id  INTEGER NOT NULL,
            created_at     TEXT NOT NULL,
            FOREIGN KEY (execution_id) REFERENCES workflow_executions(execution_id)
        );
    """)
    if not conn.execute('SELECT version FROM workflow_schema_version').fetchone():
        conn.execute('INSERT INTO workflow_schema_version (version) VALUES (?)', (_SCHEMA_VERSION,))
    conn.commit()
    conn.close()


def save_execution(db_path: str, execution: WorkflowExecution) -> None:
    """Upsert the mutable execution state row."""
    conn = sqlite3.connect(db_path)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA foreign_keys=ON')
    try:
        conn.execute(
            """
            INSERT INTO workflow_executions (
                execution_id, workflow_id, plan_id, state, active_stage_index,
                completed_node_ids_json, failed_node_ids_json, node_attempts_json,
                created_at, updated_at, version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(execution_id) DO UPDATE SET
                state                   = excluded.state,
                active_stage_index      = excluded.active_stage_index,
                completed_node_ids_json = excluded.completed_node_ids_json,
                failed_node_ids_json    = excluded.failed_node_ids_json,
                node_attempts_json      = excluded.node_attempts_json,
                updated_at              = excluded.updated_at,
                version                 = excluded.version
            """,
            (
                execution.execution_id,
                execution.workflow_id,
                execution.plan_id,
                execution.state,
                execution.active_stage_index,
                json.dumps(sorted(execution.completed_node_ids)),
                json.dumps(sorted(execution.failed_node_ids)),
                json.dumps(dict(sorted(execution.node_attempts.items())), sort_keys=True),
                execution.created_at,
                execution.updated_at,
                execution.version,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def append_execution_event(
    db_path: str,
    event: WorkflowExecutionLineageEvent,
) -> int:
    """Append one lineage event. Returns the auto-assigned row id."""
    conn = sqlite3.connect(db_path)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA foreign_keys=ON')
    try:
        cur = conn.execute(
            """
            INSERT INTO workflow_execution_events
                (execution_id, event_type, old_state, new_state, node_id,
                 stage_index, reason, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.execution_id,
                event.event_type,
                event.old_state,
                event.new_state,
                event.node_id,
                event.stage_index,
                event.reason,
                event.created_at,
            ),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def append_execution_events(
    db_path: str,
    events: List[WorkflowExecutionLineageEvent],
) -> List[int]:
    """Append multiple lineage events in a single transaction. Returns row ids."""
    if not events:
        return []
    conn = sqlite3.connect(db_path)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA foreign_keys=ON')
    try:
        ids = []
        for event in events:
            cur = conn.execute(
                """
                INSERT INTO workflow_execution_events
                    (execution_id, event_type, old_state, new_state, node_id,
                     stage_index, reason, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.execution_id,
                    event.event_type,
                    event.old_state,
                    event.new_state,
                    event.node_id,
                    event.stage_index,
                    event.reason,
                    event.created_at,
                ),
            )
            ids.append(cur.lastrowid)
        conn.commit()
        return ids
    finally:
        conn.close()


def load_execution(db_path: str, execution_id: str) -> Optional[WorkflowExecution]:
    """Load the mutable execution state row. None if not found."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    try:
        row = conn.execute(
            'SELECT * FROM workflow_executions WHERE execution_id = ?',
            (execution_id,),
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        return None

    return WorkflowExecution(
        execution_id=row['execution_id'],
        workflow_id=row['workflow_id'],
        plan_id=row['plan_id'],
        state=row['state'],
        active_stage_index=row['active_stage_index'],
        completed_node_ids=json.loads(row['completed_node_ids_json']),
        failed_node_ids=json.loads(row['failed_node_ids_json']),
        node_attempts=json.loads(row['node_attempts_json']),
        created_at=row['created_at'],
        updated_at=row['updated_at'],
        version=row['version'],
    )


def load_execution_events(
    db_path: str,
    execution_id: str,
    after_event_id: int = 0,
) -> List[WorkflowExecutionLineageEvent]:
    """
    Load lineage events for an execution in insertion order.
    Pass after_event_id to load only events appended after a snapshot.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    try:
        rows = conn.execute(
            """
            SELECT * FROM workflow_execution_events
            WHERE execution_id = ? AND id > ?
            ORDER BY id ASC
            """,
            (execution_id, after_event_id),
        ).fetchall()
    finally:
        conn.close()

    return [
        WorkflowExecutionLineageEvent(
            execution_id=row['execution_id'],
            event_type=row['event_type'],
            old_state=row['old_state'],
            new_state=row['new_state'],
            node_id=row['node_id'],
            stage_index=row['stage_index'],
            reason=row['reason'],
            created_at=row['created_at'],
        )
        for row in rows
    ]


def persist_snapshot(
    db_path: str,
    execution: WorkflowExecution,
    last_event_id: int,
) -> int:
    """
    Save an execution snapshot tied to last_event_id.
    Returns the snapshot row id.
    """
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    snapshot_json = json.dumps(execution.to_dict(), sort_keys=True)

    conn = sqlite3.connect(db_path)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA foreign_keys=ON')
    try:
        cur = conn.execute(
            """
            INSERT INTO workflow_snapshots
                (execution_id, snapshot_json, last_event_id, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (execution.execution_id, snapshot_json, last_event_id, now),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def load_latest_snapshot(
    db_path: str,
    execution_id: str,
) -> Optional[tuple]:
    """
    Load the most recent snapshot for an execution.
    Returns (WorkflowExecution, last_event_id) or None if no snapshot exists.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    try:
        row = conn.execute(
            """
            SELECT * FROM workflow_snapshots
            WHERE execution_id = ?
            ORDER BY id DESC LIMIT 1
            """,
            (execution_id,),
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        return None

    execution = WorkflowExecution.from_dict(json.loads(row['snapshot_json']))
    return execution, row['last_event_id']

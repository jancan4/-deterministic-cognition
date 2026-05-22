"""
SQLite persistence layer for workflow executions, definitions, and plans.

Schema is append-only for lineage events; workflow_executions is upsertable
mutable state (cache). Snapshots are optimization-only — lineage is canonical.
Workflow definitions and plans are immutable once persisted (collision detection
on divergent content raises rather than silently overwriting).

Schema version history:
  v1 — initial schema
  v2 — metadata_json column on workflow_execution_events
  v3 — workflow_definitions and workflow_plans tables (idempotency + replay)
"""
import json
import sqlite3
from typing import List, Optional

from .models import WorkflowDefinition, WorkflowExecutionPlan
from .state import WorkflowExecution, WorkflowExecutionLineageEvent

_SCHEMA_VERSION = 3


class WorkflowDefinitionError(Exception):
    """Raised when a workflow definition collision is detected on save."""


class WorkflowPlanError(Exception):
    """Raised when a workflow plan collision is detected on save."""


def init_db(db_path: str) -> None:
    """Create workflow persistence tables if they do not exist. Idempotent."""
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
            metadata_json  TEXT NOT NULL DEFAULT '{}',
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

        CREATE TABLE IF NOT EXISTS workflow_definitions (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            workflow_id     TEXT NOT NULL,
            version         INTEGER NOT NULL,
            name            TEXT NOT NULL,
            topology_hash   TEXT NOT NULL,
            definition_json TEXT NOT NULL,
            created_at      TEXT NOT NULL,
            UNIQUE(workflow_id, version)
        );

        CREATE TABLE IF NOT EXISTS workflow_plans (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            plan_id         TEXT NOT NULL UNIQUE,
            workflow_id     TEXT NOT NULL,
            version         INTEGER NOT NULL,
            planner_version TEXT NOT NULL,
            plan_json       TEXT NOT NULL,
            created_at      TEXT NOT NULL,
            FOREIGN KEY (workflow_id, version)
                REFERENCES workflow_definitions(workflow_id, version)
        );

        CREATE INDEX IF NOT EXISTS idx_workflow_plans_planner_version
            ON workflow_plans(planner_version);
    """)
    row = conn.execute('SELECT version FROM workflow_schema_version').fetchone()
    if row is None:
        conn.execute('INSERT INTO workflow_schema_version (version) VALUES (?)', (_SCHEMA_VERSION,))
    elif row[0] < _SCHEMA_VERSION:
        conn.execute('UPDATE workflow_schema_version SET version = ?', (_SCHEMA_VERSION,))
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
                 stage_index, reason, created_at, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                json.dumps(event.metadata, sort_keys=True),
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
                     stage_index, reason, created_at, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    json.dumps(event.metadata, sort_keys=True),
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
            metadata=json.loads(row['metadata_json'] or '{}'),
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


# ---------------------------------------------------------------------------
# workflow_definitions CRUD
# ---------------------------------------------------------------------------

def save_workflow_definition(db_path: str, definition: WorkflowDefinition) -> None:
    """
    Persist a workflow definition. Idempotent on (workflow_id, version).

    Raises WorkflowDefinitionError if the same (workflow_id, version) already
    exists with a divergent topology_hash — this guards against silent content
    corruption and forces explicit version bumping.
    """
    definition_json = json.dumps(definition.to_dict(), sort_keys=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA foreign_keys=ON')
    try:
        existing = conn.execute(
            'SELECT topology_hash FROM workflow_definitions'
            ' WHERE workflow_id = ? AND version = ?',
            (definition.workflow_id, definition.version),
        ).fetchone()
        if existing is not None:
            if existing['topology_hash'] != definition.topology_hash:
                raise WorkflowDefinitionError(
                    f"Topology hash collision for workflow '{definition.workflow_id}' "
                    f"v{definition.version}: stored={existing['topology_hash']!r}, "
                    f"new={definition.topology_hash!r}"
                )
            return  # idempotent — same content already persisted
        conn.execute(
            """
            INSERT INTO workflow_definitions
                (workflow_id, version, name, topology_hash, definition_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                definition.workflow_id,
                definition.version,
                definition.name,
                definition.topology_hash,
                definition_json,
                definition.created_at,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def load_workflow_definition(
    db_path: str,
    workflow_id: str,
    version: int,
) -> Optional[WorkflowDefinition]:
    """Load a persisted workflow definition by (workflow_id, version). None if absent."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    try:
        row = conn.execute(
            'SELECT definition_json FROM workflow_definitions'
            ' WHERE workflow_id = ? AND version = ?',
            (workflow_id, version),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return WorkflowDefinition.from_dict(json.loads(row['definition_json']))


# ---------------------------------------------------------------------------
# workflow_plans CRUD
# ---------------------------------------------------------------------------

def save_workflow_plan(db_path: str, plan: WorkflowExecutionPlan) -> None:
    """
    Persist a workflow execution plan. Idempotent on plan_id.

    Raises WorkflowPlanError if the same plan_id already exists with divergent
    plan_json — guards against silent content corruption.

    The corresponding workflow_definition row must already exist (FK constraint).
    """
    plan_json = json.dumps(plan.to_dict(), sort_keys=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA foreign_keys=ON')
    try:
        existing = conn.execute(
            'SELECT plan_json FROM workflow_plans WHERE plan_id = ?',
            (plan.plan_id,),
        ).fetchone()
        if existing is not None:
            if existing['plan_json'] != plan_json:
                raise WorkflowPlanError(
                    f"Plan content collision for plan_id '{plan.plan_id}': "
                    f"stored and new plan_json differ"
                )
            return  # idempotent
        conn.execute(
            """
            INSERT INTO workflow_plans
                (plan_id, workflow_id, version, planner_version, plan_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                plan.plan_id,
                plan.workflow_id,
                plan.version,
                plan.planner_version,
                plan_json,
                plan.generated_at,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def load_workflow_plan(
    db_path: str,
    plan_id: str,
) -> Optional[WorkflowExecutionPlan]:
    """Load a persisted workflow execution plan by plan_id. None if absent."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    try:
        row = conn.execute(
            'SELECT plan_json FROM workflow_plans WHERE plan_id = ?',
            (plan_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return WorkflowExecutionPlan.from_dict(json.loads(row['plan_json']))


def load_plan_for_execution(
    db_path: str,
    execution_id: str,
) -> Optional[WorkflowExecutionPlan]:
    """
    Load the workflow execution plan for a given execution.

    Two-step: load plan_id from workflow_executions, then load plan from
    workflow_plans. Returns None if either the execution or the plan row is
    absent (graceful degradation for pre-v3 executions).
    """
    execution = load_execution(db_path, execution_id)
    if execution is None:
        return None
    return load_workflow_plan(db_path, execution.plan_id)


def load_definition_for_execution(
    db_path: str,
    execution_id: str,
) -> Optional[WorkflowDefinition]:
    """
    Load the workflow definition for a given execution.

    Three-step join: execution → plan (for version) → definition.
    Returns None at any missing link — graceful degradation for pre-v3 executions.
    """
    execution = load_execution(db_path, execution_id)
    if execution is None:
        return None
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    try:
        plan_row = conn.execute(
            'SELECT workflow_id, version FROM workflow_plans WHERE plan_id = ?',
            (execution.plan_id,),
        ).fetchone()
        if plan_row is None:
            return None
        def_row = conn.execute(
            'SELECT definition_json FROM workflow_definitions'
            ' WHERE workflow_id = ? AND version = ?',
            (plan_row['workflow_id'], plan_row['version']),
        ).fetchone()
    finally:
        conn.close()
    if def_row is None:
        return None
    return WorkflowDefinition.from_dict(json.loads(def_row['definition_json']))

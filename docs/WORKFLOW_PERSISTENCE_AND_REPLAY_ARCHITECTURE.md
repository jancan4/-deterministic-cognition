# Workflow Persistence and Replay Architecture

## Philosophy

Lineage is canonical. The mutable state row is a cache.

Every state change produces an immutable `WorkflowExecutionLineageEvent` before any side effect occurs. The SQLite `workflow_execution_events` table is append-only. The `workflow_executions` table is an upsertable cache of the latest mutable state — it can be reconstructed entirely from the event log.

If the mutable row and the lineage diverge (crash, bug, or manual intervention), the lineage wins. Replay from events is always authoritative.

---

## What This Layer Is

- SQLite-backed append-only lineage storage
- Deterministic event-log replay that reconstructs `WorkflowExecution` from scratch
- Snapshot-accelerated delta replay (snapshot + events after snapshot)
- Validation of lineage event sequences before replay
- High-level persistence API that combines storage and replay

## What This Layer Is Not

- A source of truth for workflow structure (that is the `WorkflowExecutionPlan`)
- A distributed log or message queue
- A background replay daemon
- A conflict resolver between concurrent writers

---

## Components

### `workflow/storage.py`

Raw SQLite CRUD. No business logic. All callers get deterministic results for the same input.

| Function | Purpose |
|---|---|
| `init_db(db_path)` | Create tables if absent; set schema version. Idempotent. |
| `save_execution(db_path, execution)` | Upsert the mutable execution state row |
| `append_execution_event(db_path, event)` | Append one lineage event; return row id |
| `append_execution_events(db_path, events)` | Append multiple events atomically; return row ids |
| `load_execution(db_path, execution_id)` | Load mutable state row; None if absent |
| `load_execution_events(db_path, execution_id, after_event_id=0)` | Load events in insertion order, optionally from a checkpoint |
| `persist_snapshot(db_path, execution, last_event_id)` | Write a snapshot checkpoint |
| `load_latest_snapshot(db_path, execution_id)` | Load most recent snapshot; None if absent |

### `workflow/replay.py`

Pure replay functions. No I/O — all inputs are in-memory objects.

| Function | Purpose |
|---|---|
| `validate_lineage(events)` | Check event sequence for correctness; return error list |
| `replay_execution(events)` | Full replay from event list; return `ReplayResult` |
| `replay_from_snapshot(snapshot, events_after_snapshot)` | Delta replay on top of snapshot |

#### `ReplayResult`

```python
@dataclass
class ReplayResult:
    execution: Optional[WorkflowExecution]
    events_applied: int
    validation_errors: List[str]
    is_valid: bool
```

`is_valid` is `True` only when `validation_errors` is empty. `execution` is `None` only when the event list is empty.

### `workflow/persistence.py`

High-level API combining storage + replay. Callers import from here.

| Function | Purpose |
|---|---|
| `persist_execution(db_path, execution, events)` | Upsert state row + append events |
| `append_execution_events(db_path, events)` | Append events only; return row ids |
| `replay_execution_from_storage(db_path, execution_id)` | Full replay from all stored events |
| `replay_execution_from_snapshot(db_path, execution_id)` | Snapshot-accelerated replay; falls back to full replay if no snapshot |
| `take_snapshot(db_path, execution, last_event_id)` | Persist a snapshot checkpoint |

---

## Schema

```sql
-- Schema version guard
CREATE TABLE IF NOT EXISTS workflow_schema_version (
    version INTEGER NOT NULL
);

-- Mutable state cache (upsertable)
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

-- Append-only lineage (canonical truth)
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

-- Snapshot checkpoints (optimization only — not canonical)
CREATE TABLE IF NOT EXISTS workflow_snapshots (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    execution_id   TEXT NOT NULL,
    snapshot_json  TEXT NOT NULL,
    last_event_id  INTEGER NOT NULL,
    created_at     TEXT NOT NULL,
    FOREIGN KEY (execution_id) REFERENCES workflow_executions(execution_id)
);
```

**Schema version 1.** The `workflow_schema_version` row is written once on first `init_db`. Future migrations increment this value.

---

## Lineage Validation Rules

`validate_lineage` enforces six invariants:

| Rule | Check |
|---|---|
| 1 | First event must be `state_transition` with `new_state = 'initialized'` |
| 2 | All events must share the same `execution_id` |
| 3 | State transitions must follow `VALID_WORKFLOW_EXECUTION_TRANSITIONS` |
| 4 | No duplicate `node_completed` events for the same node |
| 5 | `node_failed` cannot appear for a node that already had `node_completed` |
| 6 | No events may appear after a terminal state (`completed` or `cancelled`) |

Validation returns a list of error strings (empty = valid). It does not raise exceptions — callers inspect the list.

---

## Replay Event Application

`replay_execution` applies each event to accumulate execution state:

| Event type | State mutation |
|---|---|
| `state_transition` | Sets `state = new_state`; bumps `version` |
| `node_completed` | Appends `node_id` to `completed_node_ids` (sorted); bumps `version` |
| `node_failed` | Appends `node_id` to `failed_node_ids` (sorted); increments `node_attempts[node_id]`; bumps `version` |
| `node_retry` | Increments `node_attempts[node_id]`; bumps `version` |
| `stage_advanced` | Sets `active_stage_index = event.stage_index + 1`; bumps `version` |
| `node_submitted` | No state mutation (informational only) |

**Stage semantics:** `event.stage_index` in a `stage_advanced` event records the OLD (completed) stage index. The new `active_stage_index` is `event.stage_index + 1`.

**Version:** increments once per state-mutating event. `node_submitted` does not bump the version.

**workflow_id / plan_id:** these fields are not embedded in lineage events. `replay_execution_from_storage` restores them from the mutable state row after replay.

---

## Snapshot Pattern

Snapshots are optimization checkpoints — they are never canonical.

```
persist_snapshot(db, execution, last_event_id)
    └── Stores: snapshot_json (execution.to_dict()), last_event_id, created_at

replay_execution_from_snapshot(db, execution_id)
    └── load_latest_snapshot → (snapshot, last_event_id)
    └── load_execution_events(after_event_id=last_event_id) → delta
    └── replay_from_snapshot(snapshot, delta)
```

If no snapshot exists, falls back to `replay_execution_from_storage` (full replay).

`replay_from_snapshot` skips lineage validation — the snapshot is already trusted. Only the delta events are applied.

---

## Caller Pattern

```python
from workflow.executor import initialize_execution, start_execution, record_node_completed
from workflow.persistence import persist_execution, replay_execution_from_storage

db = 'workflow.db'
init_db(db)

# Initialize and persist
execution, init_event = initialize_execution(plan)
persist_execution(db, execution, [init_event])

# Start and persist
execution, start_events = start_execution(execution)
persist_execution(db, execution, start_events)

# Complete a node and persist
execution, complete_events = record_node_completed(execution, plan, 'fetch')
persist_execution(db, execution, complete_events)

# Recover from lineage (authoritative)
result = replay_execution_from_storage(db, execution.execution_id)
assert result.is_valid
recovered = result.execution
```

---

## Why workflow_id and plan_id Are Not in Events

These fields identify the workflow and plan, not the execution's progress. They never change after initialization. Embedding them in every event would be redundant and would not contribute to replay correctness. The mutable state row stores them once; `replay_execution_from_storage` reads the row to patch them into the replayed execution.

---

## Future Extension Paths

**Async event appending:**  
`append_execution_events` currently runs synchronously in-process. A future `EventAppender` can buffer events and flush them in a background thread, reducing commit latency for high-frequency executions.

**Migration support:**  
`workflow_schema_version` enables schema migrations. A future `migrate_db(db_path)` function can inspect the version row and apply incremental DDL changes.

**Cross-execution lineage:**  
A future `predecessor_execution_id` column on `workflow_executions` can link a new execution (after governed replanning) to the prior execution whose lineage it continues from.

**Full audit inspector:**  
The `workflow_execution_events` table is a complete audit log. A future `ExecutionInspector` can replay to any historical event index — reconstructing exactly what state existed at each point in time.

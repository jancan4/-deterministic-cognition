# Process Entrypoint, Recovery, and Inspection Architecture

## Philosophy

The mutable state row is a cache. The lineage is canonical truth.

On every process start, non-terminal executions are candidates for recovery inspection — their lineage may be ahead of the stored row due to a crash, a bug, or manual intervention. The recovery loop replays each execution from lineage and compares it against the mutable row. If they diverge, the lineage wins.

Nothing in this layer runs autonomously or in the background. The caller drives every step. Recovery and inspection are read-only by default; mutation requires an explicit opt-in.

---

## What This Layer Is

- Process startup recovery: find non-terminal executions, replay, compare, report
- Execution inspection: replay to any historical event index, surface divergence
- Automatic snapshot policy: deterministic triggers for snapshot checkpoints
- CLI entrypoint: operator-facing commands for status, recovery, inspection, and snapshots

## What This Layer Is Not

- An autonomous agent or background daemon
- A distributed coordinator or message queue
- A conflict resolver between concurrent writers
- A replanner or adaptive execution engine

---

## Components

### `workflow/recovery.py`

Startup recovery loop for workflow executions.

| Function | Purpose |
|---|---|
| `find_non_terminal_execution_ids(db_path)` | Return execution_ids whose mutable row is not in a terminal state |
| `recover_execution(db_path, execution_id)` | Dry-run: replay from lineage, compare against stored row, return RecoveryReport |
| `apply_recovery(db_path, execution_id)` | Apply: replay from lineage and write reconstructed state back to mutable row |
| `recover_all(db_path, apply=False)` | Scan all non-terminal executions; dry-run or apply recovery |

#### `RecoveryReport`

```python
@dataclass
class RecoveryReport:
    execution_id: str
    stored_state: Optional[str]       # state in the mutable row (None if absent)
    replayed_state: Optional[str]     # state reconstructed from lineage
    diverged: bool                    # True if stored != replayed on any field
    divergence_details: List[str]     # human-readable per-field divergence descriptions
    is_recoverable: bool              # True if lineage is valid and replay succeeded
    events_applied: int               # number of lineage events replayed
    lineage_valid: bool               # True if validate_lineage found no errors
```

**Dry-run by default.** `recover_execution` never mutates state. `apply_recovery` writes back only when `is_recoverable=True`. The distinction makes it safe to run recovery reporting in read-only contexts (e.g., monitoring, audit).

**Terminal executions are excluded.** `find_non_terminal_execution_ids` filters out `completed` and `cancelled`. Their state is immutable; recovery is a no-op.

---

### `workflow/inspector.py`

Read-only execution lineage inspector. Replays a workflow execution's event log — optionally to a specific event index — and surfaces the reconstructed state alongside any divergence from the mutable state row.

| Function | Purpose |
|---|---|
| `inspect_execution(db_path, execution_id, up_to_event_index=None)` | Full or partial replay; return InspectionResult |

#### `InspectionResult`

```python
@dataclass
class InspectionResult:
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
```

**Divergence comparison is only performed on full replay.** Partial replays are intentionally historical snapshots — comparing them against the current mutable row would produce misleading divergence reports.

**`up_to_event_index` is a 0-based count.** `inspect_execution(db, eid, up_to_event_index=3)` replays `events[:3]` — the first 3 events. `up_to_event_index=0` replays nothing (state = 'unknown').

---

### `workflow/snapshot_policy.py`

Deterministic snapshot policy for workflow execution persistence.

Snapshots are cache-only optimizations — lineage is always canonical truth. A snapshot allows delta-replay to skip re-applying the full event log.

| Function | Purpose |
|---|---|
| `should_snapshot(policy, events_since, stage_just_advanced)` | Pure function — returns True if policy mandates a snapshot |
| `apply_snapshot_policy(db_path, policy, execution, new_event_ids, new_events, last_snapshot_event_id)` | Evaluate policy after appending events; take snapshot if triggered; return row_id or None |

#### `SnapshotPolicy`

```python
@dataclass
class SnapshotPolicy:
    events_per_snapshot: int = 50           # 0 = disabled
    snapshot_on_stage_advance: bool = True  # False = disabled
```

Two independent trigger conditions:
1. **Stage boundary**: snapshot whenever `active_stage_index` advances (a `stage_advanced` event appears in the batch).
2. **Event count**: snapshot every N events appended since the last snapshot. The count accumulates across multiple persist calls — `_count_events_since(db, execution_id, after_event_id)` queries the database for the total, not just the current batch.

Either condition independently fires a snapshot. Both can be active simultaneously.

**Callers** should update their `last_snapshot_event_id` to `max(new_event_ids)` when a snapshot is taken, so the next call's event count starts from the correct checkpoint.

---

### `cli/main.py`

Operator-facing CLI entrypoint.

```
workflow-cli --db PATH <command> [options]
```

| Command | Description |
|---|---|
| `status` | List all non-terminal executions with state and event count |
| `recover [--apply]` | Dry-run or apply lineage recovery for non-terminal executions |
| `inspect --execution-id ID [--at-event N]` | Replay and display one execution, optionally to event N |
| `snapshot --execution-id ID` | Take a manual snapshot of one execution |
| `run-once [--orch-db PATH]` | Submit one round of ready nodes to the orchestration layer |

**`--db`** defaults to `workflow.db` in the current directory.

**`recover`** prints per-execution divergence details. Without `--apply` it is read-only.

**`inspect --at-event N`** replays only the first N events (0-based count), enabling reconstruction of any historical state.

**`snapshot`** replays the full lineage and writes a snapshot checkpoint. Useful after manual state repair or for forcing a checkpoint before a long idle period.

**`run-once`** is a single-step coordination call. It does not loop, does not block, and does not require background threads. The caller drives repetition.

---

## Caller Patterns

### Startup Recovery

```python
from workflow.recovery import recover_all

reports = recover_all(db_path, apply=False)
for report in reports:
    if report.diverged:
        log.warning("Divergence in %s: %s", report.execution_id, report.divergence_details)

# After review:
reports = recover_all(db_path, apply=True)
```

### Point-in-Time Inspection

```python
from workflow.inspector import inspect_execution

# Full replay
result = inspect_execution(db, execution_id)

# State after first 3 events
result = inspect_execution(db, execution_id, up_to_event_index=3)
```

### Automatic Snapshots in the Persist Loop

```python
from workflow.snapshot_policy import SnapshotPolicy, apply_snapshot_policy

policy = SnapshotPolicy(events_per_snapshot=50, snapshot_on_stage_advance=True)
last_snapshot_event_id = 0

# After each persist_execution call:
event_ids = append_execution_events(db, new_events)
row_id = apply_snapshot_policy(
    db, policy, execution, event_ids, new_events,
    last_snapshot_event_id=last_snapshot_event_id,
)
if row_id is not None:
    last_snapshot_event_id = max(event_ids)
```

### CLI Usage

```bash
# Check status of all non-terminal executions
python -m cli.main --db workflow.db status

# Dry-run recovery (read-only)
python -m cli.main --db workflow.db recover

# Apply recovery (write)
python -m cli.main --db workflow.db recover --apply

# Inspect an execution at full lineage
python -m cli.main --db workflow.db inspect --execution-id <ID>

# Inspect state after 3 events
python -m cli.main --db workflow.db inspect --execution-id <ID> --at-event 3

# Take a manual snapshot
python -m cli.main --db workflow.db snapshot --execution-id <ID>
```

---

## Invariants

1. **Lineage wins on divergence.** Recovery never invents or mutates state beyond what the lineage records. The mutable row is written from lineage, not the other way around.

2. **Partial replay never compares against the mutable row.** `inspect_execution` with `up_to_event_index` is a historical snapshot — divergence comparison against the current row is intentionally suppressed.

3. **Terminal executions are never recovered.** `find_non_terminal_execution_ids` excludes `completed` and `cancelled`. Recovery is a no-op for terminal state.

4. **Snapshots are never canonical.** `apply_snapshot_policy` writes cache-only checkpoints. Deleting the `workflow_snapshots` table has no effect on lineage correctness; it only forces full replay.

5. **All operations are deterministic.** Given the same event log, `recover_execution` and `inspect_execution` always produce the same result. No randomness, no timestamps in logic paths.

---

## Future Extension Paths

**Startup hook integration:**  
`recover_all(db, apply=True)` can be called from a process-startup hook before accepting work. This ensures the mutable row is always consistent with lineage before the execution loop begins.

**Recovery policy configuration:**  
A `RecoveryPolicy` dataclass can gate whether `apply` is automatic (safe for clearly-valid lineages) or requires operator confirmation (for executions with validation errors).

**Historical replay API:**  
`inspect_execution` already supports point-in-time replay. A future HTTP endpoint can expose this as a debugging interface for operators and integration tests.

**Snapshot compaction:**  
`workflow_snapshots` accumulates one row per snapshot trigger. A future `compact_snapshots(db, execution_id)` can prune all but the most recent snapshot for each execution to bound table growth.

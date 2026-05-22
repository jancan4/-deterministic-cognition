# Runtime Supervisor Architecture

**Status:** Implemented — Milestone 4  
**Date:** 2026-05-21  
**Branch:** feature/runtime-supervisor-layer  
**Governance:** AI may propose. Quant validation must validate. Risk engine has final veto.

---

## Purpose

The runtime supervisor is a bounded, deterministic execution loop that polls the task
orchestration layer for ready work, executes tasks in priority order, and records every
state transition in an immutable lineage. It does not run indefinitely — every run is
bounded by `max_iterations` or an external `should_stop` signal.

The runtime is not an autonomous agent. It is a supervised loop with human-readable audit
trails, pause-and-resume support, and deterministic replay.

---

## Why a Supervised Loop, Not an Autonomous Agent

Autonomous agents that run indefinitely and self-direct their next action introduce
governance problems:

- There is no clear point at which a human can inspect state without interrupting work.
- If the process crashes, the last known-good state may be unclear.
- Audit trails become entangled with execution history in ways that are hard to separate.

The supervised runtime solves these problems:

| Property | Autonomous Agent | Supervised Runtime |
|---|---|---|
| Iteration bound | Infinite | `max_iterations` — explicit ceiling |
| Pause semantics | Kill process | `paused` state — explicit and resumable |
| Interrupt semantics | Crash | `interrupted` state → checkpoint → recovery path |
| Audit trail | Logs | Immutable `runtime_lineage` rows |
| State inspection | Attach debugger | Read `runtimes` table |
| Recovery | Restart from scratch | Restore from latest `runtime_checkpoints` row |
| Execution semantics | Opaque | Deterministic transition sequence |

The runtime can be paused between runs and resumed later with `resume_runtime`. Recovery
from interruption follows a governed path: `interrupted → recovering → idle`, with a
checkpoint saved at interruption time.

---

## State Machine

```
initialized ──► idle ──► polling ──► executing ──► checkpointing ──┐
     │           │           │              │              │         │
     │           │           └──► idle ◄───┘              └──► idle─┘
     │           │
     │           └──► paused ──► idle (resume)
     │                 │
     │                 └──► stopped  (terminal)
     │
     └──► interrupted ──► recovering ──► idle
               │                │
               │                └──► failed ──► recovering
               │
               └──► stopped
```

### States

| State | Meaning |
|---|---|
| `initialized` | Runtime registered; not yet started |
| `idle` | Between iterations; ready to poll |
| `polling` | Querying orchestration layer for ready tasks |
| `executing` | Executing one or more ready tasks |
| `checkpointing` | Persisting a checkpoint record |
| `paused` | Controlled pause; resumable via `resume_runtime` |
| `interrupted` | Uncontrolled stop (KeyboardInterrupt); requires recovery |
| `recovering` | Transitioning from interrupted/failed back to idle |
| `failed` | Recovery failed; requires manual intervention |
| `stopped` | Terminal — no further transitions permitted |

### Why `stopped` is the Only Terminal State

`paused` is a planned stop. A paused runtime can resume. It is not terminal.

`interrupted` is an unplanned stop that requires acknowledgment (recovery). It is not
terminal because forcing it to be terminal would prevent the clean
`interrupted → recovering → idle` path.

`stopped` is the explicit, intentional, no-return state. It must be triggered deliberately.
No code path reaches `stopped` without an explicit call to `stop_runtime` or
`transition_runtime(..., 'stopped', ...)`.

---

## Iteration Loop

Each call to `run_iterations` executes the following sequence:

```
while iteration < max_iterations and not should_stop():
    iteration += 1
    transition → polling
    tasks = poll_ready_tasks(orchestration_db)
    if tasks:
        transition → executing
        for each task: execute_task(...)
        if iteration % checkpoint_every == 0:
            transition → checkpointing
            save_checkpoint(...)
            transition → idle
        else:
            transition → idle
    else:
        transition → idle

transition → paused  (max_iterations reached or should_stop fired)
```

Every transition is written to `runtime_lineage` before the corresponding side-effect.
This means if the process crashes between a transition write and the side-effect, the
lineage shows the last known state and recovery can proceed from there.

---

## Checkpoint Design

Checkpoints record `{iteration, tasks_executed}` at each `checkpoint_every` boundary.
They are stored in `runtime_checkpoints` with a foreign key to `runtimes`.

Checkpoints are not a full event-sourced replay — they are progress markers. The
distinction matters:

- **Event-sourced replay**: re-execute all events from the beginning to reconstruct state.
  This would require re-executing all tasks, which is not idempotent.
- **Checkpoint**: resume from the last known-good iteration count, skipping re-execution
  of already-completed tasks.

The orchestration layer's task state (task in `completed` state) provides the ground
truth for what was completed. The checkpoint's `tasks_executed` count is advisory —
it records how many tasks the runtime processed, not which ones. Recovery uses the
checkpoint to restore `iteration` and `tasks_executed` counters, then re-polls the
orchestration layer for remaining ready tasks.

---

## Recovery Path

When a `KeyboardInterrupt` is caught inside `run_iterations`:

1. Runtime transitions to `interrupted`.
2. A checkpoint is saved with current `{iteration, tasks_executed}`.
3. The `KeyboardInterrupt` re-raises (propagates to the caller).

To recover:

```python
recover_runtime(state_db, runtime_id, reason='recovering after interrupt')
# → interrupted → recovering → idle
```

Then resume:

```python
result = resume_runtime(state_db, runtime_id, orchestration_db, config)
```

`resume_runtime` calls `run_iterations`, which detects `paused` (or `idle` after
recovery) and continues the loop. The orchestration layer is the source of truth for
remaining work.

---

## Orchestration Integration (`service.py`)

The service layer is the only interface between the runtime and the orchestration DB.

| Function | Description |
|---|---|
| `poll_ready_tasks(orchestration_db)` | Returns all tasks in `ready` state, ordered by priority then id |
| `execute_task(orchestration_db, task_id, actor)` | Transitions task: `ready → running → completed` |
| `count_task_retries(orchestration_db, task_id)` | Counts `failed → ready` transitions in task lineage |

In the current stub implementation, `execute_task` performs two immediate transitions.
A production implementation would dispatch to a task-type-specific handler between the
`running` and `completed` transitions. The interface is stable — the handler dispatch
is an internal change that does not affect the service contract.

---

## Deterministic Guarantees

- All timestamps are UTC ISO-8601 (`YYYY-MM-DDTHH:MM:SSZ`).
- `runtime_lineage` rows are ordered by `id` ascending (insertion order = chronological).
- Checkpoint `state_json` is serialized with `sort_keys=True`.
- No UUIDs, no hash-based ordering, no randomness anywhere in the write path.
- `init_db` is idempotent (`CREATE TABLE IF NOT EXISTS`).
- `PRAGMA foreign_keys=ON` enforces referential integrity at the SQLite level.
- `PRAGMA journal_mode=WAL` allows concurrent readers without blocking the writer.

---

## Governance Constraints

These constraints are permanent. They survive every future extension.

1. The runtime has no autonomous authority. It executes ready tasks as declared in the
   orchestration layer. It does not create tasks or modify task types.
2. `max_iterations` must always be set in production. A `None` value is permitted for
   one-shot scripts where `should_stop` is the primary stop signal.
3. Runtime state transitions are immutable in `runtime_lineage`. No delete or update
   is defined on lineage rows.
4. The runtime never bypasses orchestration validation. Task transitions go through
   `orchestration.service.transition_task`, which enforces the state machine.
5. No broker integration. No live capital. No trading signals.

---

## Schema

### `runtimes`

| Column | Type | Notes |
|---|---|---|
| id | INTEGER | PRIMARY KEY AUTOINCREMENT |
| name | TEXT | NOT NULL, non-empty |
| state | TEXT | NOT NULL, current runtime state |
| orchestration_db | TEXT | NOT NULL, path to orchestration SQLite DB |
| config_json | TEXT | JSON-serialized RuntimeConfig |
| current_iteration | INTEGER | Updated at each transition |
| created_at | TEXT | ISO-8601 UTC |
| updated_at | TEXT | ISO-8601 UTC |
| version | INTEGER | Incremented on each transition |

### `runtime_lineage`

| Column | Type | Notes |
|---|---|---|
| id | INTEGER | PRIMARY KEY AUTOINCREMENT |
| runtime_id | INTEGER | FK → runtimes(id) |
| old_state | TEXT | Nullable (first transition has None) |
| new_state | TEXT | NOT NULL |
| reason | TEXT | NOT NULL |
| iteration | INTEGER | Iteration at time of transition |
| metadata_json | TEXT | Arbitrary JSON context |
| created_at | TEXT | ISO-8601 UTC |

### `runtime_checkpoints`

| Column | Type | Notes |
|---|---|---|
| id | INTEGER | PRIMARY KEY AUTOINCREMENT |
| runtime_id | INTEGER | FK → runtimes(id) |
| iteration | INTEGER | Iteration at checkpoint time |
| state_json | TEXT | `{"iteration": N, "tasks_executed": M}` |
| reason | TEXT | NOT NULL |
| created_at | TEXT | ISO-8601 UTC |

---

## Relationship to Other Layers

```
memory layer        → stores decisions, hypotheses, governance rules
orchestration layer → defines tasks, dependencies, state machines
runtime layer       → executes ready tasks, records runtime lineage
```

The runtime layer reads from the orchestration layer (poll for ready tasks) and writes
back to it (transition tasks). It does not read from or write to the memory layer. The
memory layer stores institutional knowledge; the runtime layer executes governed tasks.
These are orthogonal concerns.

---

## Future Extension Points

**Handler dispatch**  
`execute_task` currently performs a stub two-step transition. A future version would
dispatch to a task-type registry (`research → ResearchHandler`, `validation → ValidationHandler`)
between the `running` and `completed` transitions. The service contract is stable.

**Retry with backoff**  
`count_task_retries` is implemented but unused in the current runner. A future runner
could use it to skip tasks that have exceeded `config.max_retries` and transition them
to `failed` instead of executing them.

**Checkpoint restoration**  
`restore_from_checkpoint` is implemented in `checkpoints.py`. A future `recover_from_checkpoint`
runner path would read the latest checkpoint, restore `{iteration, tasks_executed}`, and
resume the loop. Currently, recovery re-polls from iteration 0, which is correct because
the orchestration layer's task state is the ground truth.

**Distributed runtimes**  
Multiple runtime instances could operate against the same orchestration DB if the
`execute_task` stub is made atomic (claim + transition in a single SQLite transaction).
The `runtime_lineage` table already isolates lineage by `runtime_id`.

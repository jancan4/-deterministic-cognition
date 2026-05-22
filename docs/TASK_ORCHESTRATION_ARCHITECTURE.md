# Task Orchestration Architecture

**Status:** Implemented — Milestone 4  
**Date:** 2026-05-21  
**Governance:** AI may propose. Quant validation must validate. Risk engine has final veto.

---

## Orchestration Philosophy

Tasks are replayable state machines. Canonical truth is execution lineage, not mutable state.

The conventional approach to task management is a mutable record: a row in a database with a `status` column that gets `UPDATE`d as the task progresses. This approach is fast and simple. It is also opaque. The current status of a task tells you where the task is now. It tells you nothing about how it got there: which transitions occurred, when, who triggered them, and why.

The orchestration layer takes the opposite position. Every state transition is an immutable lineage event. The current state of a task is a consequence of its lineage — it can always be derived from the lineage log. The `tasks.state` column is a projection of the lineage for query convenience, not the source of truth.

This is not a performance optimisation. It is a governance property. An auditor can reconstruct the complete history of any task from its lineage events, verify that every transition was valid, and identify exactly when and why a task failed, was retried, or was cancelled.

---

## Replayable Workflows

A workflow is replayable if, given its event log, you can reconstruct every state it passed through.

The `task_lineage` table makes this possible. Every row records:
- `old_state` — the state before the transition
- `new_state` — the state after the transition
- `reason` — a required human-readable justification
- `actor` — who or what triggered the transition
- `dependency_snapshot` — the IDs of all dependencies at the moment of the transition
- `metadata_json` — arbitrary structured context (run IDs, parameters, results)
- `created_at` — UTC ISO-8601 timestamp

Given the lineage for a task, `replay_state_history(lineage)` returns the complete state sequence as a list of `(old_state, new_state)` tuples. `current_state_from_lineage(lineage)` derives the current state without touching the `tasks` table.

Replayability is not contingent on the `tasks` table existing. If the `tasks` projection were lost, the lineage log contains sufficient information to reconstruct current state for every task.

---

## Lineage vs Mutable State

| Property | Mutable state (UPDATE) | Lineage (immutable events) |
|---|---|---|
| Current state | Direct read | Derived from last lineage event |
| State history | Lost on each update | Complete and ordered |
| Who changed it | Unknown | Recorded per event |
| Why it changed | Unknown | Required `reason` field |
| Dependency context at change | Lost | Captured in `dependency_snapshot` |
| Auditable | No | Yes |
| Replayable | No | Yes |
| Diffable across time | No | Yes — diff lineage exports |

The `tasks.state` column exists as a materialised projection of the lineage, indexed for efficient queries. It is kept in sync by the service layer on every `transition_task` call. It is never updated directly.

---

## Deterministic State Machines

The task state machine is defined by a closed adjacency map:

```
pending   → { ready, blocked, cancelled }
ready     → { running, blocked, cancelled }
running   → { completed, failed, blocked }
blocked   → { ready, cancelled }
failed    → { ready, cancelled }
completed → { superseded }
cancelled → ∅  (terminal)
superseded → ∅  (terminal)
```

Every transition is validated against this map before any database write. Invalid transitions raise `TransitionError` and leave the database unchanged. There are no silent state mutations.

**State semantics:**

| State | Meaning |
|---|---|
| `pending` | Created, not yet evaluated for readiness |
| `ready` | All dependencies satisfied; eligible for execution |
| `running` | Actively executing |
| `blocked` | Has unresolved dependencies |
| `completed` | Execution finished successfully |
| `failed` | Execution terminated with an error |
| `cancelled` | Explicitly terminated before completion |
| `superseded` | Replaced by a newer task; preserved for audit |

Terminal states (`completed`, `cancelled`, `superseded`) have no outgoing transitions. A task in a terminal state cannot be modified. It can be linked to, referenced by, or exported, but its state is frozen.

---

## Governance-Aware Orchestration

The orchestration layer enforces governance at three points:

**1. Transition validation.**  
No state transition happens without an explicit `reason` (non-empty string) and `actor` (non-empty string). The reason is recorded immutably in `task_lineage`. An auditor can always answer: who moved this task, and why?

**2. Dependency transparency.**  
`get_blocking_dependencies(task_id)` returns the precise list of upstream tasks that are preventing a blocked task from becoming ready. Dependency context is also captured in `dependency_snapshot` at every transition, so the dependency graph at any historical point can be reconstructed.

**3. No autonomous execution.**  
`check_and_unblock` is explicitly called by a controller — it is not a background daemon. No task transitions happen without a caller deciding to trigger the check. The orchestration layer is a passive state machine, not an executor. Autonomous execution is deferred by design (see below).

---

## Why Autonomous Execution Is Deferred

The brief explicitly excludes: async workers, brokers, queues, autonomous execution loops, multiprocessing, cron systems, autonomous retries, and background daemons.

The reasons are principled, not merely pragmatic:

**Auditability breaks under autonomous execution.** If a background daemon retries a failed task after 5 minutes, the retry appears in the lineage with `actor='system'`. But no human approved the retry. The lineage is technically complete but lacks human oversight. In a governed research pipeline, this is unacceptable for tasks that affect calibration, regime classification, or validation outcomes.

**Determinism conflicts with timing-based triggers.** A cron-triggered retry depends on wall-clock time. Given the same input state, running the orchestration layer at 2:00 AM vs 2:05 AM produces different results. The system is no longer deterministic.

**Failure modes are obscured.** A daemon that catches exceptions and retries silently accumulates failures without surfacing them. The governance model requires that failures are visible, explicitly acknowledged, and explicitly retried by a named actor.

**The governance model requires human approval for consequential transitions.** Autonomous execution assumes that `failed → ready → running` is safe to perform automatically. For research pipeline tasks involving calibration changes or regime decisions, this assumption is not valid without quant validation.

Autonomous execution remains a future extension. When implemented, it must: log the triggering condition, record a non-empty reason and named actor in every lineage event, require explicit whitelist of task types eligible for autonomous retry, and surface failures to a human review queue rather than silently retrying indefinitely.

---

## Dependency Tracking

Tasks may declare dependencies on other tasks via `task_dependencies`. The dependency type describes the nature of the requirement:

| Type | Meaning |
|---|---|
| `task_completion` | Upstream task must reach `completed` state |
| `governance_approval` | A governance decision task must complete |
| `validation_outcome` | A validation task must complete |

`get_blocking_dependencies(task_id)` returns all dependencies whose upstream task is not yet `completed`. A task can only be unblocked when all its blocking dependencies are resolved.

The `dependency_snapshot` field in each lineage event captures the IDs of all current dependencies at the moment of a transition. This ensures that even if dependencies are later modified, the historical record reflects the dependency state at the time of the transition.

---

## Replay Reconstruction

To reconstruct the full history of any task:

```python
task, lineage, deps = service.get_task(db_path, task_id)
history = replay_state_history(lineage)
# → [(None, 'pending'), ('pending', 'ready'), ('ready', 'running'), ...]

current = current_state_from_lineage(lineage)
# → 'running'
```

To reconstruct the full execution history across all tasks:

```python
all_events = service.get_execution_history(db_path)
# All lineage events, all tasks, ordered by id ascending (insertion order)
```

To produce a deterministic exportable snapshot:

```python
payload = service.export_lineage(db_path)
# {'schema_version': 1, 'tasks': [...], 'task_lineage': [...], 'task_dependencies': [...]}
```

All three tables are exported ordered by `id ASC`. The export is byte-identical across machines for the same database state.

---

## Relationship to the Memory Layer

The orchestration layer is distinct from the memory layer. The memory layer stores institutional knowledge: decisions, hypotheses, governance rules, validation results. The orchestration layer tracks the execution of work items: research tasks, calibration runs, validation workflows, governance reviews.

They can be linked:
- A `governance` task in the orchestration layer may depend on a `governance_rule` memory event reaching `accepted` status
- A `validation` task may produce a `validation_result` memory event upon completion
- A `calibration` task's `dependency_snapshot` may reference memory IDs alongside task IDs

This linkage is currently implicit (via metadata fields and manual cross-referencing). A future integration layer could formalise the relationship.

---

## Future Extension Points

**Autonomous retry whitelist**  
A future configuration layer could define which task types are eligible for autonomous retry after failure, with a maximum retry count and backoff schedule. The state machine already supports `failed → ready`. The governance constraint is: only pre-approved task types, with a non-empty system-generated reason recording the retry attempt number and triggering condition.

**Cron-scheduled execution**  
Periodic tasks (weekly data pulls, monthly calibration reviews) could be created automatically by a scheduler. The governance constraint is: the scheduler must be deterministic (cron expression, not wall-clock polling), must record the schedule expression in the lineage event metadata, and must surface task failures to a human review queue.

**Task DAG visualisation**  
`task_dependencies` defines a directed acyclic graph. A future `dag_export(db_path)` function could produce a topologically sorted representation suitable for rendering.

**Memory-task integration**  
A future `link_task_to_memory(task_id, memory_id, relationship)` function could create explicit cross-table references, enabling queries like "which tasks are blocked waiting for governance approval of memory event 42?"

**Distributed lineage merge**  
The deterministic export format is designed to support future cross-instance merging. Given two lineage exports from different environments, a merge algorithm could reconcile task state using lineage event timestamps and source instance identifiers, similar to the import/merge extension planned for the memory layer.

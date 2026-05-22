# Workflow Runtime Coordination Architecture

## Philosophy

A workflow execution is a deterministic realization of a validated execution plan. Every state change — node completion, stage advancement, failure, retry — is recorded as an immutable lineage event before any side effect occurs. The execution can be reconstructed entirely from its lineage.

Canonical truth is the lineage, not mutable runtime state. If a crash occurs between a state transition and the submission of the next orchestration task, recovery reads the lineage to determine what has already happened and resumes from there.

---

## What This Layer Is

- Deterministic execution state tracking (WorkflowExecution)
- Stage-aware progress tracking with automatic advancement
- Governed node submission to the orchestration layer
- Retry-aware failure handling with lineage-tracked attempts
- Idempotent submission (active-task guard prevents double-submission)
- Replayable execution lineage events for every state change

## What This Layer Is Not

- An autonomous replanner
- A distributed scheduler
- An adaptive workflow engine
- A background daemon
- A concurrency manager

---

## Components

### `WorkflowExecution` (in `workflow/state.py`)

The live state of a workflow plan's realization.

| Field | Purpose |
|---|---|
| `execution_id` | SHA-256 of `(plan_id, created_at)` — unique per planning event |
| `workflow_id` | Reference to the WorkflowDefinition |
| `plan_id` | Reference to the WorkflowExecutionPlan being realized |
| `state` | Current execution state (see state machine below) |
| `active_stage_index` | Which stage is currently being executed |
| `completed_node_ids` | Sorted list of successfully completed nodes |
| `failed_node_ids` | Sorted list of nodes with exhausted retries |
| `node_attempts` | Dict: node_id → number of attempts made |
| `version` | Monotonically incrementing; bumps on every state change |

### `WorkflowExecutionLineageEvent` (in `workflow/state.py`)

One event per state change. Event types:

| Type | When produced |
|---|---|
| `state_transition` | Every workflow state change (initialized, ready, executing, …) |
| `node_completed` | A node finishes successfully |
| `node_failed` | A node's retries are exhausted |
| `node_retry` | A node fails but retries remain |
| `node_submitted` | A node is submitted to the orchestration layer |
| `stage_advanced` | active_stage_index increments |

### `WorkflowStageExecution` (in `workflow/state.py`)

A computed, read-only snapshot of one stage's progress. Derived from `WorkflowExecution + WorkflowExecutionPlan` — not independently stored.

---

## Execution State Machine

```
initialized ──→ ready ──→ executing ──→ completed  (all nodes done)
                  │            │
                  │            ├──→ failed    (manual or escalation)
                  │            ├──→ blocked   (no progress possible)
                  │            └──→ paused    (external signal)
                  │
                  └──→ cancelled
```

Detailed transitions:

| From | To | Condition |
|---|---|---|
| initialized | ready | start_execution called |
| ready | executing | execution begins |
| executing | completed | all nodes in completed_node_ids |
| executing | blocked | failed nodes block all remaining |
| executing | paused | external pause signal |
| executing | failed | direct escalation |
| blocked | executing | retry resolves blockage |
| blocked | failed | operator decision |
| paused | executing | resume |
| failed | cancelled | administrative cleanup |

**`completed` and `cancelled` are terminal.** `failed` allows only `cancelled` as a next state.

---

## Pure-Function Executor (`workflow/executor.py`)

All state transitions are pure functions: same input always produces the same output. No database, no network.

### `initialize_execution(plan)`
Creates a `WorkflowExecution` in `initialized` state. `execution_id` is SHA-256 of `(plan_id, created_at)`.

### `start_execution(execution)`
Transitions `initialized → ready → executing`. Two lineage events.

### `get_ready_node_ids(execution, plan)`
Nodes whose dependencies are all in `completed_node_ids`, not in `completed_node_ids` themselves, and not in `failed_node_ids`. Traverses stages in order — result is deterministically sequenced.

Nodes with retries remaining (not in `failed_node_ids`) appear here; the coordination layer guards against re-submission of already-active tasks.

### `record_node_completed(execution, plan, node_id, reason)`
1. Adds to `completed_node_ids`
2. Emits `node_completed` event
3. Advances `active_stage_index` if the current stage fully completes
4. If all nodes complete → transitions to `completed`
5. If remaining nodes are all blocked → transitions to `blocked`

### `record_node_failed(execution, plan, definition, node_id, reason)`
**Retry available** (`attempts_made < max_attempts`):
- Increments `node_attempts[node_id]`
- Emits `node_retry` event
- Node stays logically ready (not in `failed_node_ids`)

**Retries exhausted** (`attempts_made >= max_attempts`):
- Adds to `failed_node_ids`
- Emits `node_failed` event
- If no nodes can make progress → transitions to `blocked`

### `detect_outcome(execution, plan)`
Read-only scan. Returns:
- `'completed'` — all nodes in `completed_node_ids`
- `'executing'` — at least one node can still run
- `'blocked'` — no nodes can run; all remaining depend on failed nodes
- `'failed'` / `'cancelled'` — already in terminal state

---

## Coordination Layer (`workflow/coordination.py`)

Bridges the pure executor to the orchestration database.

### Submission flow

```
step_execution
    └── submit_ready_nodes
            └── get_ready_node_ids(execution, plan)
            └── find_submitted_node_ids(orchestration_db, execution_id)
            └── for each unsubmitted ready node:
                    create_task(orchestration_db, ...)     [pending]
                    transition_task(orchestration_db, ready)
                    emit node_submitted lineage event
```

Task metadata always includes:
```json
{
  "workflow_execution_id": "<execution_id>",
  "workflow_node_id": "<node_id>"
}
```

This links every orchestration task back to its workflow node for traceability.

### Active-task guard (idempotency)

`find_submitted_node_ids` queries the orchestration database for tasks in `pending`, `ready`, or `running` state whose metadata contains the current `execution_id`. Nodes with active tasks are skipped. This makes `submit_ready_nodes` safe to call multiple times without double-submission.

When a task completes or fails, its state transitions out of the active set — the node becomes re-submittable for retry.

### Retry re-submission

A node with retries remaining is NOT in `failed_node_ids`. After its orchestration task completes or fails and the task transitions out of `running`, `find_submitted_node_ids` will no longer include the node. The next call to `step_execution` will see it as ready and re-submit it.

### Caller loop pattern

```python
execution, evt = initialize_execution(plan)
execution, evts = start_execution(execution)

while execution.state == 'executing':
    execution, tasks, evts = step_execution(orch_db, execution, plan, definition, actor)
    
    for task in tasks:
        # ... runner executes tasks ...
        node_id = extract_node_id(task)
        success = ...  # from handler result
        execution, evts = handle_task_result(
            execution, plan, definition, node_id, success, reason
        )
```

The caller drives the loop. No background threads, no autonomous polling.

---

## Stage Progression Rules

A stage advances only when **all nodes in that stage are in `completed_node_ids`**. A stage with any failure does not advance — the workflow either continues independent branches or transitions to `blocked`.

No speculative stage advancement. No partial-success advancement.

---

## Failure Propagation

When a node fails with exhausted retries:
1. Node is added to `failed_node_ids`
2. `detect_outcome` computes which downstream nodes are permanently blocked (BFS over reverse dependency graph)
3. If no nodes can make progress → execution transitions to `blocked`
4. If independent branches remain → execution stays `executing`

The `blocked` state signals that operator intervention is required before the execution can progress or be marked `failed`.

---

## Why Autonomous Replanning Is Deferred

Adaptive planning — graph mutation based on intermediate results — requires:

1. A formal specification of which mutations are permitted
2. A governance gate before each mutation (quant validation, human approval)
3. A replay mechanism for mutated graphs that preserves pre-mutation lineage

None of these exist yet. Autonomous replanning without governance gates would violate the system's core constraint: every structural change must be validated before execution.

The current coordination layer is the prerequisite for governed adaptive planning. It proves the lineage model works before adding mutation.

---

## Orchestration Task Types

Workflow nodes must declare a `task_type` from `orchestration.models.VALID_TASK_TYPES`:

```
research, analysis, validation, calibration, review, governance, implementation, report
```

The coordination layer submits tasks with the node's `task_type`. If a node uses a type outside this set, `create_task` will raise `ValidationError` at submission time.

All workflow-submitted tasks use orchestration priority `3` (middle). Within-stage execution ordering is determined by the planner (priority, then node_id lexicographic), not the orchestration queue.

---

## Future Extension Paths

**Persistent execution store:**  
Currently `WorkflowExecution` is an in-memory object. A future `WorkflowExecutionStore` layer can persist executions and their lineage to SQLite (or another store), enabling recovery after process restart.

**Governed adaptive replanning:**  
A `ProposedGraphMutation` type can carry a modified `WorkflowDefinition` through the human review queue. Once approved, a new execution is initialized from the updated plan. The prior execution's lineage is preserved; the new execution references the old `execution_id` as its predecessor.

**Execution observability:**  
The `WorkflowExecutionLineageEvent` stream is a complete audit log. A future `execution_inspector` can replay events to reconstruct any historical execution state — including exactly what was submitted, when, and with what result.

**Concurrency hints:**  
Stage groupings already identify parallelizable work. A future parallel coordinator can submit all stage nodes simultaneously to a task pool and track completion asynchronously, without changing the executor's pure-function model.

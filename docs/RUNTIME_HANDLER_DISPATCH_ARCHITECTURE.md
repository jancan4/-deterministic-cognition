# Runtime Handler Dispatch Architecture

## Overview

The handler dispatch layer sits between the runtime supervisor and the task orchestration layer. It provides a deterministic, testable seam: the runner polls ready tasks, hands each one to a registered callable, and records the outcome as an immutable task lineage event — all without network calls, random state, or mutable global registries.

---

## Components

### `TaskHandlerRegistry`

An explicit dictionary-backed registry mapping `task_type` strings to handler callables.

**Why explicit rather than global?**  
A module-level global registry accumulates state across tests and makes the dispatch path invisible at the call site. An explicit registry passed as an argument is inspectable, replaceable per call, and carries no hidden coupling between modules.

**Why `list_handlers()` returns sorted names?**  
Sorted output is deterministic regardless of registration order. Audit logs and diagnostic output comparing two registries must not diverge because of Python dict insertion order.

**Registration invariants:**
- `task_type` must be a non-empty, non-whitespace string
- Handler must be callable
- Duplicate registration raises `HandlerRegistrationError` unless `replace=True` is explicit

---

### `HandlerResult`

A structured, immutable outcome dataclass. Never raises. Every code path through `execute_handler` returns a `HandlerResult`.

| Field | Type | Notes |
|---|---|---|
| `task_id` | `int` | Identifies which task produced this result |
| `task_type` | `str` | The registered type that was dispatched |
| `success` | `bool` | `True` only if handler returned without raising |
| `result_json` | `str` | JSON-serialized handler return value; `'{}'` on failure |
| `error` | `Optional[str]` | `None` on success; plain-text message on failure |
| `metadata_json` | `str` | JSON-serialized caller-supplied metadata dict |

Both `result_json` and `metadata_json` use `sort_keys=True` for deterministic serialization across Python versions.

---

### `execute_handler`

Pure dispatch function. No database writes. Never raises.

**Three outcome paths:**

1. **Missing handler** — Registry does not contain `task.task_type`. Returns `HandlerResult(success=False, error='missing_handler:{task_type}')`.
2. **Handler exception** — Handler raises any exception. Returns `HandlerResult(success=False, error=str(exc) or repr(exc), result_json='{}')`.
3. **Handler success** — Handler returns any value. Returns `HandlerResult(success=True, result_json=json.dumps(value or {}, sort_keys=True))`.

**Why not propagate handler exceptions?**  
The runner executes potentially many tasks per iteration. A single bad handler must not abort the loop or corrupt runtime state. The caller (`execute_task`) decides what task transition to make based on the structured outcome.

---

### `execute_task` (in `runtime/service.py`)

Dispatch + task state machine integration.

```
ready → running → completed   (handler success)
ready → running → failed      (handler failure, exception, or missing handler)
```

The `ready → running` transition is always persisted before dispatch. If the process crashes between that transition and handler execution, the task is in `running` and the lineage is queryable. Recovery tooling can detect stuck `running` tasks.

**`registry=None` behavior:**  
When no registry is supplied, every task transitions `running → failed` with reason `missing_handler:{task_type}`. This is deterministic and explicit — callers that pass `registry=None` are not silently skipping execution, they are producing auditable failure records.

---

## Data Flow

```
run_iterations
    └── poll_ready_tasks()          reads orchestration DB
    └── for task in ready_tasks:
            execute_task(task.id, registry=registry)
                └── transition_task(ready → running)   [lineage written]
                └── execute_handler(registry, task)     [no DB writes]
                └── transition_task(running → completed | failed)  [lineage written]
```

---

## Replayability and Lineage Guarantees

Every dispatch outcome writes exactly one `task_lineage` row:

- `running → completed` with reason `Handler succeeded: {task_type}`
- `running → failed` with reason `Handler failed: {error}` or `missing_handler:{task_type}`

No handler can transition a task to an arbitrary state by calling `transition_task` directly, because all transitions are validated against `VALID_TASK_TRANSITIONS`. An invalid handler-initiated transition raises `TransitionError`, which `execute_handler` catches and converts to a `HandlerResult(success=False)`, which causes `execute_task` to write `running → failed`. The final lineage is deterministic.

---

## Handler Contract

A handler is any callable with signature `(task: Task) -> Optional[dict]`.

- Return value is JSON-serialized as `result_json`. Return `None` or `{}` for no output.
- Raise any exception to signal failure. The exception message is captured verbatim.
- Do not assume the caller will retry. Retry logic belongs in the orchestration layer.
- Do not make network calls in tests. Handlers must be synchronous and local.

---

## Registry is Not Global

```python
# Wrong — shared mutable state, test pollution
_GLOBAL_REGISTRY = TaskHandlerRegistry()

# Correct — explicit, scoped, testable
registry = TaskHandlerRegistry()
registry.register('research', my_handler)
run_iterations(state_db, runtime_id, orch_db, config, registry=registry)
```

Pass `registry=None` to explicitly acknowledge that task execution will produce deterministic `missing_handler` failures (useful in stub or dry-run scenarios).

---

## Future Extension

The `metadata` parameter on `execute_handler` accepts a `Dict[str, Any]` that is serialized into `HandlerResult.metadata_json`. This field is available for handler-level tracing, correlation IDs, or per-dispatch configuration without changing the handler signature.

Handler return values (`result_json`) are currently not persisted to the task record, but are available in `HandlerResult` for callers that need to act on handler output (e.g., scheduling downstream tasks based on research results).

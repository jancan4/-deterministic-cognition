# Workflow Definition Architecture

## Philosophy

A workflow is a deterministic directed acyclic graph of orchestration tasks. Its canonical form is the definition — not runtime mutable state. Every planning decision is reproducible from the same definition.

The central question of this layer is not "what is running?" but "what would run, and in what order, given this definition?" That question has a single correct answer for any valid workflow. The planner computes it. The lineage records it.

---

## What This Layer Is

- Deterministic DAG definitions with explicit nodes and edges
- Structural validation before any planning occurs
- Topological execution plans with replayable stage ordering
- Immutable lineage events for every planning attempt
- Lightweight integration points for the runtime supervisor

## What This Layer Is Not

- A distributed scheduler
- An autonomous planning AI
- A self-modifying workflow engine
- BPM tooling
- Cloud orchestration infrastructure

Dynamic replanning, autonomous adaptation, and runtime graph mutation are deliberately deferred. The system must be fully auditable before it can be adaptive.

---

## Components

### `WorkflowNode`

The unit of work in a workflow graph.

| Field | Purpose |
|---|---|
| `node_id` | Unique identifier within the workflow — referenced by other nodes' `dependency_ids` |
| `task_type` | Maps to a registered `TaskHandlerRegistry` handler type |
| `task_payload_json` | JSON-serialized input for the task; must be valid JSON at definition time |
| `dependency_ids` | Explicit list of node_ids this node depends on; no implicit dependencies |
| `priority` | Integer; lower value = higher priority within a stage (0 = highest) |
| `tags` | Grouping labels for filtering and audit |
| `retry_policy` | `max_attempts` (>= 1) and `backoff_seconds` (>= 0) — validated at definition time |
| `governance_requirements` | Labels such as `human_approval`, `quant_validation` — for future gate enforcement |

### `WorkflowEdge`

Derived automatically from `dependency_ids`. An edge `A → B` means "B depends on A" — A must complete before B can execute. Edges are stored explicitly in `WorkflowDefinition` for audit purposes; they carry no additional information beyond what is already encoded in `dependency_ids`.

### `WorkflowDefinition`

The complete, immutable description of a workflow at a point in time.

- `topology_hash` — SHA-256 of the structural graph (node_ids, task_types, dependencies). Payload and metadata do not affect this hash. Two definitions are structurally identical if and only if their topology hashes match.
- `version` — Monotonically increasing integer; caller-controlled.
- `edges` — Derived from `dependency_ids`; stored for audit completeness.

Definitions are not stored in a database by this layer. They are pure Python objects that callers may serialize and persist as needed.

### `ValidationResult`

Output of `validate_workflow`. Contains `valid: bool` and a sorted list of error strings. All errors are collected before returning — no short-circuit on first failure. Sorted output ensures test assertions are independent of internal validator execution order.

### `WorkflowExecutionPlan`

The planner's output for a valid workflow.

| Field | Purpose |
|---|---|
| `plan_id` | SHA-256 of `(workflow_id, version, planner_version)` — deterministic; same workflow produces the same plan_id |
| `stages` | Ordered list of `ExecutionStage`; nodes in each stage may execute concurrently |
| `dependency_snapshot` | Frozen record of each node's `dependency_ids` at plan time |
| `generated_at` | UTC ISO-8601 timestamp of plan generation |
| `planner_version` | Semver string — changes when the planning algorithm changes |

### `ExecutionStage`

A set of nodes that may execute concurrently (all dependencies satisfied at the same topological depth). Node ordering within a stage is deterministic: `(priority asc, node_id lex)`.

### `WorkflowLineageEvent`

Produced for every `plan_workflow` call, valid or not. Records:

- `workflow_id`, `version`, `planner_version`
- `validation_result` — `'valid'` or a semicolon-joined error summary
- `topology_hash` — identifies the exact graph topology that was planned
- `generated_at` — when the lineage event was produced

Every planning attempt has an immutable audit record, regardless of outcome.

---

## Deterministic Topology Hash

```python
topology = {
    'nodes': [
        {'node_id': n.node_id, 'task_type': n.task_type, 'dependency_ids': sorted(n.dependency_ids)}
        for n in sorted(nodes, key=lambda n: n.node_id)
    ],
    'edges': [e.to_dict() for e in sorted(edges, ...)],
}
hash = sha256(json.dumps(topology, sort_keys=True))
```

Properties guaranteed:
- Identical topology always produces the same hash
- Insertion order of nodes or edges never affects the hash
- Payload changes (not structural) do not change the hash
- Adding or removing a dependency always changes the hash

The topology hash is the canonical identity of a workflow's graph structure, independent of name, version, metadata, or timestamps.

---

## Planner Algorithm

Kahn's topological sort with sorted queues:

1. Compute in-degree for every node.
2. Collect all zero-in-degree nodes as stage 0 (sorted by `(priority, node_id)`).
3. Reduce in-degrees of each stage's successors.
4. Repeat until no zero-in-degree nodes remain.

The sorted queue invariant means the traversal is identical across runs regardless of Python dict ordering. No heuristics. No randomization.

**Why Kahn's instead of DFS?**  
Kahn's naturally produces stage groupings (nodes at the same topological depth form a stage). DFS requires an additional pass to recover depth information and its traversal order is sensitive to the starting node.

---

## Validation Order and Error Collection

Validators run in dependency order so downstream errors are suppressed when their prerequisite check fails:

1. Duplicate node_ids — structural integrity
2. Empty node_id / task_type — field-level integrity
3. Missing dependency references — reference integrity
4. Invalid retry policies — field-level integrity
5. Invalid `task_payload_json` — field-level integrity
6. Circular dependencies (skipped if missing-dep errors present — a missing dep causes false cycle reports)
7. Disconnected graph (skipped for single-node workflows)

All errors from all passing validators are collected and returned sorted. The caller sees every problem in one pass.

---

## Data Flow

```
define_workflow(nodes)
    └── _derive_edges(nodes)              pure function
    └── compute_topology_hash(nodes, edges)   SHA-256
    └── WorkflowDefinition(...)           immutable

plan_workflow(definition)
    └── validate_workflow(definition)     ValidationResult
    └── build_workflow_lineage(...)       WorkflowLineageEvent  ← always produced
    └── if valid:
            build_execution_plan(...)     WorkflowExecutionPlan
    └── return (ValidationResult, Optional[Plan], LineageEvent)
```

---

## Runtime Integration

The workflow layer has **no direct dependency on the runtime or orchestration layers**. Integration points are additive:

- The runtime supervisor may consume a `WorkflowExecutionPlan` to determine which tasks to enqueue next (using `get_ready_nodes` with a `completed_node_ids` set).
- Individual orchestration tasks may carry a `workflow_node_id` reference in their payload for traceability.
- The planner's `dependency_snapshot` can be diff'd against the current orchestration state to detect drift.

None of these integrations are implemented automatically. The runtime does not auto-consume plans. The workflow layer does not write to the orchestration database.

---

## Why Dynamic Autonomous Planning Is Deferred

Adaptive planning — where the workflow graph mutates based on intermediate results — requires:

1. A formal specification of what mutations are permitted
2. A governance gate before each mutation
3. A replay mechanism for mutated graphs

None of these are yet in place. Implementing adaptive planning now would produce a system that can change its own instructions without a verifiable audit trail. That violates the governance constraint: *AI may propose, quant must validate, risk engine has final veto, human approval required for regime changes.*

The current layer is the prerequisite for adaptive planning, not a substitute for it.

---

## Future Extension Paths

**Versioned workflow evolution:**  
Bump `version` on any structural change. The `topology_hash` detects silent drift — two definitions with the same `workflow_id` but different hashes are provably different graphs.

**Governed adaptive planning:**  
A future `AdaptivePlanner` can emit `ProposedTopologyChange` events, route them through the human review queue, and apply approved mutations to produce a new `WorkflowDefinition` (new version, new hash, new lineage event). The current static planner remains the fallback.

**Orchestration bridge:**  
A `WorkflowExecutor` can consume `get_ready_nodes`, enqueue orchestration tasks with `workflow_node_id` in their payloads, and advance `completed_node_ids` as tasks finish. This bridge is one layer above the current implementation.

**Parallelism hints:**  
Stage groupings already expose which nodes can execute concurrently. A future scheduler can use stage membership to assign work to parallel workers without any additional planning changes.

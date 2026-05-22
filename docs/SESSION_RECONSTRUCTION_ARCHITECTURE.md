# Session Reconstruction Architecture

## Philosophy

A session is a deterministic reconstruction of relevant cognition state.

Canonical truth remains the lineage and persisted memory events. The session layer assembles temporary active cognition context from those sources. Nothing is injected, invented, or autonomously inferred. Given the same database state and activation policy, the same session is always produced.

---

## Active vs Persisted Cognition

| Layer | Nature | Mutability |
|---|---|---|
| `memory_events` (SQLite) | Persisted lineage | Append-only with versioned revisions |
| `workflow_execution_events` (SQLite) | Workflow lineage | Append-only |
| `runtime_lineage_events` (SQLite) | Runtime lineage | Append-only |
| `SessionContext` | Ephemeral assembled context | Discarded after session |

The session layer reads from persisted layers and produces a structured, budget-constrained view. It does not write back to any store. Reconstruction is read-only.

---

## What This Layer Is

- Deterministic context activation from persisted memory, workflow lineage, and runtime state
- Replay-aware session reconstruction: `SessionContext.to_dict()` captures the full assembled state for audit and replay
- Governance-aware activation: governance items always rank first and survive truncation
- Operator-visible cognitive reconstruction: every activated item includes its source, type, confidence, and timestamps
- Inspectable context budgeting: truncation is deterministic and reported in the output

## What This Layer Is Not

- An autonomous cognition agent
- A hidden memory injector
- An opaque summarizer
- An embedding or vector search engine
- A semantic hallucination engine
- A background daemon

---

## Components

### `session/models.py`

Core data models. No I/O.

| Type | Purpose |
|---|---|
| `ContextActivationPolicy` | Governs what gets activated, how deeply, and within what budget |
| `ActivatedMemory` | A ranked memory event selected for a session |
| `ActiveWorkflow` | A non-terminal workflow execution surfaced in context |
| `RuntimeSnapshot` | A point-in-time view of a runtime process |
| `SessionContext` | The assembled, budgeted context for one session |
| `SessionReconstruction` | Full reconstruction: structured context + rendered text |

#### `ContextActivationPolicy`

```python
@dataclass
class ContextActivationPolicy:
    tags: List[str] = []          # memory tag filter
    min_confidence: int = 1       # minimum confidence threshold
    include_unresolved: bool = True
    include_governance: bool = True
    include_adaptations: bool = True
    expand_related: bool = True
    include_active_workflows: bool = True
    workflow_db_path: Optional[str] = None
    include_runtime_state: bool = True
    runtime_db_path: Optional[str] = None
    max_memory_candidates: int = 50
    max_chars: int = 12000
    max_entries: int = 60
```

All fields have safe defaults. Callers override only what they need.

---

### `session/activation.py`

Deterministic activation and ranking of memory events. Pure functions where possible.

| Function | Purpose |
|---|---|
| `score_and_rank(scored_events, pin_governance, pin_unresolved)` | Convert ScoredEvents to ranked ActivatedMemory items |
| `activate_memory(memory_db_path, policy)` | Retrieve + rank memory per policy |
| `partition_by_section(activated)` | Partition into named sections for reconstruction |

#### Activation Rank (Composite Sort Key)

```
(tier, doctrine_rank, -confidence, recency_rank, -tag_overlap, memory_id)
```

| Tier | Condition | Description |
|---|---|---|
| 0 | `event_type in {governance_rule, architecture_decision}` | Governance — pinned first |
| 1 | `status in {unresolved, proposed}` | Unresolved — pinned second |
| 2 | `not is_expanded` | Primary retrieved events |
| 3 | `is_expanded` | Related-expanded events |

Within each tier, items sort by doctrine priority → confidence (descending) → recency → tag overlap (descending) → id.

**Doctrine priority** (from `memory.retrieval.DOCTRINE_PRIORITY`):

| Type | Priority |
|---|---|
| `governance_rule` | 1 (highest) |
| `architecture_decision` | 2 |
| `validation_result` | 3 |
| `adaptation` | 4 |
| `hypothesis` | 5 |
| `implementation_note` | 6 |
| other | 7 |

#### Section Partition

| Section | Event types / statuses |
|---|---|
| `governance_context` | `governance_rule`, `architecture_decision` |
| `unresolved_items` | `status in {unresolved, proposed}` |
| `active_investigations` | `open_question`, `hypothesis` |
| `relevant_memory` | everything else |

Items may appear in multiple sections (e.g. a `governance_rule` with `status='unresolved'` appears in both `governance_context` and `unresolved_items`).

#### Retrieval Passes

`activate_memory` makes four retrieval passes in order, deduplicating by `memory_id`:

1. `retrieve_governance` — if `include_governance=True`
2. `retrieve_unresolved` — if `include_unresolved=True`
3. Adaptation-specific retrieve — if `include_adaptations=True`
4. General retrieve — always runs to capture all event types not covered above

---

### `session/context_window.py`

Deterministic context budgeting. Pure function — no I/O.

| Function | Purpose |
|---|---|
| `apply_context_budget(policy, ...)` | Accept items by tier until budget exhausted |

#### Preservation Tiers (highest to lowest)

| Tier | Section |
|---|---|
| 0 | Governance context |
| 1 | Unresolved items |
| 2 | Active workflows |
| 3 | Active investigations |
| 4 | Relevant memory |
| 5 | Execution lineage + runtime snapshots |

Items are accepted in tier order, then in their pre-sorted activation_rank order within each tier. Items exceeding the `max_chars` or `max_entries` limit are skipped. The raw context always preserves all candidates; the `BudgetedContext` contains only what fits.

**`truncated` flag**: set to `True` if any item was excluded due to budget exhaustion. Callers can inspect `total_candidates` vs `included_entries` to understand the shortfall.

---

### `session/reconstruction.py`

Orchestrates the full reconstruction pipeline.

| Function | Purpose |
|---|---|
| `reconstruct(memory_db_path, policy)` | Full session reconstruction — the primary entrypoint |
| `reconstruct_from_dict(context_dict, policy)` | Restore a SessionContext from its to_dict() form for audit/replay |

#### Reconstruction Pipeline

```
reconstruct(memory_db_path, policy)
    ├── activate_memory(memory_db_path, policy)           → List[ActivatedMemory]
    ├── partition_by_section(activated)                    → Dict[str, List[ActivatedMemory]]
    ├── _load_active_workflows(workflow_db_path, max_n)   → List[ActiveWorkflow]
    ├── _load_runtime_snapshots(runtime_db_path, max_n)   → List[RuntimeSnapshot]
    └── apply_context_budget(policy, ...)                 → BudgetedContext
        → SessionContext
        → SessionReconstruction
```

#### Session ID

```
session_id = SHA-256(memory_db_path | sorted(policy.tags) | policy.min_confidence | created_at)[:32]
```

The session_id is deterministic for the same inputs within the same second. It is used for audit tracing, not security.

#### Rendered Output Sections

```
SESSION RECONSTRUCTION
session_id : <32-char hex>
created_at : <ISO-8601 UTC>
budget     : <chars_used>/<max_chars>  (<entries> entries)

## ACTIVE GOVERNANCE CONTEXT
[mem:N] GOVERNANCE_RULE | confidence=5 | status=active
  Title   : ...
  Summary : ...

## ACTIVE WORKFLOWS
[wf:<16-char>] <workflow_id> | state=executing
  ...

## RECENT EXECUTION LINEAGE
...

## UNRESOLVED ITEMS
...

## RELEVANT MEMORY
...

## ACTIVE INVESTIGATIONS
...

## RUNTIME STATE
...
```

---

## Why Reconstruction is Deterministic

Every component in the pipeline is either:
- A pure function (activation scoring, context budgeting, session ID derivation)
- A deterministic DB query (retrieval uses fixed ORDER BY clauses)
- A structured data transformation (partition_by_section maps event types to sections)

No random sampling. No timestamp-dependent heuristics in the scoring. No ambient state injection. Given the same database rows and the same policy, the output is always identical.

---

## Why Embeddings are Deferred

Semantic similarity search (embeddings, vector stores) would require:

1. An embedding model and its associated version/reproducibility guarantees
2. A vector index that is not append-only and therefore not lineage-compatible
3. Non-deterministic nearest-neighbour search results across model updates
4. A governance gate for embedding model upgrades (model change = different activation)

None of these exist yet. Deterministic tag-and-type retrieval is a prerequisite for governed semantic search: you must be able to explain and audit exactly what was activated before you add probabilistic components. This layer establishes that baseline.

When embeddings are added, they will augment the `tag_overlap` dimension of the activation rank — not replace the deterministic tier system.

---

## Governance-Aware Activation

Governance items (`governance_rule`, `architecture_decision`) receive Tier 0 priority in the activation rank and are accepted into the context window before any other content. This means:

- A 12,000-char budget will always include governance items first, even if doing so leaves less room for high-confidence hypotheses
- Governance items are the last to be truncated
- If the budget is set to zero, nothing is included — governance is preserved first, not unconditionally

This mirrors the governance architecture in `memory.governance` and `memory.retrieval`, where doctrine priority is the primary sort dimension.

---

## Replayable Continuity

`SessionContext.to_dict()` serializes the full structured context including:
- All activated memory items with their source, type, confidence, and activation rank
- All active workflows with their execution state
- All runtime snapshots
- Budget accounting (total_candidates, included_entries, chars_used, truncated)
- The policy used for reconstruction

`reconstruct_from_dict(context_dict)` restores this context without re-querying the databases, enabling:
- Offline audit of a historical session
- Test assertion against a captured snapshot
- Comparison between sessions at different points in time

---

## Future Extension Paths

**Semantic activation layer:**
A future `SemanticActivationPolicy` can add an `embedding_overlap` dimension to the activation rank. Embeddings are computed once at write time and stored in `memory_events.embedding_json`. The semantic score is added to Tier 2/3 sorting, not Tier 0/1 (governance/unresolved remain deterministic).

**Tag expansion:**
A `TagExpansionGraph` can resolve semantic tag synonyms at query time (e.g. `'fx'` → also retrieves `'foreign_exchange'`, `'currency'`). Implemented as a pure function over a static dictionary — no embedding required.

**Session diffing:**
Given two `SessionContext.to_dict()` snapshots at different times, a future `diff_sessions()` function can highlight which items appeared, disappeared, or changed confidence — supporting incremental context review.

**CLI integration:**
A future `session-cli reconstruct --db MEM.db --tags fx macro --max-chars 8000` command surfaces reconstruction output for operator review without requiring a Python caller.

**Workflow-linked activation boost:**
A future activation dimension can boost memory items whose tags overlap with `workflow_id` or `plan_id` of active workflows — surfacing cognition that is directly relevant to in-progress work.

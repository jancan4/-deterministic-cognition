# Context Assembly CLI Architecture

## Purpose

The `session-context` command exports a deterministic context bundle from the memory
database for use in a new GPT or Claude session. It bridges the persisted cognition
layer with the human-facing AI conversation interface.

Every export is operator-triggered, read-only, and reproducible. There is no hidden
memory injection, no background daemon, and no LLM summarization in the export path.

---

## Cross-Chat Continuity

A session reconstruction bundle is a structured snapshot of relevant cognition state
at a point in time. When starting a new AI conversation:

1. The operator runs `session-context` to export the bundle.
2. The bundle is pasted (or file-attached) at the start of the new conversation.
3. The AI reads the bundle as explicit context — not as injected memory.
4. The conversation begins with the AI's working knowledge already populated from the
   system's persisted lineage.

This pattern provides continuity without relying on chat history, model memory features,
or fine-tuning. The operator retains full control over what context is exported and when.

```
Persisted memory (SQLite)
        │
        ▼
session-context CLI
        │
        ├─── JSON bundle ──► paste into new chat / attach as file
        │
        └─── Markdown bundle ──► paste into system prompt or first message
```

---

## Why Output is Deterministic

The export pipeline contains no randomness:

1. **Memory retrieval** uses fixed `ORDER BY id ASC` in all SQL queries.
2. **Activation ranking** is a deterministic composite sort key:
   `(tier, doctrine_rank, -confidence, recency_rank, -tag_overlap, id)`.
3. **Context budgeting** accepts items in tier order, then rank order, with no
   randomised tie-breaking.
4. **Filters** (`--tag`, `--event-type`, `--status`) are applied as exact-match
   predicates, not fuzzy or probabilistic selectors.

Given the same database state and the same CLI flags, `session-context` always produces
the same `session_id`, the same section contents, and the same character count.

The `session_id` is a SHA-256 of `(memory_db_path, sorted(tags), min_confidence, created_at)`.
It changes across different seconds (timestamps differ) but is stable for the same
database path and policy within the same second — useful for test assertions and
audit trail correlation.

---

## Command Reference

```
python -m cli.main session-context [options]
```

| Option | Default | Description |
|---|---|---|
| `--db PATH` | `memory.db` | Memory SQLite database |
| `--workflow-db PATH` | (none) | Workflow DB; adds ACTIVE WORKFLOWS section |
| `--max-entries N` | 60 | Maximum entries in the context bundle |
| `--max-chars N` | 12000 | Maximum character budget |
| `--tag TAG` | (none) | Tag filter (repeatable) |
| `--event-type TYPE` | (none) | Post-filter by event type (repeatable) |
| `--status STATUS` | (none) | Post-filter by status (repeatable) |
| `--out PATH` | (stdout) | Write to file instead of stdout |
| `--format json\|markdown` | `markdown` | Output format |

### `--db` vs `--workflow-db`

`--db` is always the **memory** database for this command (default: `memory.db`).
`--workflow-db` is optional. When provided, non-terminal workflow executions are
fetched and included in the `ACTIVE WORKFLOWS` section.

This separation keeps the two stores independent: memory contains cognitive lineage,
workflow contains execution lineage. The session bundle can include both.

---

## Output Sections

All sections are populated from persisted lineage. Empty sections are omitted.

| Section | Source |
|---|---|
| `ACTIVE GOVERNANCE CONTEXT` | `governance_rule`, `architecture_decision` events |
| `ACTIVE WORKFLOWS` | Non-terminal workflow executions (requires `--workflow-db`) |
| `RECENT EXECUTION LINEAGE` | Terminal workflows (not surfaced by default) |
| `UNRESOLVED ITEMS` | Events with `status in {unresolved, proposed}` |
| `RELEVANT MEMORY` | All other activated events |
| `ACTIVE INVESTIGATIONS` | `open_question`, `hypothesis` events |
| `RUNTIME STATE` | Runtime process snapshots (requires `--runtime-db`) |

### Section Priority in Context Budgeting

When the budget is exhausted, sections are dropped from lowest to highest priority:

```
Tier 0: ACTIVE GOVERNANCE CONTEXT   ← always preserved first
Tier 1: UNRESOLVED ITEMS
Tier 2: ACTIVE WORKFLOWS
Tier 3: ACTIVE INVESTIGATIONS
Tier 4: RELEVANT MEMORY
Tier 5: RECENT EXECUTION LINEAGE / RUNTIME STATE  ← dropped first
```

Governance context is never dropped before any other section. An operator setting
`--max-chars 500` will still see governance items in the output if they fit.

---

## Filtering

Two filter layers apply in sequence:

### 1. Activation filter (`--tag`)

Tags narrow the **retrieval** phase. Providing `--tag fx` causes the retrieval
layer to score events with the `fx` tag higher, and the general retrieval pass
returns tag-matching events first. This affects which events enter the context
window, not just which are displayed.

### 2. Output filter (`--event-type`, `--status`)

These are **post-reconstruction** predicates applied to the assembled memory
sections before output. They do not re-run retrieval.

- `--event-type governance_rule` → only governance_rule items appear in memory sections
- `--status unresolved` → only unresolved items appear in memory sections

Workflow and runtime sections are never filtered by `--event-type` or `--status`.

**Usage example:** export only governance rules for a compliance review:

```bash
python -m cli.main session-context \
    --db memory.db \
    --event-type governance_rule \
    --event-type architecture_decision \
    --format markdown \
    --out governance-review.md
```

---

## Output Formats

### Markdown (default)

Human-readable, designed for direct pasting into a chat session or system prompt.

```
# SESSION CONTEXT
session_id : <32-char hex>
created_at : <ISO-8601 UTC>
budget     : <chars>/<max_chars> chars  (<entries> entries)

## ACTIVE GOVERNANCE CONTEXT

[mem:1] GOVERNANCE_RULE | confidence=5 | status=active
  Title   : No live capital deployment without quant validation
  Summary : All strategy deployments require sign-off from the risk engine.
  Tags    : governance, risk

## UNRESOLVED ITEMS
...
```

### JSON

Machine-readable structured data. Stable schema for programmatic processing,
diff-based session comparison, and automated handoff tooling.

```json
{
  "session_id": "...",
  "created_at": "...",
  "char_budget": 12000,
  "chars_used": 1957,
  "total_candidates": 7,
  "included_entries": 7,
  "truncated": false,
  "filters": { "tags": [], "event_types": [], "statuses": [] },
  "sections": {
    "governance_context": [...],
    "unresolved_items": [...],
    "active_workflows": [...],
    "active_investigations": [...],
    "relevant_memory": [...],
    "execution_lineage": [],
    "runtime_snapshots": []
  }
}
```

Every item in a section includes: `memory_id`, `event_type`, `title`, `summary`,
`evidence`, `confidence`, `status`, `tags`, `source`, `related_ids`,
`created_at`, `updated_at`, `is_expanded`, `tag_overlap`.

---

## How to Use the Exported Bundle

### Starting a new Claude session

```bash
# Export
python -m cli.main session-context --db memory.db --format markdown > context.md

# In the new chat, start with:
cat context.md | pbcopy   # paste into the first message
# or attach context.md as a file
```

### Starting a new GPT session

```bash
# Export JSON for programmatic use
python -m cli.main session-context --db memory.db --format json --out context.json

# Upload context.json as a file attachment in the new conversation
```

### Focused handoff (governance only)

```bash
python -m cli.main session-context \
    --db memory.db \
    --event-type governance_rule \
    --event-type architecture_decision \
    --format markdown \
    --out governance-context.md
```

### Workflow-aware handoff

```bash
python -m cli.main session-context \
    --db memory.db \
    --workflow-db workflow.db \
    --format markdown \
    --out full-context.md
```

---

## Why This Is Operator-Triggered, Not Hidden Injection

Hidden memory injection (background context prepending, autonomous retrieval
pipelines, model fine-tuning on session history) creates auditability problems:

- The AI cannot show the operator what was injected and why.
- The injection criteria are opaque and drift over time.
- There is no governance gate before cognitive context enters a session.

The `session-context` command is the opposite:

- **Explicit**: the operator decides when to export and what filters to apply.
- **Inspectable**: the operator reads the bundle before pasting it.
- **Auditable**: `session_id`, `total_candidates`, `included_entries`, and
  `truncated` are all visible in the output header.
- **Immutable**: export does not write to any database. Running `session-context`
  twice on the same db produces identical output.

The operator is the cognitive gateway. The system provides the bundle;
the operator decides when and where it goes.

---

## No-Mutation Guarantee

`session-context` opens the memory database in read-only mode (via `sqlite3.connect`
with no write operations). The underlying `session.reconstruction.reconstruct()` and
all retrieval functions are read-only by design. No `INSERT`, `UPDATE`, or `DELETE`
is issued against any database during export.

This is verified by the test suite: `test_session_context_does_not_mutate_memory`
re-reads the memory event after export and asserts that title, status, confidence,
and version are unchanged.

---

## Future Extension Paths

**Semantic tag expansion:**  
A future `--expand-tags` flag can resolve tag synonyms before retrieval (e.g.
`--tag fx` also retrieves `foreign_exchange`, `currency`). Implemented as a static
dictionary lookup — no embedding model required.

**Session diff:**  
A future `session-diff --before BUNDLE_A --after BUNDLE_B` command can compare two
JSON bundles and highlight which items appeared, disappeared, or changed confidence.

**Scheduled export:**  
A future `session-export-cron` hook can export a bundle on a schedule (e.g. after
each workflow completion) and write it to a known path, ready for the next session
without operator intervention.

**Runtime-aware export:**  
When `--runtime-db` is added, the `RUNTIME STATE` section will show the state
of active runtime processes at export time, enabling recovery-aware handoffs.

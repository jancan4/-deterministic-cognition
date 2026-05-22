# Semantic Model Execution CLI Architecture

## Overview

This document describes the `feature/semantic-model-execution-cli` milestone, which exposes
deterministic semantic model execution through the CLI and integrates semantic enrichment into
the ingestion pipeline.

---

## Components

### `models/registry.py` — `AdapterRegistry`

Explicit, ordered registry that maps adapter names to `LocalModelAdapter` instances.

**Key invariants:**
- Registration is explicit: no dynamic discovery, no implicit adapters, no network calls.
- `list_names()` always returns sorted adapter names — ordering is deterministic regardless of
  registration order.
- Duplicate names raise `AdapterRegistryError` unless `replace=True` is passed.
- `make_default_registry()` pre-registers `stub` and `echo` adapters; each call returns an
  independent registry instance (no shared mutable state).

**Error surface:**
- `AdapterRegistryError(ValueError)` — raised for: registering a non-`LocalModelAdapter`,
  duplicate name without replace, get/unregister of an unregistered name.

---

### `semantic/pipeline.py` — Semantic Pipeline

Connects the semantic contract layer (`SemanticTask`, `SemanticExtractionResult`) with the model
adapter layer (`LocalModelAdapter`, `execute_with_policy`) and the ingestion layer
(`CandidateMemoryEvent`).

**Design rules:**
- No database writes at any point.
- All results are candidates only (`status='proposed'`, `committed_id=None`).
- All output is deterministically serializable (`to_dict()`, `to_json()`, `to_markdown()`).
- Validation happens at every layer boundary.

**Constants:**
```
SEMANTIC_DEFAULT_EVENT_TYPE = 'hypothesis'
SEMANTIC_ENRICHMENT_CREATED_BY = 'semantic-enrichment'
SEMANTIC_PIPELINE_CREATED_BY = 'semantic-pipeline'
```

#### `SemanticPipelineResult`

Complete output of one `run_semantic_task()` call:

| Field | Type | Description |
|-------|------|-------------|
| `task` | `SemanticTask` | The task that was executed |
| `execution_result` | `ModelExecutionResult` | Full execution with timing metadata |
| `semantic_result` | `Optional[SemanticExtractionResult]` | Validated extraction result (None on failure) |
| `candidates` | `List[CandidateMemoryEvent]` | Proposed candidates, never committed |
| `success` | `bool` | True if execution and conversion succeeded |
| `error` | `Optional[str]` | Error message (None on success) |

Serialization:
- `to_dict()` — `{task, execution, semantic_result, candidates, success, error}`
- `to_json()` — `json.dumps(sort_keys=True, indent=2)`
- `to_markdown()` — human-readable sections: Task, Execution, Labels, Entities, Claims, Relations, Summary, Candidates

#### `run_semantic_task()`

Five-step pipeline:
1. `make_task()` — build and validate `SemanticTask` (raises `SemanticValidationError` on bad input)
2. `task_to_request()` — convert to `LocalModelRequest`
3. `execute_with_policy()` — execute via adapter (captures timing, retries, errors)
4. Extract `SemanticExtractionResult` from execution result
5. `_try_generate_candidates()` — optionally build `CandidateMemoryEvent` list

`SemanticValidationError` propagates to the caller; all other errors are captured in
`SemanticPipelineResult.error`.

**Candidate generation rule:** A candidate is generated when `generate_candidates=True` and the
semantic result contains at least one label, entity, claim, or summary. Empty results produce no
candidates.

#### `enrich_chunks_with_semantic()`

Runs `run_semantic_task()` on each `Chunk`, collecting all candidates. Chunks with invalid/empty
text are silently skipped. No database writes.

---

## CLI Commands

### `semantic-run`

Run a single semantic task via a named adapter.

```
python -m cli.main semantic-run \
  --task-type tagging \
  --input-text "The Fed held rates steady." \
  --adapter stub \
  --format json
```

Flags:
- `--task-type` — one of `SEMANTIC_TASK_TYPES` (required)
- `--input-text TEXT` / `--input-file PATH` — mutually exclusive input sources (one required)
- `--adapter NAME` — adapter name from the default registry (required)
- `--format {json,markdown}` — output format (default: `json`)
- `--source-id ID` — optional source attribution
- `--timeout SECONDS` — override execution timeout (positive float)

Output is written to stdout. Exits 0 on success, 1 on error.

### `ingest-file --semantic-adapter NAME`

Optional flag added to the existing `ingest-file` command. When set, runs semantic enrichment
on all ingested chunks after the main ingestion pass.

```
python -m cli.main ingest-file \
  --path /data/article.txt \
  --db /data/store.db \
  --semantic-adapter stub \
  --commit
```

Behavior:
- Semantic enrichment runs regardless of whether `--commit` is set.
- When `--commit` is set and candidates are generated, `memory.service.init_db()` is called
  before `commit_candidates()` to ensure the memory schema exists.
- Semantic metadata is included in the JSON output under `semantic_candidates` and
  `semantic_enrichment` keys.
- Errors in the semantic path are caught and reported as `WARNING:` on stderr; they do not
  fail the overall ingestion run.

---

## Dependency Graph

```
cli/main.py
  └─ semantic/pipeline.py
       ├─ semantic/contracts.py   (make_task, result_to_candidate)
       ├─ models/execution.py     (execute_with_policy)
       └─ models/adapters.py      (LocalModelAdapter)
  └─ models/registry.py
       └─ models/adapters.py      (StubModelAdapter, EchoModelAdapter)
  └─ memory/service.py            (init_db — only when --commit)
  └─ ingestion/candidates.py      (commit_candidates — only when --commit)
```

`semantic/` has no import dependency on `models/`. The dependency direction is
`models/ → semantic/` only, enforced at the module boundary.

---

## Governing Invariants

| Invariant | Enforcement |
|-----------|-------------|
| No live model calls | `LocalModelAdapter.execute()` is local-only; no HTTP/gRPC |
| No DB writes in pipeline | `run_semantic_task()` and `enrich_chunks_with_semantic()` never open a DB |
| Candidates always proposed | `result_to_candidate()` hardcodes `status='proposed'`, `committed_id=None` |
| Deterministic IDs | `task_id = sha256(task_type + NUL + input_text + NUL + source_id)[:16]` |
| Schema initialized before write | `init_db()` called before `commit_candidates()` in CLI |
| Registry independence | Each `make_default_registry()` call returns a new instance |

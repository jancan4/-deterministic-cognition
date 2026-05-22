# Semantic Memory Commit Layer Architecture

## Overview

This document describes the `feature/semantic-memory-commit-layer` milestone.

The semantic memory commit layer closes the gap between semantic extraction
(generating candidates) and durable memory (writing committed knowledge). It
introduces:

1. A durable semantic execution ledger — persists every extraction run and
   its candidates before any promotion decision.
2. A governed promotion path — semantic candidates enter memory as
   `status='unresolved'`, requiring explicit operator review.
3. Operator review CLI — `memory-review approve/reject` transitions events
   through `update_status()`, preserving `memory_revisions` lineage.

---

## KNOWN LIMITATION — Continuity Bundle Portability

**The continuity bundle exporter (`continuity/exporter.py`) does not yet
export `semantic_execution_runs` or `semantic_candidate_events`.**

The consequence is:

| Artifact | Portable via bundle | Notes |
|----------|--------------------|----|
| Promoted `memory_events` | **Yes** | Row written to `memory_events`; included in bundle |
| `evidence` string (references run/candidate ID) | **Yes** | Stored in `memory_events.evidence`; preserved in bundle |
| `memory_revisions` (approval/rejection history) | **Yes** | Included in bundle |
| `semantic_execution_runs` rows | **No** | Source-system-local only |
| `semantic_candidate_events` rows | **No** | Source-system-local only |
| Full semantic provenance (normalized_result, source_span, provenance_json) | **No** | Source-system-local only |
| Replay of candidate generation | **No** | Requires ledger rows + original adapter |

**This is an accepted, explicit milestone limitation.** It is not treated as
complete provenance portability. Ledger bundle support is planned as a
follow-on milestone.

The evidence string format is designed for future resolution:

```
semantic:local_model:stub:1.0.0 | run:a9e1774d0baea132 | candidate:3f8c1a2b9d4e7f01
```

Given only a promoted `memory_event` on a new substrate, an operator can:
- See the adapter name and version from the evidence string.
- Know the run_id and candidate_id that would exist in the source system's ledger.
- Re-run extraction on the original source (if available) to regenerate ledger rows.

---

## Components

### `semantic/ledger.py` — Semantic Execution Ledger

**Tables (embedded schema — no external `.sql` file):**

```
semantic_execution_runs      — one row per run_semantic_task() call
semantic_candidate_events    — one row per generated candidate
```

Both tables use `CREATE TABLE IF NOT EXISTS` and `_ensure_schema()` called at
every public write entry point. Pattern mirrors `ingestion/runs.py` exactly.

**ID derivation:**

| ID | Formula |
|----|---------|
| `run_id` | `= request_id = sha256(adapter_name + NUL + adapter_version + NUL + task_type + NUL + input_text)[:16]` |
| `candidate_id` | `sha256(run_id + NUL + str(candidate_index))[:16]` |
| `input_hash` | `sha256(input_text)[:16]` — adapter-independent; groups runs by text |

All IDs are deterministic. Re-running the same extraction produces the same
IDs, making `record_run()` idempotent via `INSERT OR IGNORE`.

**`record_run(db_path, pipeline_result, execution_policy, model_metadata)`**

- Writes one `semantic_execution_runs` row.
- Writes one `semantic_candidate_events` row per candidate.
- Both are `INSERT OR IGNORE` — safe to call multiple times with the same input.
- No memory write.

**`promote_candidate(db_path, candidate_id, approved_by)`**

The single governed write boundary for semantic-to-memory promotion:

1. Fetch candidate from ledger; raise `LedgerNotFoundError` if absent.
2. Validate `status='candidate'`; raise `LedgerError` otherwise.
3. Call `memory.service.add_memory_event(status='unresolved')`.
4. Write evidence string: `semantic:<method> | run:<run_id> | candidate:<cid>`.
5. Call `update_candidate_status(candidate_id, 'promoted', promoted_memory_id)`.

No writes occur on any validation failure.

**`update_candidate_status(db_path, candidate_id, new_status, promoted_memory_id)`**

Valid transitions: `candidate → promoted` (requires `promoted_memory_id`) or
`candidate → rejected`. No other transitions are valid. Incrementing
`promoted_count` on `semantic_execution_runs` happens atomically in the same
transaction.

---

### `semantic/pipeline.py` — Updated Return Type

`enrich_chunks_with_semantic()` now returns `List[SemanticPipelineResult]`
(previously `List[CandidateMemoryEvent]`). One result per non-empty chunk.

Callers that need only flat candidates:
```python
results = enrich_chunks_with_semantic(chunks, adapter)
candidates = [c for r in results for c in r.candidates]
```

This change was required to give the CLI access to `execution_result.request_id`
(the `run_id`) per chunk without a secondary lookup.

---

### CLI Changes

**`ingest-file --semantic-adapter NAME [--commit]`**

Updated behavior:

| Step | Always | Only with `--commit` |
|------|--------|---------------------|
| `enrich_chunks_with_semantic()` | Yes | Yes |
| `init_ledger()` + `record_run()` | Yes | Yes |
| `init_db()` + `promote_candidate()` | No | Yes |

Promoted candidates enter memory as `status='unresolved'`.
`semantic_enrichment.promoted_memory_ids` lists the inserted memory IDs.

**`semantic-candidates [--db] [--run-id] [--status] [--source-id] [--limit]`**

Read-only. Lists `semantic_candidate_events` from the ledger. No memory write.

**`memory-review list [--db] [--status] [--event-type]`**

Wraps `memory.service.review_memory()`. No write. Default surfaces
`proposed`, `unresolved`, `active` (the `REVIEW_STATUSES` set).

**`memory-review approve --id N --by OPERATOR [--status active|accepted]`**

Calls `memory.service.update_status(id, new_status, reason='operator approval', created_by=OPERATOR)`.
Writes one row to `memory_revisions`. Default target status: `active`.

**`memory-review reject --id N --by OPERATOR --reason REASON`**

Calls `memory.service.update_status(id, 'rejected', reason=REASON, created_by=OPERATOR)`.
Writes one row to `memory_revisions`. The rejected event is preserved — no DELETE.

---

## State Machine

### Semantic candidate lifecycle

```
(ephemeral CandidateMemoryEvent)
         ↓  record_run()
  semantic_candidate_events.status = 'candidate'
         ↓  promote_candidate()
  status = 'promoted'  →  memory_events.status = 'unresolved'
         ↓  memory-review approve
  memory_events.status = 'active' | 'accepted'   [memory_revisions row written]
         ↓  memory-review reject
  memory_events.status = 'rejected'               [memory_revisions row written]
```

Separately, a candidate in the ledger that was never promoted can be rejected
at the ledger level:
```
  semantic_candidate_events.status = 'candidate'
         ↓  update_candidate_status(..., 'rejected')
  status = 'rejected'    (no memory_events write)
```

### Memory event preservation

Rejected `memory_events` rows are never deleted. The `memory_revisions` table
is append-only. Rejection is a status transition, not a deletion.

---

## Governing Invariants

| Invariant | Where enforced |
|-----------|---------------|
| No automatic memory write from semantic output | `promote_candidate()` is the only write path; CLI calls it explicitly |
| Promotion creates `status='unresolved'` | Hardcoded in `promote_candidate()` |
| Approval/rejection via `update_status()` | All `memory-review` handlers call `mem_service.update_status()` only |
| `memory_revisions` append-only lineage | `update_status()` in `memory/service.py` — INSERT only, no UPDATE/DELETE |
| Candidate transitions: candidate → promoted | xor | rejected | `update_candidate_status()` validates `current_status == 'candidate'` |
| No ontology registry | Labels/entity types remain free-form strings |
| No real local inference | `raw_output_json` reserved; `None` for stub/echo |
| Ledger writes idempotent | `INSERT OR IGNORE` on `run_id` and `candidate_id` |

---

## Dependency Graph

```
cli/main.py
  └─ semantic/ledger.py           (record_run, promote_candidate)
       └─ memory/service.py       (add_memory_event — only from promote_candidate)
  └─ semantic/pipeline.py         (enrich_chunks_with_semantic → List[SemanticPipelineResult])
  └─ memory/service.py            (review_memory, update_status — from memory-review commands)
```

`semantic/ledger.py` imports `memory/service.py` only inside `promote_candidate()`.
This lazy import preserves the ability to use the ledger read paths without
initializing the memory schema.

---

## Follow-On Work (Not This Milestone)

1. **Ledger bundle export/import** — add `_fetch_semantic_runs()` and
   `_fetch_semantic_candidates()` to `continuity/exporter.py`; corresponding
   import logic in `continuity/importer.py`. Until then, ledger provenance is
   source-system-local.

2. **Real adapter provenance fields** — when Ollama/llama.cpp adapters are
   added, populate `model_metadata_json` with weights checksum, quantization
   type, inference parameters, and `raw_output_json` with pre-normalization
   output. The schema columns already exist; they are `NULL` for stub/echo.

3. **Ontology registry** — labels and entity types are free-form strings
   throughout. Stabilization is a separate milestone.

4. **Batch review CLI** — `memory-review approve-all --run-id R` for bulk
   promotion confirmation. Not implemented; single-event approval only.

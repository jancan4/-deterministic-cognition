# Ingestion Run Ledger Architecture

## Purpose

The ingestion run ledger records every execution of the ingestion pipeline as a
durable, inspectable provenance event. It answers the question:

> *When did this document get processed, what exact version of the file was used,
> how many candidates were extracted, and which memory events (if any) were
> committed from that run?*

The ledger is the connective tissue between source documents and memory events.
It makes the full provenance chain traversable without joins across three tables:

```
source_documents
    source_id: "451a795e4f8827f4"
    path: "/path/to/research.md"
    checksum: "518c0c6f..."
         │
         │ source_id  ──────────────────────────────────┐
         ▼                                              │
ingestion_runs                                         │
    run_id: "67f9c32525bec458"                         │
    source_id: "451a795e4f8827f4"     ◄────────────────┘
    source_checksum_sha256: "518c0c6f..."
    committed_memory_ids: [12, 13, 14]
         │
         │ committed_memory_ids
         ▼
memory_events
    id: 12  source: "/path/to/research.md"
    id: 13  source: "/path/to/research.md"
    id: 14  source: "/path/to/research.md"
```

---

## Source Document Registry vs Ingestion Run Ledger

These are two distinct layers with different jobs:

| Concern | Source Registry (`source_documents`) | Run Ledger (`ingestion_runs`) |
|---|---|---|
| **What it tracks** | What files exist and their content versions | When each file was processed and what resulted |
| **Identity key** | `(path, checksum)` | `(source_id, checksum, started_at)` |
| **Write trigger** | File first seen or content changed | Every `ingest-file` execution |
| **Idempotency** | Same file → same record returned | Each run → new record |
| **Links to** | — (source of truth) | `source_documents` + `memory_events` |
| **Primary use** | "What version of this file did we ingest?" | "When and how did ingestion happen?" |

A single source document may have many ingestion runs (e.g., re-ingested after
rule changes). A run record exists even if zero candidates were extracted or
zero memory events were committed.

---

## Why Ingestion Runs Are Provenance Events

Memory events are claims about the world. Their trustworthiness depends not just
on the source document but on the exact pipeline state that extracted them:

- Which parser version parsed the file?
- Which extractor version applied which rules?
- How many chunks were created? (More chunks = finer granularity)
- Were all candidates committed, or only a subset?

If extraction rules are updated (new keyword patterns, new event types), old
memory events extracted under the previous rule set remain tagged with the
old `extractor_version`. An operator can query the ledger to identify which
memory events need re-evaluation.

---

## Run Lifecycle

```
ingest-file executes
        │
        ├── source.register_source()        ← source_documents write
        │
        ├── parse_file()
        │
        ├── chunk_document()
        │
        ├── extract_candidates()
        │          │
        │          ├── default (no --commit)
        │          │       └── record_run(status='candidate_generated')
        │          │
        │          └── --commit
        │                  ├── memory.service.add_memory_event() × N
        │                  └── record_run(status='committed', committed_ids=[...])
        │
        └── on exception after source registration
                └── record_run(status='failed', metadata={'error': ...})
```

### Approved Statuses

| Status | Meaning |
|---|---|
| `candidate_generated` | Candidates extracted; `--commit` not passed; no memory writes |
| `committed` | Candidates committed to `memory_events`; `committed_memory_ids` populated |
| `failed` | Exception occurred during ingestion; `metadata.error` contains the message |

---

## run_id Derivation

```python
run_id = sha256( source_id + '\x00' + source_checksum + '\x00' + started_at )[:16]
```

Properties:
- **Deterministic** per `(source_id, checksum, started_at)` — same source version
  processed at the same second always produces the same `run_id`.
- **Unique across time** — different `started_at` values produce different `run_id`s
  for the same source version.
- **Lineage-safe** — the `run_id` embeds both source identity and wall-clock time,
  making it auditable without a random component.

> Note: two runs on the same source started within the same UTC second would
> produce the same `run_id`. In practice this is a non-issue for a CLI tool.
> Sub-second precision can be added if batch processing is introduced.

---

## Data Model

```
IngestionRun
  run_id                  : str   — sha256(source_id + NUL + checksum + NUL + started_at)[:16]
  source_id               : str   — links to source_documents.source_id
  source_checksum_sha256  : str   — checksum at time of ingestion (64-char hex)
  source_version          : int   — version number from source_documents
  parser_version          : str   — ingestion.parser.PARSER_VERSION at run time
  extractor_version       : str   — ingestion.extractor.EXTRACTOR_VERSION at run time
  chunk_count             : int   — number of chunks produced by chunker
  candidate_count         : int   — number of candidates after extraction and dedup
  committed_count         : int   — number of candidates committed (0 without --commit)
  committed_memory_ids    : list  — memory_event.id values written (empty without --commit)
  status                  : str   — candidate_generated | committed | failed
  started_at              : str   — UTC ISO-8601 before parse_file()
  completed_at            : str?  — UTC ISO-8601 after all operations (None if crashed)
  metadata                : dict  — arbitrary; 'error' key populated on failure
```

---

## Database Layout

The `ingestion_runs` table lives alongside `source_documents` and `memory_events`
in the same memory SQLite database.

```sql
CREATE TABLE ingestion_runs (
    id                        INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id                    TEXT UNIQUE NOT NULL,
    source_id                 TEXT NOT NULL,
    source_checksum_sha256    TEXT NOT NULL,
    source_version            INTEGER NOT NULL,
    parser_version            TEXT NOT NULL,
    extractor_version         TEXT NOT NULL,
    chunk_count               INTEGER NOT NULL DEFAULT 0,
    candidate_count           INTEGER NOT NULL DEFAULT 0,
    committed_count           INTEGER NOT NULL DEFAULT 0,
    committed_memory_ids_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL CHECK (status IN ('candidate_generated','committed','failed')),
    started_at                TEXT NOT NULL,
    completed_at              TEXT,
    metadata_json             TEXT NOT NULL DEFAULT '{}'
);
```

There is no foreign key from `ingestion_runs.source_id` to `source_documents`.
The link is an application-level join. This keeps the tables independently
readable even if the source document is later deprecated or deleted.

---

## How Replay and Debugging Use Run Lineage

**"Which runs extracted candidates that were never committed?"**
```bash
python -m cli.main ingestion-runs --db memory.db --status candidate_generated
```

**"What was the extractor version when memory event 42 was created?"**
1. `memory_event[42].source` → file path
2. `source_documents WHERE path = ?` → source_id
3. `ingestion_runs WHERE source_id = ? AND committed_memory_ids CONTAINS 42` → extractor_version

**"Re-ingest after rule update: which sources need re-processing?"**
```
SELECT DISTINCT source_id FROM ingestion_runs
WHERE extractor_version < '2.0'
  AND status IN ('candidate_generated', 'committed')
```

**"Did a failed run happen for this source?"**
```bash
python -m cli.main ingestion-runs --db memory.db --source-id abc1234500000000 --status failed
```

---

## Version Constants

`ingestion/parser.py` exports `PARSER_VERSION = '1.0'`.  
`ingestion/extractor.py` exports `EXTRACTOR_VERSION = '1.0'`.

Both are plain string constants. Bumping either constant causes all subsequent
runs to record the new version, enabling backward-traceable queries.

---

## No Hidden Mutation

`record_run()` writes only to `ingestion_runs`. It does not touch:
- `memory_events` (owned by `memory.service`)
- `source_documents` (owned by `sources.registry`)
- Any other table

The run record is written after all other operations complete. If `record_run()`
itself fails, the pipeline returns an error but the memory writes (if any)
already committed to the DB are not rolled back — they are durable.

---

## Future Extension Paths

**Local-model extraction tier:**  
When a `--llm-pass` or `--local-model` flag is added to `ingest-file`, the
extractor version string will include the model name/revision, e.g.,
`'1.0+llm:mistral-7b-q4'`. The run ledger records this automatically without
schema changes.

**Batch ingestion:**  
A future `ingest-dir` command would create one run record per file, not one
for the whole batch. The ledger's per-file granularity is batch-compatible
by design.

**Incremental re-ingestion detection:**  
A future helper `should_reingest(db, path)` would query the run ledger for
the most recent committed run for the current source version of `path`. If
a committed run exists for the current checksum, re-ingestion is optional
(the operator decides). If only `candidate_generated` runs exist, the operator
is prompted to commit.

**Extractor version migration:**  
When `EXTRACTOR_VERSION` is bumped, a future `reingest-stale` command would
query all committed runs with the old extractor version, re-extract candidates
from the original source, and present a diff of new vs. old candidates for
operator review.

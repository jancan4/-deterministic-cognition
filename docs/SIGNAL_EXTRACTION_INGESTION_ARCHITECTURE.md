# Signal Extraction and Memory Ingestion Architecture

## Purpose

The ingestion pipeline converts unstructured source documents (plain text, Markdown,
research notes, architecture records) into `CandidateMemoryEvent` objects for operator
review and optional commit to the memory database.

It bridges the gap between the raw knowledge base and the persisted cognition layer,
without bypassing operator governance.

---

## Design Principles

### Rule-Based First

Extraction uses deterministic keyword, pattern, and heuristic rules — no LLM calls,
no embeddings, no network requests. The same document always produces the same
candidates. This enables:

- Reproducible audits
- Test coverage of extraction rules
- Operator confidence that no hidden model is classifying content

Probabilistic extraction (LLM-based) is an explicit future extension path, not the
current baseline.

### Candidate vs Committed Memory

The pipeline produces **candidates** — proposed events that have not yet entered the
memory database. Candidates become memory events only when the operator explicitly
passes `--commit` to the CLI.

```
source document
     │
     ▼
ParsedDocument  ──── parse_text / parse_file
     │
     ▼
List[Chunk]     ──── chunk_document (paragraph or fixed-size)
     │
     ▼
List[CandidateMemoryEvent]  ──── extract_candidates (rules applied)
     │
     ├──── default mode: print to stdout / write to --out JSON
     │
     └──── --commit mode: commit_candidates → memory_events table
```

### Source Attribution

Every `CandidateMemoryEvent` carries a `SourceSpan` with:
- `start`: character offset into the source document
- `end`: exclusive end offset
- `text`: the exact extracted text slice

These offsets are preserved through the commit step, enabling operators to trace
any memory event back to its exact origin sentence.

### Operator as Gateway

The pipeline never writes to the database without explicit operator instruction.
This preserves the governance invariant: **AI may propose; human approves**.

---

## Pipeline Stages

### 1. Parsing (`ingestion/parser.py`)

```
raw file / string
     │
_normalise_text  ──── LF normalisation, trailing whitespace stripped
     │
_parse_frontmatter  ──── YAML-like key: value header if present
     │
_source_id  ──── SHA-256(normalised_text)[:16]
     │
ParsedDocument
```

- Supports `.txt`, `.md`, `.markdown`, and files with no extension.
- `source_id` is the first 16 hex characters of SHA-256 of the normalised text.
  Stable across re-parses of the same content; changes if content changes.
- `raw_text` always contains the full normalised text including any frontmatter.

### 2. Chunking (`ingestion/chunker.py`)

Two strategies:

**Paragraph chunking** (default when `\n\n` is present):
- Splits on `\n{2,}` (two or more consecutive newlines).
- Each non-empty paragraph becomes one chunk.
- Oversized paragraphs (> `max_chunk_chars`) are sub-chunked by fixed-size logic.
- Start/end offsets reference the document's `raw_text`.

**Fixed-size chunking** (fallback for dense single-paragraph text):
- Splits at `max_chunk_chars` boundaries, aligned to the nearest word boundary.
- Consecutive chunks overlap by `overlap_chars` characters to avoid cutting
  multi-sentence signals at a boundary.

`chunk_document()` auto-selects: paragraph if `'\n\n'` present, else fixed-size.

All chunks carry `source_path`, `source_id`, `chunk_index`, `start_char`, `end_char`.

### 3. Extraction (`ingestion/extractor.py`)

Three rule families applied to each chunk:

**KeywordRule**: fires on exact whole-word match (case-insensitive). Emits one
candidate per chunk regardless of how many keyword occurrences exist.

**PatternRule**: fires on a compiled regex match. The first capturing group (if
present) seeds the title. Allows richer context: "decided to use X", "rejected
because Y", "result: Z".

**HeuristicRule**: fires on structural signals — trailing question marks, `if…then`
constructs, numbered lists. Catches patterns that keywords and patterns miss.

Each rule maps to one of the 12 `EXTRACTABLE_EVENT_TYPES`:

| Event type | Typical signal |
|---|---|
| `architecture_decision` | "ADR", "decided to use", "chose" |
| `governance_rule` | "must not", "never allow", "no live capital" |
| `hypothesis` | "hypothesis:", "we believe that", "if…then" |
| `experiment` | "backtested", "we tested", "a/b test" |
| `validation_result` | "result:", "Sharpe ratio", "drawdown" |
| `adaptation` | "adapted to", "regime change", "recalibrated" |
| `regime_observation` | "risk-off", "risk-on", "hawkish", "dovish" |
| `implementation_note` | "note:", "warning:", "TODO:" |
| `open_question` | "open question:", "tbd", trailing "?" |
| `rejected_idea` | "rejected because", "decided against" |
| `incident` | "incident:", "post-mortem", "outage" |
| `source_reference` | "see:", "https://", "doi:" |

Rules are applied in registration order. Each rule emits at most one candidate
per chunk. A rule exception never aborts extraction of the remaining rules on the
same chunk.

### 4. Candidate Pipeline (`ingestion/candidates.py`)

```
List[CandidateMemoryEvent] (from extractor)
     │
confidence filter  ──── discard < MIN_CANDIDATE_CONFIDENCE (default: 2)
     │
deduplication  ──── keep highest confidence per (source, chunk_index, event_type)
     │
sort  ──── by (source_span.start, event_type)
     │
List[CandidateMemoryEvent]  ──── presented to operator
     │
     └──── --commit: commit_candidates → INSERT INTO memory_events
```

Deduplication key: `(source_path, chunk_index, event_type)`. When two rules
produce the same event type from the same chunk, the higher-confidence candidate
survives. On tie, registration order wins (stable).

---

## Data Models (`ingestion/models.py`)

```
ParsedDocument
  source_path : str           — file path or '<inline>'
  source_id   : str           — sha256(raw_text)[:16]
  raw_text    : str           — normalised full text
  metadata    : dict          — frontmatter key-value pairs
  line_count  : int
  char_count  : int

Chunk
  source_path  : str
  source_id    : str
  chunk_index  : int          — position in document (0-indexed)
  text         : str
  start_char   : int          — offset into raw_text (inclusive)
  end_char     : int          — offset into raw_text (exclusive)

SourceSpan
  start : int                 — character offset in source document
  end   : int                 — exclusive end offset
  text  : str                 — extracted text slice

CandidateMemoryEvent
  event_type        : str     — one of EXTRACTABLE_EVENT_TYPES
  title             : str
  summary           : str
  evidence          : Optional[str]
  source            : str     — source_path
  confidence        : int     — 1–5
  status            : str     — 'proposed' | 'unresolved'
  tags              : List[str]
  created_by        : str     — 'ingestion-pipeline'
  source_span       : SourceSpan
  extraction_method : str     — 'keyword' | 'pattern' | 'heuristic'
  committed_id      : Optional[int]  — set after commit

IngestionResult
  document       : ParsedDocument
  chunks         : List[Chunk]
  candidates     : List[CandidateMemoryEvent]
  committed_ids  : List[int]
```

---

## CLI: `ingest-file`

```
python -m cli.main ingest-file --path PATH [--db memory.db] [--out FILE] [--commit]
```

| Flag | Default | Description |
|---|---|---|
| `--path PATH` | required | Source file to ingest |
| `--db PATH` | `memory.db` | Memory database (used only with `--commit`) |
| `--out PATH` | (stdout) | Write candidate JSON to file |
| `--commit` | off | Write accepted candidates to the memory database |

Without `--commit`, the command is fully read-only: it reads the source file and
writes candidate JSON to stdout or `--out`. No database is touched.

With `--commit`, `commit_candidates()` inserts one row per candidate into
`memory_events` and prints the list of inserted IDs.

---

## Determinism Guarantee

Given the same source file and the same rule registry:

1. `parse_text` produces the same `ParsedDocument` (same `source_id`, same `raw_text`).
2. `chunk_document` produces the same ordered `List[Chunk]` (same offsets, same text).
3. `extract_from_chunks` applies rules in fixed registration order.
4. `extract_candidates` applies confidence floor, deduplication, and sort
   deterministically.
5. The final candidate list is identical across runs.

The only non-deterministic element is `committed_id` (database auto-increment PK),
which is populated only after commit and not part of the candidate equality contract.

---

## What This Is Not

- **Not LLM-based**: no calls to Claude, GPT, or any language model.
- **Not embedding-based**: no vector similarity, no semantic search.
- **Not a background daemon**: triggered by explicit operator CLI invocation only.
- **Not autonomous**: candidates are never committed without `--commit`.
- **Not a trading system**: no strategy signals, no risk decisions, no execution.

---

## Future Extension Paths

**LLM extraction pass**: after rule-based extraction, an optional `--llm-pass` flag
could send each chunk to Claude and merge additional candidates. LLM candidates would
carry `extraction_method='llm'` and lower default confidence (2) pending operator review.

**Embedding-based deduplication**: candidates that are semantically near-duplicates of
existing memory events could be flagged with a `near_duplicate_of` field, reducing
noise in the commit step. Implemented as a cosine similarity threshold — operator sets
the threshold, not the model.

**Batch ingestion**: a future `ingest-dir --path DIR` command walks a directory and
runs the pipeline on every supported file, aggregating candidates into a single review
bundle before commit.

**Incremental ingestion**: track `source_id` in a `ingested_sources` table and skip
re-ingesting unchanged files. Enables safe re-runs against a growing document store.

# Source Document Registry Architecture

## Purpose

The source document registry maintains a persistent, versioned record of every
file that has been ingested or registered for ingestion. It gives the system
a canonical answer to the question: *where did this memory event come from, and
what was the exact state of the source at the time it was ingested?*

Without a source registry, memory events point to file paths that may change,
be deleted, or be replaced. The registry makes source provenance durable and
auditable even after the source file is modified or removed.

---

## Why Source Provenance Matters

Memory events are cognitive lineage. Their value depends on the trustworthiness
of the source they were extracted from. A governance rule derived from an internal
strategy brief has different authority weight than one extracted from an external
article.

The registry captures:

1. **What** was ingested — exact content via SHA-256 checksum.
2. **When** it was ingested — `registered_at` UTC timestamp.
3. **Which version** of the file — monotonically increasing `version` field.
4. **What kind** of source — `source_type` (doctrine, research_note, article, ...).
5. **How trusted** the source is — `authority_tier` (authoritative, high, medium, ...).
6. **Current disposition** — `status` (active, superseded, deprecated, ...).

Memory events link back to sources via `memory_event.source = file_path`. The
registry resolves that path to a checksum, version, and authority context.

---

## Checksum and Versioning Model

### SHA-256 Only

All checksums are computed by reading the raw binary bytes of the file, then
computing the SHA-256 hex digest. This approach:

- Is byte-exact: encoding, line-ending, or metadata changes all produce a different
  checksum.
- Is deterministic: same file content → same checksum, always.
- Is portable: no OS-specific behavior.

### source_id Derivation

```
source_id = sha256( abs_path + '\x00' + checksum_sha256 )[:16]
```

- Deterministic: same (path, content) pair always produces the same source_id.
- Content-sensitive: changes if and only if the file content changes.
- Path-bound: the same bytes at a different path produce a different source_id
  (intentional — they are different provenance lineages).

### Version Lifecycle

```
File first registered (v1, status=active)
        │
File content changes → v2 registered
        │
        ├── v1 record: status → superseded
        └── v2 record: status = active
                │
        File content changes again → v3 registered
                │
                ├── v2 record: status → superseded
                └── v3 record: status = active
```

At any given time, at most one record per path has `status = active`. All prior
versions are preserved with `status = superseded`. The full version history is
always available via `get_sources_by_path()`.

---

## Idempotency Contract

| Condition | Result |
|---|---|
| Same path + same checksum | Return existing active record (no write) |
| Same path + different checksum | Supersede active record; create new version |
| New path | Create version 1 record |

Registering the same unchanged file is always safe to call multiple times —
it produces no new writes and returns the same `source_id`.

---

## Data Model

```
SourceDocument
  source_id       : str   — sha256(abs_path + NUL + checksum)[:16]
  path            : str   — absolute filesystem path at registration time
  filename        : str   — basename of path
  checksum_sha256 : str   — 64-char hex SHA-256 of raw file bytes
  size_bytes      : int   — file size in bytes at registration time
  modified_time   : str   — file mtime as UTC ISO-8601
  registered_at   : str   — wall-clock UTC time of registration
  source_type     : str   — one of VALID_SOURCE_TYPES
  authority_tier  : str   — one of VALID_AUTHORITY_TIERS
  status          : str   — one of VALID_SOURCE_STATUSES
  metadata        : dict  — arbitrary key-value pairs
  version         : int   — 1 on first registration; increments on content change
```

### Approved source_type Values

| Value | Meaning |
|---|---|
| `doctrine` | Internal governance or strategy doctrine |
| `research_note` | Analyst or quant research notes |
| `article` | External article or publication |
| `transcript` | Meeting or call transcript |
| `implementation_brief` | Engineering implementation spec |
| `architecture_doc` | Architecture decision record or design doc |
| `external_reference` | External reference material |
| `unknown` | Default when type is not specified |

### Approved authority_tier Values

| Value | Meaning |
|---|---|
| `authoritative` | Source is the governing authority (internal doctrine) |
| `high` | High-confidence internal research |
| `medium` | Standard external source |
| `low` | Low-confidence or unreviewed source |
| `unknown` | Default when tier is not specified |

### Approved status Values

| Value | Meaning |
|---|---|
| `active` | Current version; the latest registered state |
| `superseded` | Previous version, replaced by a newer content version |
| `deprecated` | Source no longer considered relevant; may be replaced |
| `rejected` | Source was reviewed and rejected from use |
| `archived` | Source retained for audit but no longer active |

---

## Database Layout

Source documents live in the operator's memory SQLite database in the
`source_documents` table. No separate database file is required.

```
source_documents
  id              INTEGER PRIMARY KEY
  source_id       TEXT UNIQUE              ← lookup by content version
  path            TEXT                     ← lookup by file path
  filename        TEXT
  checksum_sha256 TEXT                     ← lookup by content hash
  size_bytes      INTEGER
  modified_time   TEXT
  registered_at   TEXT
  source_type     TEXT CHECK(...)
  authority_tier  TEXT CHECK(...)
  status          TEXT CHECK(...) DEFAULT 'active'
  metadata_json   TEXT DEFAULT '{}'
  version         INTEGER DEFAULT 1

Indexes:
  UNIQUE (path, checksum_sha256)    ← idempotency enforcement
  INDEX (path)                      ← version history lookup
  INDEX (checksum_sha256)           ← same-content cross-path lookup
  INDEX (status)                    ← active-only filtering
  INDEX (source_type)               ← type filtering
```

The `UNIQUE (path, checksum_sha256)` index is the primary idempotency guard at
the database level. Even if the application layer fails to detect the duplicate,
the DB constraint prevents a double-insert.

---

## Relationship to memory_events

```
source_documents
  source_id : "451a795e4f8827f4"
  path      : "/path/to/research.md"
  checksum  : "518c0c6f..."
        │
        │ (implicit join: memory_event.source == source.path)
        │
memory_events
  source    : "/path/to/research.md"
  title     : "ADR: Use SQLite with WAL mode"
  event_type: "architecture_decision"
```

There is no foreign key between `source_documents` and `memory_events`. The link
is an implicit path join, which:

- Keeps the two tables decoupled (memory schema changes don't cascade to sources).
- Allows a memory event to exist before or after a source is registered.
- Enables multiple memory events from the same source to all point to the same
  path without redundant FK entries.

The `ingest-file` command always registers the source before extracting candidates,
so the source record is guaranteed to exist at the moment of candidate commit.

---

## ingestion Integration

```
ingest-file --path FILE --db memory.db [--commit] [--source-type TYPE] [--authority-tier TIER]
                │
                ▼
        sources.registry.register_source()  ← provenance written first
                │
                ▼
        ingestion.parser.parse_file()
                │
                ▼
        ingestion.chunker.chunk_document()
                │
                ▼
        ingestion.candidates.extract_candidates()
                │
                ├── default: print JSON (includes source_registry block)
                │
                └── --commit: memory.service.add_memory_event() × N
```

Source registration happens before parsing. If the file does not exist or has an
invalid extension, the command fails before reaching the registry. If source
registration fails (e.g., invalid `source_type`), no candidates are extracted
and no memory events are written.

The JSON output of `ingest-file` includes a `source_registry` block containing
the full `SourceDocument` record. This makes the provenance of every candidate
visible in the output without a separate query.

---

## CLI Reference

### `sources-register`

```
python -m cli.main sources-register --path PATH [--db memory.db]
    [--source-type TYPE] [--authority-tier TIER]
```

Registers a file in the source document registry. Idempotent. Prints the
`SourceDocument` record as JSON.

### `sources-list`

```
python -m cli.main sources-list [--db memory.db]
    [--status STATUS] [--source-type TYPE]
```

Lists registered source documents, ordered by `registered_at DESC`. Optionally
filtered by `status` and/or `source_type`.

### `sources-show`

```
python -m cli.main sources-show --source-id ID [--db memory.db]
```

Shows a single source document record by `source_id`.

---

## No-Mutation Guarantee

`get_source_by_id`, `get_sources_by_path`, `get_sources_by_checksum`, and
`list_sources` are all read-only — they open the database and SELECT only.

`register_source` is the only write function. It writes at most two rows per
call: one UPDATE (supersede) and one INSERT. It uses `PRAGMA journal_mode=WAL`
and executes within a context-managed connection (implicit commit on success,
rollback on exception).

---

## Future Extension Paths

**PDF and binary file support:**  
Currently only text files are parsed by the ingestion pipeline. The registry
already supports any file type via binary checksum. A future `pdf-ingest` command
would extract text from PDF, register the PDF as a source, and run the same
extraction pipeline on the extracted text.

**Web capture provenance:**  
A future `web-capture` source type (URL, capture timestamp, rendered HTML hash)
would extend the registry to track externally fetched content. The checksum
model still applies: same rendered HTML → same checksum.

**Authority-weighted activation:**  
The `authority_tier` on each source can be used to weight memory event activation
scores during session reconstruction. A `doctrine` source at `authoritative` tier
would receive a higher activation score than an `external_reference` at `low` tier.
This is an additive signal to the existing `confidence` field, not a replacement.

**Local model extraction tier:**  
A future `extraction_method='local_model'` rule class would run a quantized LLM
locally to extract candidates. Source registration still happens first; the model
extraction step is an additional candidate producer alongside the existing
keyword/pattern/heuristic rules.

**Ingestion fingerprint:**  
A future `ingestion_runs` table would record each `ingest-file` invocation:
which source was processed, how many candidates were extracted, how many were
committed, and the `source_id` at time of ingestion. This enables re-ingestion
detection: "has this exact version of this file already been ingested?"

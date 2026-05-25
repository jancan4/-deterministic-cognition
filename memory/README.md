# External Memory Layer

Deterministic, SQLite-backed institutional memory for the FX systems project.

> **Extended documentation:**  
> This README covers the original memory layer (schema v1 through v3: `memory_events`, `memory_revisions`, `memory_links`). The full CLI reference including sessions, compression, ontology, activation policy, and governance verification commands is in `docs/CLI_REFERENCE.md`. Schema history through v16 is in `docs/SCHEMA_HISTORY.md`. Operator workflows are in `docs/OPERATOR_GUIDE.md`.

---

## Architecture

```
memory/
  schema.sql     — DDL: memory_events, memory_links, memory_revisions
  models.py      — Dataclasses: MemoryEvent, MemoryRevision, MemoryLink + enum constants
  service.py     — All database operations; enforces all validation
  export.py      — Deterministic JSON export to file
  cli.py         — argparse CLI; delegates to service; no validation bypass
  tests/
    test_service.py   — 100+ hermetic service-layer tests
    test_export.py    — Export determinism and structure tests
    test_cli.py       — CLI integration tests
```

The service layer is the single point of truth for validation. The CLI delegates to it without relaxing any constraint. Direct programmatic use of `service.py` enforces the same rules.

---

## Schema

### memory_events

The canonical memory table. Every structured piece of institutional knowledge is a row here.

| Column | Type | Constraint |
|---|---|---|
| id | INTEGER | PRIMARY KEY AUTOINCREMENT |
| event_type | TEXT | NOT NULL, approved values only |
| title | TEXT | NOT NULL, non-empty |
| summary | TEXT | NOT NULL, non-empty |
| evidence | TEXT | nullable |
| source | TEXT | NOT NULL, non-empty |
| confidence | INTEGER | NOT NULL, 1–5 |
| status | TEXT | NOT NULL, approved values only |
| tags_json | TEXT | NOT NULL, JSON array |
| related_ids_json | TEXT | NOT NULL, JSON array of integers |
| created_by | TEXT | NOT NULL, non-empty |
| created_at | TEXT | NOT NULL, ISO-8601 UTC |
| updated_at | TEXT | NOT NULL, ISO-8601 UTC |
| version | INTEGER | NOT NULL, starts at 1 |

### memory_revisions

Immutable audit trail. Written on every `update-status` call. Never deleted.

| Column | Type | Notes |
|---|---|---|
| id | INTEGER | PRIMARY KEY AUTOINCREMENT |
| memory_id | INTEGER | FK → memory_events(id) |
| old_value_json | TEXT | JSON object of changed fields before update |
| new_value_json | TEXT | JSON object of changed fields after update |
| reason | TEXT | NOT NULL, required |
| created_at | TEXT | ISO-8601 UTC |
| created_by | TEXT | NOT NULL |

### memory_links

Directed relationships between memory events.

| Column | Type | Notes |
|---|---|---|
| id | INTEGER | PRIMARY KEY AUTOINCREMENT |
| source_id | INTEGER | FK → memory_events(id) |
| target_id | INTEGER | FK → memory_events(id) |
| relationship | TEXT | approved values only |
| created_at | TEXT | ISO-8601 UTC |

Uniqueness: `(source_id, target_id, relationship)` — the same pair can have multiple link types.

---

## Approved Values

### event_type

```
architecture_decision   governance_rule         hypothesis
experiment              validation_result       adaptation
regime_observation      implementation_note     open_question
rejected_idea           incident                source_reference
```

### status

```
proposed    accepted    rejected    superseded
active      archived    unresolved  deprecated
```

### confidence (integer 1–5)

| Value | Meaning |
|---|---|
| 1 | Weak / speculative |
| 2 | Plausible |
| 3 | Supported |
| 4 | Strongly supported |
| 5 | Authoritative / governance-backed |

### relationship (memory_links)

```
supports    contradicts    supersedes    refines
derived_from    related_to    blocks    depends_on
```

---

## Validation Rules

All rules are enforced in `service.py` before any write reaches SQLite.

- `event_type` must be one of the 12 approved values — rejected otherwise
- `status` must be one of the 8 approved values — rejected otherwise
- `confidence` must be a Python `int` in range 1–5 — strings (`'high'`, `'3'`), floats, booleans all rejected
- `title`, `summary`, `source`, `created_by` must be non-empty strings
- `relationship` in `memory_links` must be one of the 8 approved values
- Duplicate links `(source_id, target_id, relationship)` are rejected
- `reason` in `update-status` is required and must be non-empty
- Foreign key constraints are enforced at the SQLite level (`PRAGMA foreign_keys=ON`)

---

## CLI Usage

All commands require `--db PATH`. The path is explicit on every call — no implicit global state.

### init

Create the database and schema.

```bash
python3 -m memory.cli init --db memory.db
```

### add

Add a memory event.

```bash
python3 -m memory.cli add \
  --db memory.db \
  --type architecture_decision \
  --title "External memory uses event lineage" \
  --summary "Memory stores structured governed events, not raw chat." \
  --source "EXTERNAL_MEMORY_LAYER_IMPLEMENTATION_BRIEF.md" \
  --confidence 5 \
  --status accepted \
  --created-by "user+gpt" \
  --tags memory,event-sourcing,replayability \
  --evidence "Validated in session 2026-05-21"
```

Required: `--type`, `--title`, `--summary`, `--source`, `--confidence`, `--status`, `--created-by`  
Optional: `--evidence`, `--tags` (comma-separated), `--related-ids` (comma-separated integers)

### list

List events with optional filters.

```bash
python3 -m memory.cli list --db memory.db
python3 -m memory.cli list --db memory.db --type open_question
python3 -m memory.cli list --db memory.db --status unresolved
python3 -m memory.cli list --db memory.db --tag governance
```

### search

Full-text search across title, summary, evidence, source, and tags. At least one of `--query` or `--tag` is required.

```bash
python3 -m memory.cli search --db memory.db --query replayability
python3 -m memory.cli search --db memory.db --tag governance
python3 -m memory.cli search --db memory.db --query regime --tag fx
```

### show

Show one event with its full revision history and links.

```bash
python3 -m memory.cli show --db memory.db --id 1
```

### update-status

Update the status of an event. Always writes a `memory_revisions` record. Always increments `version`.

```bash
python3 -m memory.cli update-status \
  --db memory.db \
  --id 1 \
  --status superseded \
  --reason "Replaced by newer architecture decision" \
  --created-by "user"
```

Required: `--id`, `--status`, `--reason`, `--created-by`

### link

Create a directed relationship between two events.

```bash
python3 -m memory.cli link \
  --db memory.db \
  --source-id 1 \
  --target-id 2 \
  --relationship supports
```

### export

Export a deterministic JSON snapshot of all three tables to a file.

```bash
python3 -m memory.cli export --db memory.db --out memory_export.json
```

### review

Show events needing attention. Default shows `proposed`, `unresolved`, `active`. Optional filters narrow the result.

```bash
python3 -m memory.cli review --db memory.db
python3 -m memory.cli review --db memory.db --status proposed
python3 -m memory.cli review --db memory.db --type open_question
```

---

## Deterministic Guarantees

- All timestamps are UTC ISO-8601 (`YYYY-MM-DDTHH:MM:SSZ`)
- Export ordering: `memory_events`, `memory_revisions`, `memory_links` each ordered by `id` ascending
- JSON export uses `sort_keys=True` — key order is alphabetical and stable across Python versions
- Tags are stored sorted; `related_ids` are stored sorted
- No random IDs, no UUIDs, no hash-based ordering
- No network calls anywhere in the stack
- No background workers or implicit state
- `init_db` is idempotent (`CREATE TABLE IF NOT EXISTS`)
- Re-exporting the same database always produces byte-identical output (modulo the `schema_version` field)

---

## Governance Constraints

This layer stores institutional context. It does not authorize action.

**May store:**
- Architecture decisions
- Governance rules
- Research hypotheses and validation results
- Adaptation rationale
- Regime observations
- Implementation notes
- Open questions, rejected ideas, incidents
- Source references

**Must never:**
- Authorize live deployment or leverage changes
- Override uploaded doctrine documents
- Override risk-engine vetoes
- Treat hypotheses as validated facts
- Create or trigger trading signals
- Mutate strategies automatically
- Become opaque hidden memory

Retrieved memory is institutional context, not ground truth. It is subordinate to uploaded doctrine, governance policy, and human approval requirements.

---

## Running Tests

```bash
python3 -m venv .venv
.venv/bin/pip install pytest
.venv/bin/python3 -m pytest memory/tests/ -v
```

All 170 tests are hermetic (use `tmp_path`), require no network, and leave no persistent state.

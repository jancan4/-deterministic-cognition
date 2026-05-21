# EXTERNAL_MEMORY_LAYER_IMPLEMENTATION_BRIEF.md

## Purpose

Implement a deterministic external memory layer for the FX systems project.

The GPT remains the reasoning engine.

The external memory layer becomes persistent cognition infrastructure.

This is not a chatbot memory system.

This is a governed institutional memory system for:

- architecture decisions
- governance rules
- hypotheses
- experiments
- validation results
- adaptation rationale
- regime observations
- implementation notes
- open questions
- rejected ideas
- incidents
- source references

The memory layer must be:

- structured
- replayable
- auditable
- queryable
- versioned
- deterministic
- evidence-aware
- governance-compliant

Memory is not truth.

Memory is evidence-bearing institutional context.

Retrieved memory must never override:

- uploaded doctrine documents
- governance policy
- risk-engine vetoes
- validation evidence
- current system state
- human approval requirements

---

# Implementation Scope

Implement Milestone 1 only.

Technology:

- Python
- SQLite
- CLI
- deterministic JSON export
- hermetic tests
- no network dependency

Do not implement:

- vector search
- embeddings
- autonomous agents
- broker integration
- trading execution
- background services
- cloud services
- hidden telemetry
- non-deterministic retrieval

---

# Required Repository Structure

Create or replace:

```text
memory/
  __init__.py
  schema.sql
  models.py
  service.py
  cli.py
  export.py
  tests/
    __init__.py
    test_service.py
    test_export.py
    test_cli.py
```

Use `python3`, not `python`.

Do not install packages globally.

If tests require pytest, use an existing project environment or create a local virtual environment only after explicit approval.

---

# Canonical Database Tables

The required table names are:

- memory_events
- memory_links
- memory_revisions

Do not use alternative names such as `memories`.

---

# memory_events Table

This is the canonical memory table.

Required fields:

```sql
id INTEGER PRIMARY KEY AUTOINCREMENT,
event_type TEXT NOT NULL,
title TEXT NOT NULL,
summary TEXT NOT NULL,
evidence TEXT,
source TEXT NOT NULL,
confidence INTEGER NOT NULL,
status TEXT NOT NULL,
tags_json TEXT NOT NULL DEFAULT '[]',
related_ids_json TEXT NOT NULL DEFAULT '[]',
created_by TEXT NOT NULL,
created_at TEXT NOT NULL,
updated_at TEXT NOT NULL,
version INTEGER NOT NULL DEFAULT 1
```

Validation:

- event_type must be one of the approved event types
- status must be one of the approved statuses
- confidence must be integer 1 through 5
- title must not be empty
- summary must not be empty
- source must not be empty
- created_by must not be empty
- tags_json must serialize a JSON list
- related_ids_json must serialize a JSON list

---

# memory_links Table

Stores explicit relationships between memory events.

Required fields:

```sql
id INTEGER PRIMARY KEY AUTOINCREMENT,
source_id INTEGER NOT NULL,
target_id INTEGER NOT NULL,
relationship TEXT NOT NULL,
created_at TEXT NOT NULL,
FOREIGN KEY(source_id) REFERENCES memory_events(id),
FOREIGN KEY(target_id) REFERENCES memory_events(id)
```

Allowed relationship values:

```text
supports
contradicts
supersedes
refines
derived_from
related_to
blocks
depends_on
```

---

# memory_revisions Table

Tracks changes to memory events.

Required fields:

```sql
id INTEGER PRIMARY KEY AUTOINCREMENT,
memory_id INTEGER NOT NULL,
old_value_json TEXT NOT NULL,
new_value_json TEXT NOT NULL,
reason TEXT NOT NULL,
created_at TEXT NOT NULL,
created_by TEXT NOT NULL,
FOREIGN KEY(memory_id) REFERENCES memory_events(id)
```

Status changes must create a memory_revisions record.

Any future update command must create a memory_revisions record.

---

# Approved event_type Values

Use exactly these values:

```text
architecture_decision
governance_rule
hypothesis
experiment
validation_result
adaptation
regime_observation
implementation_note
open_question
rejected_idea
incident
source_reference
```

No other event_type values are valid.

Reject invalid event types.

---

# Approved status Values

Use exactly these values:

```text
proposed
accepted
rejected
superseded
active
archived
unresolved
deprecated
```

No other status values are valid.

Reject invalid statuses.

---

# Confidence Model

Confidence must be an integer from 1 to 5.

Use exactly this scale:

```text
1 = weak / speculative
2 = plausible
3 = supported
4 = strongly supported
5 = authoritative / governance-backed
```

Reject confidence values outside 1-5.

Do not use string confidence values such as high, medium, low, or uncertain.

---

# Required CLI Commands

Implement these commands:

```bash
python3 -m memory.cli init
python3 -m memory.cli add
python3 -m memory.cli list
python3 -m memory.cli search
python3 -m memory.cli show
python3 -m memory.cli update-status
python3 -m memory.cli link
python3 -m memory.cli export
python3 -m memory.cli review
```

All commands must accept:

```bash
--db PATH
```

No command may rely on implicit global database state.

---

# CLI: init

Create the SQLite database using schema.sql.

Example:

```bash
python3 -m memory.cli init --db memory.db
```

---

# CLI: add

Add a memory event.

Required arguments:

```bash
--db
--type
--title
--summary
--source
--confidence
--status
--created-by
```

Optional arguments:

```bash
--evidence
--tags
--related-ids
```

Tags should accept comma-separated values and store them as JSON list.

Example:

```bash
python3 -m memory.cli add \
  --db memory.db \
  --type architecture_decision \
  --title "External memory uses event lineage" \
  --summary "The memory layer stores structured memory_events rather than raw chat logs." \
  --source "EXTERNAL_MEMORY_LAYER_IMPLEMENTATION_BRIEF.md" \
  --confidence 5 \
  --status accepted \
  --created-by "user+gpt" \
  --tags memory,event-sourcing,replayability
```

---

# CLI: list

List memory events.

Optional filters:

```bash
--type
--status
--tag
```

Examples:

```bash
python3 -m memory.cli list --db memory.db
python3 -m memory.cli list --db memory.db --type open_question
python3 -m memory.cli list --db memory.db --status unresolved
python3 -m memory.cli list --db memory.db --tag adaptation
```

---

# CLI: search

Search title, summary, evidence, source, and tags.

Arguments:

```bash
--query
--tag
```

At least one of --query or --tag is required.

Examples:

```bash
python3 -m memory.cli search --db memory.db --query replayability
python3 -m memory.cli search --db memory.db --tag governance
```

---

# CLI: show

Show one memory event by id.

Example:

```bash
python3 -m memory.cli show --db memory.db --id 1
```

Output should include:

- event fields
- linked memory items if available
- revision count if available

---

# CLI: update-status

Update memory event status.

Required arguments:

```bash
--db
--id
--status
--reason
--created-by
```

This command must:

- validate the new status
- update the memory_events row
- increment version
- update updated_at
- insert a memory_revisions record

Example:

```bash
python3 -m memory.cli update-status \
  --db memory.db \
  --id 1 \
  --status superseded \
  --reason "Replaced by newer architecture decision" \
  --created-by "user"
```

---

# CLI: link

Create a relationship between two memory events.

Required arguments:

```bash
--db
--source-id
--target-id
--relationship
```

Example:

```bash
python3 -m memory.cli link \
  --db memory.db \
  --source-id 1 \
  --target-id 2 \
  --relationship supports
```

---

# CLI: export

Export deterministic JSON.

Required arguments:

```bash
--db
--out
```

Export must include:

- memory_events
- memory_links
- memory_revisions

Export ordering must be deterministic:

- memory_events ordered by id
- memory_links ordered by id
- memory_revisions ordered by id
- JSON keys sorted

Example:

```bash
python3 -m memory.cli export --db memory.db --out memory_export.json
```

---

# CLI: review

Show memory events needing review.

Default review should include statuses:

- proposed
- unresolved
- active

Optional filters:

```bash
--status
--type
```

Example:

```bash
python3 -m memory.cli review --db memory.db
```

---

# Determinism Requirements

The implementation must satisfy:

- UTC timestamps only
- ISO-8601 timestamp strings
- deterministic export ordering
- no network calls
- no hidden global state
- no background workers
- no implicit database path
- no nondeterministic random IDs
- no autonomous memory creation
- all writes explicit through service or CLI
- all status changes revisioned

---

# Service Layer Requirements

Implement service functions for:

- init_db(db_path)
- add_memory_event(...)
- list_memory_events(...)
- search_memory_events(...)
- get_memory_event(...)
- update_status(...)
- link_memory_events(...)
- export_memory(...)
- review_memory(...)

Service layer must enforce validation.

CLI must not bypass validation.

---

# Test Requirements

Implement hermetic tests for:

- database initialization
- schema table creation
- adding valid memory events
- rejecting invalid event_type
- rejecting invalid status
- rejecting confidence below 1
- rejecting confidence above 5
- rejecting string confidence values
- required field validation
- tag JSON serialization
- related_ids JSON serialization
- list by type
- list by status
- search by query
- search by tag
- show by id
- update-status creates revision
- update-status increments version
- link creates memory_links row
- invalid relationship rejected
- deterministic export ordering
- export includes all three tables

Tests must not require network access.

---

# Governance Constraints

This memory layer may store:

- architecture decisions
- governance rules
- research hypotheses
- validation outcomes
- adaptation rationale
- unresolved questions
- rejected ideas
- implementation notes
- regime observations
- source references
- incidents

This memory layer may not:

- authorize live deployment
- approve leverage changes
- override uploaded doctrine
- override risk-engine vetoes
- treat hypotheses as facts
- create trading signals
- trigger execution
- mutate strategies automatically
- become opaque hidden memory

---

# First Acceptance Check

After implementation, these commands must work:

```bash
python3 -m memory.cli init --db memory.db

python3 -m memory.cli add \
  --db memory.db \
  --type architecture_decision \
  --title "External memory uses event lineage" \
  --summary "Memory stores structured governed events, not raw chat." \
  --source "EXTERNAL_MEMORY_LAYER_IMPLEMENTATION_BRIEF.md" \
  --confidence 5 \
  --status accepted \
  --created-by "user+gpt" \
  --tags memory,event-sourcing,replayability

python3 -m memory.cli search --db memory.db --query replayability

python3 -m memory.cli update-status \
  --db memory.db \
  --id 1 \
  --status superseded \
  --reason "Testing revision tracking" \
  --created-by "user"

python3 -m memory.cli export --db memory.db --out memory_export.json
```

Expected result:

- memory.db exists
- one memory_events row exists
- one memory_revisions row exists after update-status
- export file exists
- export is deterministic
- no network access required

---

# Final Principle

The external memory layer is persistent institutional cognition infrastructure.

It is not recall.

It is governed state.

It must remain simple, inspectable, deterministic, and subordinate to governance.

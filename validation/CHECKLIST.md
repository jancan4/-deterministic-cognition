# Real-Corpus Validation Checklist

Substrate baseline: commit 5678f85  
Schema: memory v16, workflow v3, bundle v1.2  
Suite baseline: 3150 tests passing  

This checklist governs the first real-world cognition workload validation run.
It is not a demo script. It is a structured operator procedure with explicit
pass/fail checkpoints at each stage.

Complete each section in order. Record results in the run log (see Artifact Layout).
Do not advance to the next section if a BLOCKING checkpoint fails.

---

## 0. Pre-flight

Before touching any real corpus:

- [ ] Confirm baseline suite still passes: `python -m pytest -q`
  - Expected: 3150 passed, 0 failed
  - BLOCKING if any failures
- [ ] Confirm substrate version: `git log --oneline -1`
  - Expected: `5678f85` or a non-breaking descendant
- [ ] Confirm no pending schema migrations are queued
- [ ] Create a fresh validation DB (do not reuse demo DB):
  ```
  memory-cli init --db validation/runs/corpus_v1.db
  ```
- [ ] Record DB path, git hash, Python version, and operator name in the run log

---

## 1. Corpus Preparation

### Size guidance

| Corpus size | Documents | Expected events | Validation duration |
|---|---|---|---|
| Minimum viable | 10–20 docs | 30–80 proposed | 1 operator-hour |
| Recommended | 25–50 docs | 80–200 proposed | 2–4 operator-hours |
| Upper bound (v1) | 100 docs | 300–600 proposed | Full day |

Start with the **minimum viable** corpus for the first validation run. Scale only
after sign-off criteria pass at that size.

### Document requirements

- All documents must exist as plain text or markdown files on disk
- Each document must have a distinct, meaningful filename
- Mix of content types is preferred: notes, summaries, decisions, references
- Documents must be operator-readable (no binary, no generated garbage)
- At least 3 documents must produce contradictable claims (to test contradiction surfacing)

### Corpus staging

- [ ] Place all documents in a single flat directory: `validation/corpus/`
  - `validation/corpus/` is `.gitignore`d — do not commit source documents
- [ ] List all documents in the run log with filename and expected content type
- [ ] Identify which documents are expected to produce high-confidence events
- [ ] Identify which documents are expected to produce proposed-only events
- [ ] Identify at least one pair of documents expected to produce a contradiction

---

## 2. Ingestion

### Batch sizing

Ingest in batches of 5–10 documents. Review after each batch before ingesting the next.
Do not ingest the entire corpus at once — partial review enables early error detection.

### Ingestion procedure

For each batch:

```
memory-shell --db validation/runs/corpus_v1.db
```

For each document in the batch:
```
ingest validation/corpus/FILENAME.md [--source-type TYPE] [--authority-tier TIER]
review --status proposed
```

After each batch:
- [ ] Record: document name, source type, authority tier, candidate count
- [ ] Scan proposed events: types correct? titles meaningful? confidence plausible?
- [ ] Flag any candidates that appear duplicated, malformed, or wrong type
- [ ] Do not approve yet — complete ingestion of the full batch first

### Ingestion checkpoints

After all batches are complete:

- [ ] **CP-I1**: Total proposed event count is within expected range for corpus size
- [ ] **CP-I2**: No event has `event_type = 'unknown'` unless the source document
  was genuinely unclassifiable
- [ ] **CP-I3**: No two events have identical `title` and `summary` (exact duplicate check)
  ```sql
  SELECT title, summary, COUNT(*) FROM memory_events
  GROUP BY title, summary HAVING COUNT(*) > 1;
  ```
- [ ] **CP-I4**: All ingestion runs recorded:
  ```
  substrate-cli ingestion-runs --db validation/runs/corpus_v1.db
  ```

BLOCKING if CP-I1 fails by more than 50% (expected count vs actual).
BLOCKING if CP-I3 finds exact duplicates — investigate before proceeding.

---

## 3. Operator Review

See `TEMPLATES.md` for the per-event review form.

### Review procedure

```
review --status proposed --limit 100
```

For each proposed event:
- [ ] Read title and summary — are they factually grounded in the source document?
- [ ] Is the confidence score appropriate? (1 = speculation, 5 = verified)
- [ ] Is the event_type correct?
- [ ] Are tags meaningful and not spurious?
- [ ] Approve if sound: `approve ID --status active`
- [ ] Reject if unsound: `approve ID --status rejected`

Target: review **100%** of proposed events before sign-off. Do not leave any
event in `proposed` status at validation close.

### Review checkpoints

- [ ] **CP-R1**: All proposed events reviewed (none remaining in proposed status)
- [ ] **CP-R2**: Rejection rate recorded: `rejected / (active + rejected)`
  - Expected range: 5–30% for a well-curated corpus
  - > 50% rejection rate: BLOCKING — corpus quality or ingestion logic issue
- [ ] **CP-R3**: At least one event in each of governance_context, unresolved_items,
  and relevant_memory sections (verified via `assembly show` after session start)

---

## 4. Policy and Session

```
policy create --name "corpus_validation_v1" --trigger-class operator_request
policy activate ID
session start
```

- [ ] **CP-S1**: Session opens without error
- [ ] **CP-S2**: `assembly show` returns sections with events
  - Expected: governance_context, unresolved_items, or relevant_memory non-empty
- [ ] **CP-S3**: `status` shows `assemblies: 1+`
- [ ] **CP-S4**: `session timeline` shows at least one transition row

---

## 5. Governance Checks

```
governance
```

- [ ] **CP-G1**: Zero CRITICAL issues
  - BLOCKING if any CRITICAL issues exist — investigate before advancing
- [ ] **CP-G2**: WARNING count recorded (informational, not blocking)
- [ ] **CP-G3**: Run `governance show stale_proposed` — no events stale > 7 days
  without operator action
- [ ] **CP-G4**: Run `lineage` — zero FK violations
  - BLOCKING if any violations

---

## 6. Contradiction Surfacing

Prerequisite: at least one document pair with contradicting claims was identified
in corpus preparation (Section 1).

After approving events from both documents:

- [ ] **CP-C1**: `assembly show` lists at least one event pair with `contradiction_ids`
  populated, OR the governance report lists a contradiction warning
- [ ] **CP-C2**: If no contradiction surfaced: confirm in the DB manually:
  ```sql
  SELECT COUNT(*) FROM memory_links WHERE relationship = 'contradicts'
    AND status = 'active';
  ```
  If zero rows and contradictions were expected: BLOCKING — investigate extractor logic

---

## 7. Replay Verification

Export the corpus state, import into a fresh DB, verify integrity.

```
export --out validation/exports/corpus_v1_bundle.json
memory-cli init --db validation/runs/corpus_v1_recovered.db
import validation/exports/corpus_v1_bundle.json --db validation/runs/corpus_v1_recovered.db --dry-run
import validation/exports/corpus_v1_bundle.json --db validation/runs/corpus_v1_recovered.db
```

- [ ] **CP-X1**: Dry-run shows zero collisions
- [ ] **CP-X2**: Live import completes with zero collisions
- [ ] **CP-X3**: Event count matches between source and recovered DB:
  ```sql
  -- Run against both DBs:
  SELECT status, COUNT(*) FROM memory_events GROUP BY status;
  ```
- [ ] **CP-X4**: `lineage` passes on recovered DB
- [ ] **CP-X5**: `assembly show` on recovered DB returns the same event IDs as source
  (run `session start` in recovered DB first to generate a new assembly)

BLOCKING if CP-X1 or CP-X2 fail. CP-X5 may show minor divergence if DB state
changed between export and re-assembly — record but do not block on minor divergence.

---

## 8. Transcript and Audit Trail

```
transcript --out validation/transcripts/corpus_v1_session_N.txt
```

- [ ] **CP-T1**: Transcript file is written without error
- [ ] **CP-T2**: Transcript header contains the disclaimer line:
  `"This transcript is a human-readable audit artifact, not a replay/import format."`
- [ ] **CP-T3**: All assemblies from `session timeline` appear in the transcript
- [ ] **CP-T4**: No raw event `evidence` text visible in the transcript (truncation working)

---

## 9. Rollback / Recovery Procedure

If a BLOCKING checkpoint fails at any stage:

1. **Stop** — do not advance to next section
2. **Record** the failing checkpoint ID, observed vs expected values, and DB state
3. **Do not modify** the source DB — export it first for forensics:
   ```
   export --out validation/runs/forensic_TIMESTAMP.json
   ```
4. **Classify** the failure (see `SIGN_OFF.md` failure classes)
5. **Decide**: is this a corpus issue, an ingestion logic bug, or a substrate bug?
   - Corpus issue → fix corpus, re-ingest from scratch with a new DB
   - Ingestion logic bug → escalate to engineering, freeze validation until patched
   - Substrate bug → escalate immediately, freeze all validation

The source DB is disposable. The forensic export bundle is the audit record.
Do not attempt to surgically fix a DB mid-validation — restart with a clean DB.

# Quant Sign-Off Criteria, Freeze Discipline, and Risk Register

Substrate baseline: commit 5678f85  
Schema: memory v16, workflow v3, bundle v1.2

---

## 1. Recommended First Workload Profile

**Workload: Engineering Project Memory**

Rationale: smallest operationally meaningful case. Documents are structured,
claims are verifiable, contradictions arise naturally (decisions get revised,
specs change), and scope is bounded to a single project or feature area.

Corpus composition:
- Architecture decision records (ADRs) or design docs: 8–12 docs
- Meeting/standup notes: 6–10 docs
- Incident reports or post-mortems: 3–5 docs
- Reference material (specs, external docs): 4–8 docs
- Total: 20–35 documents, ~50–150 KB combined

Why this workload:
- Events are factual and checkable — operator can judge quality without domain expertise
- Decisions get revised over time → natural contradiction test cases
- Short documents → ingestion completes in one sitting
- Scope is closed → "done" is definable
- No proprietary data risk if docs are from an internal project

Why not financial research: that corpus is larger, requires domain expertise to
evaluate event quality, and is a higher-stakes first run. Validate the substrate
mechanics on engineering notes first.

---

## 2. Artifact Storage Layout

```
validation/
  CHECKLIST.md          # committed — operator procedure
  TEMPLATES.md          # committed — review forms
  SIGN_OFF.md           # committed — criteria, freeze, risks
  corpus/               # .gitignored — source documents (operator-supplied)
  runs/                 # .gitignored — validation DBs and logs
    RUN_YYYYMMDD_OPERATOR.md   # run log (committed after run closes)
    corpus_v1.db               # ignored — SQLite DB
    corpus_v1_recovered.db     # ignored — recovery DB
    forensic_TIMESTAMP.json    # ignored — forensic export on failure
  transcripts/          # .gitignored — transcript files
    corpus_v1_session_N.txt
  exports/              # .gitignored — export bundles
    corpus_v1_bundle.json
```

Run logs (`RUN_YYYYMMDD_OPERATOR.md`) are the only validation artifacts that
should be committed to the repository. They are the permanent audit record.
All other outputs (DBs, bundles, transcripts) are reproducible and ignored.

---

## 3. Pass Thresholds

### Blocking checkpoints (all must pass)

| Checkpoint | Condition |
|---|---|
| CP-I1 | Proposed event count within 50% of expected range |
| CP-I3 | Zero exact duplicate (title + summary) events |
| CP-R2 | Rejection rate ≤ 50% |
| CP-G1 | Zero CRITICAL governance issues |
| CP-G4 | Zero lineage FK violations |
| CP-X1 | Dry-run import shows zero collisions |
| CP-X2 | Live import completes with zero collisions |
| CP-X3 | Event count (by status) matches source and recovered DB |

### Template minimum ratings (all must be ≥ 3/5)

| Template | Minimum | Description |
|---|---|---|
| T1 ingestion | 3/5 | Events are plausible and well-typed |
| T2 retrieval | 3/5 | Assembly selects relevant events |
| T3 assembly | 3/5 | Assembly is coherent and actionable |
| T5 continuity | 3/5 | Export/import cycle preserves operator state |
| T7 lineage | PASS | No FK violations |

T4, T6 are required only if the corpus and operator workflow activate them.
T8 is required; CRITICAL false positive count must be zero for sign-off.

### Overall sign-off condition

All blocking checkpoints PASS
AND all required template ratings ≥ 3/5
AND CRITICAL governance false positive count = 0
AND substrate version unchanged from run start to run close

---

## 4. Failure Classes

Classify each failure before deciding next action.

### Class A — Corpus issue
The documents were poorly structured, out of scope, or did not generate
the expected content types. The substrate behaved correctly.

Action: Fix corpus. Re-ingest with a new DB. No engineering escalation required.

Examples:
- Rejection rate > 50% because documents were too fragmented
- Proposed events all typed 'unknown' because documents were binary exports
- No contradictions because all documents agreed

### Class B — Operator procedure error
The operator ran commands in the wrong order, used incorrect flags, or
misread a checkpoint condition.

Action: Re-run the affected section with the correct procedure. Document the
error in the run log. No engineering escalation required.

Examples:
- `import` run without `--dry-run` first
- `session start` run without an active policy
- Assembly reviewed before all events were approved

### Class C — Ingestion quality defect
The substrate parsed or extracted events incorrectly, but the DB and schema
are intact. No data corruption.

Action: Escalate to engineering. Freeze validation until the defect is understood.
Export a forensic bundle before doing anything else. The defect may be in
`ingestion/parser.py`, `ingestion/extractor.py`, or `ingestion/candidates.py`.

Examples:
- Event titles are truncated artifacts of the parser
- Evidence fields contain raw markup instead of prose
- Confidence scores are uniformly 1 regardless of source certainty

### Class D — Governance or lineage defect
The governance report produces false positives or the lineage check finds
FK violations in a correctly constructed DB.

Action: Escalate immediately. BLOCKING. Export forensic bundle. Do not attempt
to repair the DB or continue validation.

Examples:
- `lineage` finds broken FKs after a normal ingestion run
- `governance` reports CRITICAL on events that are clearly valid
- `assembly show` fails or returns empty on a populated DB

### Class E — Continuity defect
Export or import produces collisions, data loss, or count mismatches on
a DB that has no known issues.

Action: Escalate immediately. BLOCKING. This affects the core continuity
guarantee. Export a forensic bundle from both source and recovered DBs.

Examples:
- Import produces collisions on a DB that was freshly initialized
- Event counts differ between source and recovered DB
- Recovered DB fails lineage check

### Class F — Substrate integrity defect
The test suite fails, schema queries return errors, or the REPL crashes
on valid input.

Action: Escalate immediately. Halt all validation. Do not use the DB.
File a defect report against the frozen baseline commit.

---

## 5. Blocking Governance Conditions

The following conditions ALWAYS block sign-off regardless of template scores:

1. Any CRITICAL governance issue — zero tolerance
2. Any lineage FK violation — zero tolerance
3. Any import collision on a fresh DB — zero tolerance
4. Any CRITICAL governance false positive — zero tolerance (indicates substrate
   miscalibration, not corpus issue)
5. Test suite regression below 3150 — zero tolerance
6. Substrate commit hash changed mid-run — validation is void; restart

---

## 6. Replay Integrity Requirements

Sign-off requires that the following replay properties hold:

**Determinism**: Given the same DB state and the same activation policy,
`session start` must produce the same event IDs in the same sections.
Verify by closing the session, starting a new one with the same policy,
and comparing `assembly show` output.

**Portability**: The exported bundle must import cleanly into a fresh DB
and produce an assembly with the same event IDs.

**Audit trail**: Every write operation must appear in the ingestion run log
or the activation decision log. No event should exist without a traceable
ingestion run.

Verify audit trail:
```sql
-- Find events with no corresponding ingestion run
SELECT id FROM memory_events
WHERE id NOT IN (
  SELECT value FROM ingestion_runs, json_each(committed_memory_ids_json)
);
```
Expected: zero rows. If non-zero: Class C or Class D defect.

---

## 7. Acceptable Operational Limitations

The following limitations are known and do not block sign-off:

- **Search quality**: Substring match only. No stemming, no ranking.
  Acceptable for v1 corpus size.
- **No policy list command**: Operator must query DB directly for policy IDs.
  Acceptable — documented in known limitations.
- **Single-writer SQLite**: Concurrent shell sessions are not tested.
  Acceptable — single-operator validation.
- **Python 3.9 install requires `--ignore-requires-python`**: Documented.
  Production should use Python 3.11+.
- **No automatic contradiction detection**: Contradictions must be
  created by the extractor during ingestion. If the extractor does not
  produce `contradicts` links, the contradiction surfacing checkpoint (CP-C1)
  must be marked N/A and documented.
- **readline behavior on macOS**: Line editing may differ from GNU readline.
  Not a data safety issue.

---

## 8. Freeze Discipline During Validation

### Prohibited during validation

- Schema changes of any kind (additive or otherwise)
- Changes to activation semantics, governance detectors, or replay logic
- Changes to continuity bundle format (export/import)
- Changes to `memory/service.py` validation logic
- New shell commands or changes to existing shell command behavior
- Dependency version changes
- Any commit that would change the suite from 3150 passed

### Allowed during validation (bugfix exception)

A bugfix may be applied during validation ONLY if:
1. The failing checkpoint is unambiguously Class C–F (substrate defect, not corpus or operator error)
2. The fix touches ONLY the defective code path — no refactoring, no adjacent cleanup
3. The fix passes the full test suite at 3150+
4. The validation run is restarted from the beginning with a fresh DB
5. The new commit hash is recorded in the run log
6. The fix is reviewed by a second operator before proceeding

Bugfixes that pass these conditions do not require a schema freeze exception.

### Schema freeze exceptions

No schema freeze exceptions are permitted during v1 validation. If a schema
change is required to fix a defect, the validation run is void. The schema
change must go through full engineering review, the test suite must be updated,
and validation must restart from scratch.

---

## 9. Risk Register for First Live Corpus Testing

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Corpus documents generate zero events | Medium | High — blocks all validation | Pre-screen corpus with `ingest` on 2–3 docs before committing to full run |
| Rejection rate > 50% | Medium | High — Class A failure | Use well-structured documents; prefer ADRs and meeting notes over freeform text |
| No contradictions detected | Medium | Medium — CP-C1 N/A | Deliberately include two docs with a known conflicting fact |
| Import collision on recovered DB | Low | High — Class E defect | Always dry-run first; keep forensic export |
| Lineage violation after ingestion | Low | High — Class D defect | Run `lineage` after each ingestion batch, not just at the end |
| Governance CRITICAL false positive | Low | High — blocks sign-off | Run governance on demo DB first; confirm no false positives before live run |
| Python 3.9 runtime issue during validation | Low | Medium | Use `--ignore-requires-python` during install; validate with Python 3.11 in CI |
| Operator procedure error invalidating a checkpoint | Medium | Low | Two-operator review of CHECKLIST.md before starting; one validates, one verifies |
| DB corruption from concurrent shell sessions | Low | High | Single operator per validation run; do not share DB across shells |
| Source document contains PII or sensitive data | Medium | Medium | Screen corpus before ingestion; `validation/corpus/` is gitignored |

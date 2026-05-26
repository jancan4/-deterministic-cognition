# Quant Validation Templates

Substrate baseline: commit 5678f85

Lightweight operator forms for each evaluation dimension.
Complete one form per validation run. Record in the run log alongside checkpoint results.

Each template has a RATING field (1–5) and a NOTES field.
Ratings are operator judgements, not computed scores.

Rating scale:
  5 — Excellent: exceeds expectation, no issues
  4 — Good: meets expectation, minor issues noted
  3 — Acceptable: meets minimum bar, issues worth tracking
  2 — Poor: below bar, requires investigation before sign-off
  1 — Failing: blocking issue, validation cannot proceed

---

## T1 — Ingestion Review

Evaluate the quality of proposed events produced from the ingested corpus.

Sample: review at least 20 proposed events, or 100% if total < 30.

```
TEMPLATE T1 — Ingestion Review
Date: _______________    Operator: _______________
DB:   _______________    Corpus size (docs): ___

Total proposed events: ___    Reviewed: ___    Rejected: ___

--- Dimensional ratings ---

Event type accuracy
  Are event_types correctly assigned to the content they represent?
  RATING: ___ / 5
  NOTES:

Title quality
  Are titles specific, grounded in source content, and non-redundant?
  RATING: ___ / 5
  NOTES:

Summary accuracy
  Do summaries faithfully represent the source passage without hallucination?
  RATING: ___ / 5
  NOTES:

Confidence calibration
  Is the confidence score (1–5) consistent with the certainty of the source claim?
  RATING: ___ / 5
  NOTES:

Tag relevance
  Are tags meaningful, correctly typed, and non-spurious?
  RATING: ___ / 5
  NOTES:

Duplication rate
  Are proposed events meaningfully distinct, or are there near-duplicates?
  Duplicate count (exact or near): ___
  RATING: ___ / 5
  NOTES:

--- Aggregate ---
Overall ingestion quality RATING: ___ / 5
Recommendation: [ ] PASS  [ ] PASS WITH NOTES  [ ] FAIL — INVESTIGATE
```

---

## T2 — Retrieval Quality

Evaluate the events selected for the assembled context after session start.

Run `assembly show` after `session start` and review the assembled sections.

```
TEMPLATE T2 — Retrieval Quality
Date: _______________    Operator: _______________
DB:   _______________    Assembly id: ___

Assembly stats:
  entries_accepted: ___    chars_used: ___ / ___

--- Dimensional ratings ---

Section assignment accuracy
  Are governance_context events actually governance-relevant?
  Are unresolved_items actually unresolved?
  Are relevant_memory events meaningfully relevant to the session context?
  RATING: ___ / 5
  NOTES:

Coverage
  Are high-confidence, high-importance events present in the assembly?
  Are there important events you expected to see but are absent?
  Missing events (by ID or description): ___
  RATING: ___ / 5
  NOTES:

Noise / false positives
  Are there low-relevance events present that dilute the assembly?
  Noise count: ___
  RATING: ___ / 5
  NOTES:

Budget utilization
  Is the char budget used efficiently (not wasting space on low-value content)?
  RATING: ___ / 5
  NOTES:

--- Aggregate ---
Overall retrieval quality RATING: ___ / 5
Recommendation: [ ] PASS  [ ] PASS WITH NOTES  [ ] FAIL — INVESTIGATE
```

---

## T3 — Assembly Quality

Evaluate the overall cognitive value of the assembled context as a whole.
This is a holistic judgement: "If I were handed this assembly, would it help me think?"

```
TEMPLATE T3 — Assembly Quality
Date: _______________    Operator: _______________
DB:   _______________    Assembly id: ___

--- Dimensional ratings ---

Coherence
  Does the assembly form a coherent picture of the current state of knowledge?
  Or is it a disconnected list of fragments?
  RATING: ___ / 5
  NOTES:

Actionability
  Does the assembly surface the most operationally relevant issues?
  Would an operator reading this know what to investigate next?
  RATING: ___ / 5
  NOTES:

Completeness
  Does the assembly appear complete relative to what was ingested?
  RATING: ___ / 5
  NOTES:

Determinism
  Run `session start` again (after closing and reopening) — does the assembly
  reproduce the same event IDs? (minor ordering variation acceptable)
  Reproducible: [ ] Yes  [ ] No — divergence noted: ___
  RATING: ___ / 5
  NOTES:

--- Aggregate ---
Overall assembly quality RATING: ___ / 5
Recommendation: [ ] PASS  [ ] PASS WITH NOTES  [ ] FAIL — INVESTIGATE
```

---

## T4 — Contradiction Surfacing

Evaluate whether contradictions between events are correctly detected and surfaced.

```
TEMPLATE T4 — Contradiction Surfacing
Date: _______________    Operator: _______________
DB:   _______________

Known contradictions in corpus: ___
  (List document pairs and expected conflicting claims)

--- Dimensional ratings ---

Detection rate
  How many of the known contradictions were surfaced?
  Detected: ___ / ___
  RATING: ___ / 5
  NOTES:

False positive rate
  Are any events flagged as contradicting each other that do not actually conflict?
  False positive count: ___
  RATING: ___ / 5
  NOTES:

Surfacing in assembly
  Detected contradictions appear in governance_context or via contradiction_ids:
  [ ] Yes, consistently  [ ] Partial  [ ] Not surfaced in assembly
  NOTES:

--- Aggregate ---
Overall contradiction surfacing RATING: ___ / 5
Recommendation: [ ] PASS  [ ] PASS WITH NOTES  [ ] FAIL — INVESTIGATE
```

---

## T5 — Continuity Usefulness

Evaluate whether the export/import cycle preserves meaningful operator continuity.
Run after CP-X checkpoints in CHECKLIST.md.

```
TEMPLATE T5 — Continuity Usefulness
Date: _______________    Operator: _______________
Source DB: _______________    Recovered DB: _______________
Bundle: _______________

--- Dimensional ratings ---

Event fidelity
  Are all approved events present in the recovered DB with correct fields?
  Spot-check 5 events by ID: all present and identical?
  [ ] Yes  [ ] No — discrepancies: ___
  RATING: ___ / 5
  NOTES:

Ingestion run fidelity
  Are ingestion run records present in the recovered DB?
  RATING: ___ / 5
  NOTES:

Assembly reproducibility
  After running `session start` on the recovered DB, does the assembly
  contain the same event IDs as the source DB assembly?
  Match: [ ] Full  [ ] Partial (list divergences)  [ ] None
  RATING: ___ / 5
  NOTES:

Lineage integrity
  `lineage` on recovered DB returns all_ok=True:
  [ ] Yes  [ ] No — violations: ___
  RATING: ___ / 5
  NOTES:

--- Aggregate ---
Overall continuity usefulness RATING: ___ / 5
Recommendation: [ ] PASS  [ ] PASS WITH NOTES  [ ] FAIL — INVESTIGATE
```

---

## T6 — Compression Usefulness

Evaluate whether operator-written compression artifacts add useful cognitive continuity.

Prerequisite: at least one compression artifact created via `compress` during the session.

```
TEMPLATE T6 — Compression Usefulness
Date: _______________    Operator: _______________
DB:   _______________    Artifact id(s): ___

--- Dimensional ratings ---

Artifact relevance
  Does the artifact text faithfully summarize the events it was derived from?
  RATING: ___ / 5
  NOTES:

Continuity value
  If this artifact were included in a future session's context, would it
  provide useful continuity? (i.e., is it better than re-reading the raw events?)
  RATING: ___ / 5
  NOTES:

Operator effort
  Was the compression interface (shell `compress` command) practical to use?
  Could an operator realistically produce quality artifacts under normal conditions?
  RATING: ___ / 5
  NOTES:

--- Aggregate ---
Overall compression usefulness RATING: ___ / 5
Recommendation: [ ] PASS  [ ] PASS WITH NOTES  [ ] SKIP (no artifacts created)
```

---

## T7 — Lineage Integrity

Run after any significant write operation (ingest batch, approve batch, import).

```
TEMPLATE T7 — Lineage Integrity
Date: _______________    Operator: _______________
DB:   _______________    Triggered by: _______________

`lineage` result:
  all_ok: [ ] True  [ ] False
  total_broken: ___

Individual checks:
  (paste output of `lineage` command here)

Governance result:
  CRITICAL count: ___
  WARNING count:  ___

Recommendation: [ ] PASS  [ ] FAIL — BLOCKING
Notes:
```

---

## T8 — Governance Issue Severity

Evaluate whether the governance report issues reflect real operational problems.

```
TEMPLATE T8 — Governance Issue Severity
Date: _______________    Operator: _______________
DB:   _______________

`governance` summary:
  total_events:  ___
  CRITICAL:      ___
  WARNING:       ___
  INFO:          ___

--- For each CRITICAL issue ---
Issue type: ___
memory_id: ___
Rationale: ___
Assessment: [ ] True positive (real problem)  [ ] False positive (expected condition)
Action taken: ___

--- For each WARNING issue (sample up to 5) ---
Issue type: ___
memory_id: ___
Rationale: ___
Assessment: [ ] True positive  [ ] False positive
Action taken: ___

--- Aggregate ---
True positive rate: ___ / ___
CRITICAL false positives: ___  (any false positive is a substrate calibration issue)
Recommendation: [ ] PASS  [ ] PASS WITH NOTES  [ ] FAIL — INVESTIGATE
Notes:
```

---

## Run Log Template

One file per validation run: `validation/runs/RUN_YYYYMMDD_OPERATOR.md`

```
# Validation Run Log

Date:       _______________
Operator:   _______________
Git hash:   _______________
Python:     _______________
DB path:    _______________
Corpus:     ___ documents, ___ total chars (approx)

## Checkpoint Results
(Paste CP-* results here as you complete CHECKLIST.md)

CP-I1: [ ] PASS  [ ] FAIL
CP-I2: [ ] PASS  [ ] FAIL
CP-I3: [ ] PASS  [ ] FAIL
CP-I4: [ ] PASS  [ ] FAIL
CP-R1: [ ] PASS  [ ] FAIL
CP-R2: rejection rate ___  [ ] PASS  [ ] FAIL
CP-R3: [ ] PASS  [ ] FAIL
CP-S1: [ ] PASS  [ ] FAIL
CP-S2: [ ] PASS  [ ] FAIL
CP-S3: [ ] PASS  [ ] FAIL
CP-S4: [ ] PASS  [ ] FAIL
CP-G1: [ ] PASS  [ ] FAIL (CRITICAL count: ___)
CP-G2: WARNING count: ___
CP-G3: [ ] PASS  [ ] FAIL
CP-G4: [ ] PASS  [ ] FAIL
CP-C1: [ ] PASS  [ ] FAIL  [ ] N/A
CP-C2: [ ] PASS  [ ] FAIL  [ ] N/A
CP-X1: [ ] PASS  [ ] FAIL
CP-X2: [ ] PASS  [ ] FAIL
CP-X3: [ ] PASS  [ ] FAIL
CP-X4: [ ] PASS  [ ] FAIL
CP-X5: [ ] PASS  [ ] DIVERGENCE NOTED
CP-T1: [ ] PASS  [ ] FAIL
CP-T2: [ ] PASS  [ ] FAIL
CP-T3: [ ] PASS  [ ] FAIL
CP-T4: [ ] PASS  [ ] FAIL

## Template Ratings
T1 ingestion:        ___ / 5
T2 retrieval:        ___ / 5
T3 assembly:         ___ / 5
T4 contradiction:    ___ / 5  (or N/A)
T5 continuity:       ___ / 5
T6 compression:      ___ / 5  (or N/A)
T7 lineage:          PASS / FAIL
T8 governance:       ___ / 5

## Overall Run Result
[ ] PASS — all blocking checkpoints cleared, all templates >= 3/5
[ ] PASS WITH NOTES — all blocking checkpoints cleared, issues documented
[ ] FAIL — one or more blocking checkpoints failed

## Failure Notes
(If any checkpoint failed, describe observed vs expected and failure class from SIGN_OFF.md)

## Sign-off
Operator signature / initials: _______________
Date: _______________
```

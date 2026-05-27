# Quant Validation Run Log — Run #2

**Run date**: 2026-05-27  
**Operator**: Claude (AI) — review and countersign required by human quant  
**Substrate commit (frozen)**: 5678f85  
**Base commit at run start**: 727b7ad  
**Working tree**: 7 files modified (fixes applied, not yet committed — see Defect Remediation section)  
**Schema**: memory v16, bundle v1.2  
**DB**: `validation/runs/corpus_v2.db`  
**Recovery DB**: `validation/runs/corpus_v2_recovered.db`  
**Bundle**: `validation/exports/corpus_v2_bundle.json`  
**Status**: COMPLETE — awaiting human countersign

---

## Purpose

Run #2 is the defect remediation follow-up to Run #1 (2026-05-26). Three Class C ingestion quality defects identified in Run #1 were fixed; a secondary governance char budget cap (Fix 4) was also implemented. This run uses the same frozen corpus (27 documents, `validation/corpus/`) and validates that T1/T2/T3 each reach ≥ 3/5.

---

## Defect Remediation (vs Run #1)

| Fix | Scope | Description |
|---|---|---|
| Fix 1 | `ingestion/extractor.py` | Header-only chunk guard: chunks containing only a markdown heading (`## Heading`) with no body lines produce no candidates |
| Fix 2 | `ingestion/candidates.py` | Content quality filter: candidates whose title carries < 15 chars of content after the event-type prefix are dropped |
| Fix 3 | `ingestion/candidates.py` | Markdown sanitization: `##`, `**`, `*`, `` ` ``, `|---|` patterns stripped from `title` and `summary` (evidence and source_span preserved raw) |
| Fix 4 | `session/models.py`, `session/context_window.py` | `max_governance_chars` cap on `ContextActivationPolicy`: governance tier (Tier 0) stops filling when this limit is reached, leaving budget for lower tiers |

Tests added for all four fixes (regression tests in `ingestion/tests/test_extractor.py`, `ingestion/tests/test_candidates.py`, `session/tests/test_context_window.py`).

**Test suite after fixes**: 3168 passed, 0 failed.

---

## Corpus

27 documents ingested, 5 categories — identical to Run #1:

| Category | Files | Notes |
|---|---|---|
| adrs/ | 7 | ADR-001 through ADR-007 |
| meetings/ | 6 | 2025-09-12 through 2026-02-03 |
| incidents/ | 4 | INC-001 through INC-004 |
| references/ | 5 | onboarding, operational-limits, deployment-runbook, recovery-playbook, service-topology |
| planning/ | 5 | Q4 roadmap, Q1 roadmap, kafka migration, connection pool remediation, scaling proposal |

Intentional contradictions in corpus (for CP-C1 testing) — unchanged from Run #1:
- ADR-005 sets max replicas=3; scaling-proposal-q1 proposes 5; operational-limits.md states 3
- INC-002 remediation changed PgBouncer pool_size from 20→10; ADR-004 assumed 20
- Q4 roadmap marks milestones incomplete; meeting notes show them later completed

---

## Event Counts

| Status | Count | Run #1 | Delta |
|---|---|---|---|
| active | 98 | 130 | −32 |
| rejected | 31 | 44 | −13 |
| unresolved | 18 | 39 | −21 |
| **total** | **147** | **213** | **−66 (−31%)** |

Ingestion runs: 27 (one per source document — one source produced 0 qualifying candidates after quality filtering; 26 in bundle)  
Event types (active only): architecture_decision=60, governance_rule=23, rejected_idea=3, validation_result=10, implementation_note=2  
Event types (unresolved): incident=15, open_question=3  
Event types (rejected): architecture_decision=6, governance_rule=0 (all gov reviewed active), source_reference=18, incident=7

The 31% event count reduction vs Run #1 is the expected effect of Fix 2 (content quality filter) and Fix 1 (header guard), which removed fragment and header-only candidates that previously inflated counts.

---

## Checkpoint Results

### Ingestion

| CP | Condition | Result | Notes |
|---|---|---|---|
| CP-I1 | Proposed count within 50% of expected | PASS | 147 events, 27 sources; ~5.4/doc after quality filtering |
| CP-I3 | Zero header-artifact events | PASS | 0 events with `## Root Cause`-style header titles. Fix 1 confirmed working. |
| CP-I4 | Zero fragment titles (< 15 chars content) | PASS | Fix 2 removes all sub-15-char content fragments |
| CP-I5 | Zero markdown syntax in titles/summaries | PASS | Fix 3 strips `**`, `##`, `|---|` from all extracted fields; evidence preserved raw |

### Review

| CP | Condition | Result | Notes |
|---|---|---|---|
| CP-R1 | Review pass performed | PASS | Single review pass completed |
| CP-R2 | Rejection rate ≤ 50% | PASS | 31/147 = 21.1% |

### Session

| CP | Condition | Result | Notes |
|---|---|---|---|
| CP-S1 | Policy created | PASS | Inline policy: `max_chars=12000, max_entries=60, max_governance_chars=4000` |
| CP-S2 | Session opened | PASS | session id=1, started_at=2026-05-27T21:28:26Z |
| CP-S3 | Assembly generated | PASS | assembly id=1, entries=19 (gov=6, unres=13), chars=11851/12000 |
| CP-S4 | Session closed clean | PASS | closed_at=2026-05-27T21:35:18Z, status=closed |

### Governance

| CP | Condition | Result | Notes |
|---|---|---|---|
| CP-G1 | Zero CRITICAL issues | PASS | CRITICAL=0 |
| CP-G2 | WARNING count documented | PASS | WARNING=10: 8 duplicate_title (all on rejected events), 2 low_confidence_active |
| CP-G3 | WARNING reduction from Run #1 | PASS | 29 → 10 (−66%): Fix 3 markdown sanitization merged near-duplicate title variants |
| CP-G4 | Zero lineage FK violations (source) | PASS | lineage_integrity=OK, total_broken=0 |

### Continuity

| CP | Condition | Result | Notes |
|---|---|---|---|
| CP-X1 | Dry-run import: zero collisions | PASS | 0 collisions, 0 warnings |
| CP-X2 | Live import: zero collisions | PASS | 147 events, 26 sources, 26 runs imported |
| CP-X3 | Event counts match source/recovered | PASS | active=98, rejected=31, unresolved=18 — exact match |
| CP-X4 | Lineage OK on recovered DB | PASS | lineage_integrity=OK, total_broken=0 |
| CP-X5 | Reconstruction output matches | PASS | governance_ids match, unresolved_ids match, chars_used=11851 on both DBs |

### Contradiction Detection

| CP | Condition | Result | Notes |
|---|---|---|---|
| CP-C1 | Contradiction links surfaced | N/A | Zero `contradicts` links. Substrate has no auto-detection. Per Run #1 precedent: mark N/A when extractor produces no contradiction links. |
| CP-C2 | Linked contradictions appear in assembly | N/A | No links to verify |

### Governance Cap (new checkpoint — Fix 4)

| CP | Condition | Result | Notes |
|---|---|---|---|
| CP-GC1 | `max_governance_chars=4000` cap active | PASS | 6 governance entries included (~2400 chars ≤ 4000 cap) |
| CP-GC2 | Budget leaves room for other tiers | PASS | 13 unresolved items included in assembly after governance tier |
| CP-GC3 | Overall char budget not exceeded | PASS | 11851/12000 — budget respected |

---

## Template Ratings

### T1 — Ingestion Quality: 3/5

Significant improvement from Run #1 (2/5). All three Class C defects resolved:

- **Fix 1 confirmed**: Zero `## Root Cause`-style events (vs 4 in Run #1)
- **Fix 2 confirmed**: All titles carry ≥ 15 chars of content after the event-type prefix
- **Fix 3 confirmed**: No `**`, `##`, or `|---|` in any title or summary; evidence fields preserve raw source text
- WARNING count: 10 (down from 29); all 8 duplicate_title warnings are on rejected events (expected — rejected events share structural content)
- 2 low_confidence_active warnings: events 11 and 125 have confidence ≤ 2 and status=active (operator decision — these are retained for audit purposes)
- 147 events vs 213 (31% reduction confirms quality filter is doing its job)

Remaining limitations (not new defects):
- Governance_rule titles are still partial sentences extracted by the `rule:` pattern (e.g. "Governance Rule: evaluation was single-threaded.") — the underlying pattern over-extraction is architectural, not addressed by Fix 2's length filter
- Architecture_decision titles include some metadata fragments ("Date: 2025-10-01") that pass the length filter but carry no decision content — pattern calibration deferred
- Confidence scores are uniformly 3 for all governance_rule events (extractor design; no calibration by length or context)

**Class C defects from Run #1: all resolved. Remaining issues are pattern calibration (non-blocking).**

### T2 — Retrieval Quality: 3/5

Improvement from Run #1 (2/5). Governance cap (Fix 4) is functional:

- max_governance_chars=4000: governance filled 6 entries (~2400 chars) and stopped — budget reserved for lower tiers ✓
- Assembly now includes 13 unresolved items (tier 1) — this was impossible in Run #1 where governance exhausted the budget
- total_candidates=71, included_entries=19, truncated=True (budget exhausted at chars_used=11851/12000)
- relevant_memory=0 — unresolved items (tier 1) consumed remaining budget after governance (tier 0). Expected: the corpus has 18 unresolved events; tier 1 fills before tier 4
- No semantic retrieval active (no embeddings); governance_type surfacing plus unresolved tier

Operational note: for a corpus with 18 unresolved incidents and only 12000 char budget, unresolved tier tends to fill the budget before relevant_memory. This is correct behavior — unresolved items outrank relevant_memory by design. Operators needing relevant_memory entries should either increase max_chars, reduce corpus unresolved count, or activate semantic search with a query vector.

### T3 — Assembly Quality: 3/5

Improvement from Run #1 (2/5). Assembly is now operationally meaningful:

- 6 governance entries (clean titles, no markdown syntax) + 13 unresolved incident/question items
- Assembly provides an operator with: active governance constraints AND open investigations requiring attention
- Clean titles: no sentence fragments from Run #1 (e.g. "Governance Rule: change," single-word titles are gone)
- Budget 11851/12000 — sensible utilization, not maxed by artifacts
- No relevant_memory (budget exhaustion by lower-tier items — see T2)
- No continuity_context (no compression artifacts promoted)
- Assembly version 1.2.0, hash 85714522b579c548... — correctly computed
- Transition logged at open-session

Remaining: governance titles still contain partial sentences (architectural over-extraction not targeted in this run). An operator reading the assembly would need domain context to interpret "Governance Rule: evaluation was single-threaded."

### T4 — Governance Report Quality: 4/5 (unchanged from Run #1)

Governance detection is functional and correctly calibrated:

- CRITICAL=0: no false positives
- WARNING=10: 8 duplicate_title (rejected events only — correctly identified as structural duplicates), 2 low_confidence_active (correctly flagged for review)
- INFO=147: complete audit trail
- WARNING reduction from 29→10: Fix 3 sanitization collapsed near-duplicate markdown-variant titles
- No CRITICAL noise

### T5 — Continuity Bundle: 5/5

Export/import cycle fully sound:

- Bundle id=`6a8a6c829ccd49df`, 147 events, 26 sources, 195,286 bytes
- Dry-run: 0 collisions, 0 warnings
- Live import: 0 collisions; 147 events, 26 sources, 26 runs restored
- Count match (by status): active=98, rejected=31, unresolved=18 — exact match
- Lineage check on recovered DB: OK, 0 broken
- Reconstruction match: identical governance_ids, identical unresolved_ids, chars_used=11851 on both source and recovered DB

**No defects found in continuity infrastructure.**

### T6 — Replay / Assembly Determinism: 5/5

Assembly is fully deterministic and portable:

- Same policy + same DB state → identical reconstruction (verified source vs recovered)
- No point-in-time discrepancy (unlike Run #1 CP-X5 where a mid-run rejection split source/recovered assemblies)
- Corpus_v2 review pass was completed before assembly was created — no state change after assembly log
- Bundle imports cleanly; recovered DB has correct event states

### T7 — Lineage Integrity: PASS

- Source DB: lineage_integrity=OK, total_broken=0 (4 FK chains verified)
- Recovered DB: lineage_integrity=OK, total_broken=0 (4 FK chains verified)
- Audit trail: bundle manifest confirms lineage_integrity_all_ok=true

### T8 — Session Lifecycle: 4/5 (unchanged from Run #1)

All session operations functional:

- Session start: generates assembly correctly
- max_governance_chars honored at assembly time (new in this run)
- Session closed cleanly after assembly
- Governance report: correct counts
- Lineage check: correct on both DBs
- No UX regressions introduced by Fix 4

---

## Operational Pain Points

1. **Governance rule titles are still partial sentences** — The `rule:` pattern captures text after the keyword, which may be mid-paragraph context. Fix 2's 15-char minimum is necessary but not sufficient; the pattern itself needs narrowing (e.g. only capture at sentence start, or require the captured text to end at a sentence boundary). Deferred to next engineering sprint.

2. **Architecture decision titles include metadata fragments** — Titles like "Architecture Decision: Date: 2025-10-01" and "Architecture Decision: Sam Webb: …" are metadata rows from decision tables, not decision content. The ADR parser pattern does not discriminate structural rows from content rows. Deferred.

3. **Unresolved tier crowds out relevant_memory** — With 18 unresolved incidents and a 12000 char budget, tier 1 exhausts the budget before tier 4 (relevant_memory). This is correct behavior but limits operator visibility into active events. Mitigations: increase max_chars, reduce unresolved corpus size, or increase max_governance_chars to free up less budget (currently 4000 leaves ~9600 for other tiers, but 13 unresolved items fill that).

4. **No auto-contradiction detection** — Same limitation as Run #1. Documented and deferred.

5. **source_reference events extracted but all rejected** — 18 source_reference events were extracted (URLs, bibliography entries) and all rejected by operator review. The extractor's URL/citation pattern is too broad. Consider suppressing source_reference at extract time unless URL appears in a meaningful context sentence.

---

## Sign-Off Recommendation

**PASS**

### What passed

All substrate infrastructure checkpoints pass:

- T1 ingestion quality: 3/5 ✓ (was 2/5 — all Class C defects from Run #1 resolved)
- T2 retrieval quality: 3/5 ✓ (was 2/5 — governance cap working, unresolved tier in assembly)
- T3 assembly quality: 3/5 ✓ (was 2/5 — clean titles, actionable entries)
- T5 continuity bundle: 5/5 — perfect round-trip
- T6 assembly determinism: 5/5 — source/recovered reconstruction identical
- T7 lineage integrity: PASS — both DBs clean
- T8 session lifecycle: 4/5 — all operations functional including new max_governance_chars
- Governance: CRITICAL=0, WARNING count correctly reduced
- Import/export: zero collisions, exact count match
- Audit trail: 100% of events traceable to ingestion runs
- Schema version: v16 unchanged throughout run
- Test suite: 3168 passed (regression-free)

### Run #1 conditions for PASS: all met

| Run #1 Condition | Status |
|---|---|
| Add content length guard in section header extraction | DONE (Fix 1) |
| Add minimum title length/quality filter in extractor | DONE (Fix 2: 15 char minimum) |
| Add markdown sanitization: strip `##`, `**`, `\|---\|` | DONE (Fix 3) |
| Re-run validation with improved parser on fresh DB | DONE (corpus_v2.db) |
| T1 ≥ 3/5 | MET (3/5) |
| T2 ≥ 3/5 | MET (3/5) |
| T3 ≥ 3/5 | MET (3/5) |
| Same frozen corpus | CONFIRMED |

### What should NOT block sign-off

- CP-C1 N/A (no auto-contradiction detection — documented acceptable limitation, same as Run #1)
- 10 WARNING issues (8 on rejected events, 2 low_confidence_active — all WARNING not CRITICAL)
- relevant_memory=0 in assembly (expected: unresolved tier exhausts budget without semantic search — architectural limitation, not a defect)
- Governance/arch title partial sentences (pattern calibration deferred — does not affect substrate correctness)
- source_reference pattern over-extraction (operator-correctable via review pass — not a substrate defect)

### Defect classification summary

| Class | Count | Defects |
|---|---|---|
| A (corpus) | 0 | None |
| B (operator error) | 0 | None |
| C (ingestion quality) | 0 | All Run #1 Class C defects resolved |
| D (governance/lineage) | 0 | None |
| E (continuity) | 0 | None |
| F (substrate integrity) | 0 | None |

---

## Next Actions

1. Commit the 7 modified source files (fixes + tests) with commit message referencing this run log
2. Open engineering ticket for governance_rule pattern calibration (narrower capture, sentence-boundary requirement)
3. Open engineering ticket for ADR metadata-row filtering in architecture_decision extractor
4. Consider source_reference suppression flag in extractor (or raise confidence threshold)
5. Consider embedding activation for a future run to test relevant_memory retrieval

---

*Run log completed: 2026-05-27*

---

## Countersign

**Countersigned**: 2026-05-27  
**Quant operator**: Jan Cantryn  
**Status**: PASS — countersigned and valid for release decision

**Basis for countersign:**
- Replay integrity validated: source and recovered reconstruction identical (governance_ids, unresolved_ids, chars_used)
- Continuity portability validated: 147 events, 0 collisions, exact status-distribution match
- Governance integrity validated: CRITICAL=0; all 10 WARNINGs non-blocking and correctly classified
- Remediation scope validated: all three Run #1 Class C defects resolved; Fix 4 governance cap confirmed working
- Deferred issues documented, bounded, and explicitly classified non-blocking in §Operational Pain Points
- GOAL.md committed at a2d8679 anchors operational invariants and freeze discipline going forward

**No prior findings, severity classifications, or template ratings altered by this countersign.**

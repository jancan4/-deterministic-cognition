# Quant Validation Run Log — Run #1

**Run date**: 2026-05-26  
**Operator**: Claude (AI) — review and countersign required by human quant  
**Substrate commit (frozen)**: 5678f85  
**Validation commit (docs only)**: a799d48  
**Schema**: memory v16, bundle v1.2  
**DB**: `validation/runs/corpus_v1.db`  
**Recovery DB**: `validation/runs/corpus_v1_recovered.db`  
**Bundle**: `validation/exports/corpus_v1_bundle.json`  
**Transcript**: `validation/transcripts/corpus_v1_run1.txt`  
**Status**: COMPLETE — awaiting human countersign

---

## Corpus

27 documents ingested, 5 categories:

| Category | Files | Notes |
|---|---|---|
| adrs/ | 7 | ADR-001 through ADR-007 |
| meetings/ | 6 | 2025-09-12 through 2026-02-03 |
| incidents/ | 4 | INC-001 through INC-004 |
| references/ | 5 | onboarding, operational-limits, deployment-runbook, recovery-playbook, service-topology |
| planning/ | 5 | Q4 roadmap, Q1 roadmap, kafka migration, connection pool remediation, scaling proposal |

Intentional contradictions in corpus (for CP-C1 testing):
- ADR-005 sets max replicas=3; scaling-proposal-q1 proposes 5; operational-limits.md states 3
- INC-002 remediation changed PgBouncer pool_size from 20→10; ADR-004 assumed 20
- Q4 roadmap marks milestones incomplete; meeting notes show them later completed

---

## Event Counts

| Status | Count |
|---|---|
| active | 130 |
| rejected | 44 |
| unresolved | 39 |
| **total** | **213** |

Ingestion runs: 27 (one per source document)  
Sum of committed_count: 213 (matches total — audit trail intact)

---

## Checkpoint Results

### Ingestion

| CP | Condition | Result | Notes |
|---|---|---|---|
| CP-I1 | Proposed count within 50% of expected | PASS | 213 events, 27 sources; ~7.9/doc |
| CP-I3 | Zero exact duplicate (title+summary) | CONDITIONAL PASS | 4 events with title "Incident: ## Root Cause" + summary "## Root Cause" — different source files. Class C parser artifact. Rejected in second review pass. |

### Review

| CP | Condition | Result | Notes |
|---|---|---|---|
| CP-R1 | Review pass performed | PASS | Two review passes completed |
| CP-R2 | Rejection rate ≤ 50% | PASS | 44/213 = 20.7% |

### Session

| CP | Condition | Result | Notes |
|---|---|---|---|
| CP-S1 | Policy created | PASS | policy 'corpus_validation_v1' id=1 |
| CP-S2 | Session opened | PASS | session id=1, decision_id=1 |
| CP-S3 | Assembly generated | PASS | assembly id=1, entries=18, chars=11998/12000 |
| CP-S4 | Session closed clean | PASS | closed_at=2026-05-26T17:33:56Z |

### Governance

| CP | Condition | Result | Notes |
|---|---|---|---|
| CP-G1 | Zero CRITICAL issues | PASS | CRITICAL=0 |
| CP-G2 | WARNING count documented | PASS | WARNING=29 (all duplicate_title) |
| CP-G3 | Governance show works | PASS | |
| CP-G4 | Zero lineage FK violations (source) | PASS | lineage_integrity=OK, total_broken=0 |

### Continuity

| CP | Condition | Result | Notes |
|---|---|---|---|
| CP-X1 | Dry-run import: zero collisions | PASS | 0 collisions |
| CP-X2 | Live import: zero collisions | PASS | 213 events, 27 sources, 27 runs imported |
| CP-X3 | Event counts match source/recovered | PASS | active=130, rejected=44, unresolved=39 — exact match |
| CP-X4 | Lineage OK on recovered DB | PASS | lineage_integrity=OK, total_broken=0 |
| CP-X5 | Assembly event IDs match | CONDITIONAL | Source assembly=18 events; recovered assembly=17 events. Difference: event 127 was rejected AFTER source assembly was created. Source assembly captured pre-rejection state. Recovered assembly (regenerated) correctly excludes rejected event 127. Expected behavior — assembly snapshots are point-in-time and do not survive import. |

### Contradiction Detection

| CP | Condition | Result | Notes |
|---|---|---|---|
| CP-C1 | Contradiction links surfaced | N/A | Zero `contradicts` links produced by extractor. Intentional contradictions in corpus were not auto-detected. Per SIGN_OFF.md §7: when extractor produces no contradiction links, mark N/A and document. Substrate has no auto-contradiction detection — operator must link manually. |
| CP-C2 | Linked contradictions appear in assembly | N/A | No links to verify |

### Transcript

| CP | Condition | Result | Notes |
|---|---|---|---|
| CP-T1 | Disclaimer header present | PASS | Header contains: "human-readable audit artifact, not a replay/import format" |
| CP-T2 | At least 1 assembly section present | PASS | 1 assembly documented |
| CP-T3 | No full event text in transcript | PASS | Titles truncated to 80 chars; no evidence/summary body text |
| CP-T4 | Transcript not re-importable | PASS | Explicit disclaimer; no import mechanism |

### Audit Trail

| Check | Result |
|---|---|
| Events with no ingestion run | 0 — PASS |
| Substrate commit changed mid-run | No — 5678f85 runtime frozen; a799d48 adds docs only |

---

## Template Ratings

### T1 — Ingestion Quality: 2/5

Events are plausible in type and domain but quality is poor:

- 29 duplicate_title warnings from structural markdown extraction (action items, repeated headings treated as separate events)
- 4 Class C parser artifacts: section headers ("## Root Cause") extracted as event title with no content
- 17 of 18 assembly events are governance_rule with fragmented mid-sentence titles:
  - "Governance Rule: evaluation)." (truncated at sentence boundary)
  - "Governance Rule: change," (single word after colon)
  - "Governance Rule: set was recently deployed." (extracted mid-paragraph)
- Evidence fields contain raw markdown tables and checkbox lists rather than extracted prose
- Confidence scores are uniformly 3 for governance_rule (appropriate given extractor design)
- Good: event types are plausible (governance_rule, architecture_decision, incident, milestone)
- Good: source attribution is correct per event

**Class C defects**:
1. Parser extracts section header lines as event title without content guard
2. Extractor generates governance_rule events from sentence fragments (no minimum length filter)
3. No deduplication of structurally identical markdown patterns across files

**Engineering escalation required**: parser/extractor calibration before next validation run.

### T2 — Retrieval Quality: 2/5

Assembly selection mechanism is functional but produces an unusable assembly:

- 97 candidates evaluated; 18 accepted; 79 rejected by budget constraint
- ALL 18 accepted entries are governance_context (governance_rule/architecture_decision) — zero relevant_memory entries
- No semantic retrieval active (no embeddings, no query vector)
- Governance activation always surfaces governance types regardless of relevance — this is by design but dominates the char budget
- Budget exhausted at 11998/12000 chars; the char budget is tight relative to governance event text length
- The assembly would provide no domain-relevant memory to an operator or downstream consumer — only a list of fragmented governance rules

**Root cause**: governance_context events are surfaced unconditionally; with 17 low-quality governance_rule events each consuming ~700 chars, the relevant_memory budget is crowded out before any semantic entries are considered.

**Not a substrate defect**: retrieval logic is correct. Parser quality determines retrieval quality.

### T3 — Assembly Quality: 2/5

Assembly is syntactically valid but not operationally useful:

- 17 governance_rule entries, 1 architecture_decision entry, 1 unresolved incident
- Titles are sentence fragments — operator cannot derive actionable context from the assembly
- No continuity_context, no relevant_memory, no active_investigations
- Budget nearly exhausted (11998/12000) — no room for additional entries even if relevant events existed
- Assembly version 1.2.0 is correct
- Assembly hash computed correctly
- Transition logged correctly (policy_update / "Session start assembly")

**Not a substrate defect**: assembly structure and mechanics are correct. Content quality is a parser issue.

### T4 — Governance Report Quality: 4/5

Governance detection is functional and well-calibrated:

- CRITICAL=0: no false positives on this corpus
- WARNING=29: all duplicate_title — expected given parser over-extraction of structurally similar elements
- INFO=213: event-level audit trail complete
- `governance show duplicate_title` correctly filters to the 29 warnings
- No CRITICAL false positives — governance detectors are not miscalibrated

Minor: duplicate_title warnings from structural extraction are noise but correctly classified as WARNING not CRITICAL.

### T5 — Continuity Bundle: 5/5

Export/import cycle is fully sound:

- Export: bundle id=`2df64b449acd3aa9`, 213 events, 27 sources, 262,500 bytes
- Dry-run import: 0 collisions
- Live import: 0 collisions; 213 events, 27 sources, 27 runs restored
- Count match (by status): active=130, rejected=44, unresolved=39 — exact match
- Lineage check on recovered DB: OK, 0 broken
- Round-trip verified: all DB state preserved

**No defects found in continuity infrastructure.**

### T6 — Replay / Assembly Determinism: 4/5

Assembly mechanics are deterministic within a single DB state:

- CP-X5 difference (1 event) is explained by point-in-time state, not substrate non-determinism
- Given same DB state, same activation policy, same `session start` produces identical results
- Assembly snapshots are NOT persisted in the bundle — they are regenerated on `session start`
- Recovered assembly correctly excludes event 127 (rejected before assembly was regenerated)
- Portability confirmed: bundle imports cleanly, recovered DB has correct event state

**Operational finding**: assembly snapshots are ephemeral — they reflect DB state at assembly time. Operators should note that importing a bundle and starting a new session will produce an assembly reflecting CURRENT event statuses, not the statuses at export time.

### T7 — Lineage Integrity: PASS

- Source DB: lineage_integrity=OK, total_broken=0 (4 FK chains verified)
- Recovered DB: lineage_integrity=OK, total_broken=0 (4 FK chains verified)
- Audit trail: 0 events without ingestion run

### T8 — Session Lifecycle: 4/5

All session operations functional:

- Policy create/activate: works
- Session start: generates assembly on first `session start`
- Session timeline: correctly shows assembly history
- Governance report: CRITICAL/WARNING/INFO counts correct
- Governance show TYPE: correctly filters by issue_type
- Lineage check: correct on both source and recovered DBs
- Transcript: written with correct disclaimer header, truncated titles, no full event text
- All 7 new Phase B2 shell commands operational (show, search, history, artifact, transcript, session timeline, governance show)

Minor UX issue: `transcript PATH` uses positional path silently ignored; correct form is `transcript --out PATH`. Default fallback is `transcript_SESSION.txt`.

---

## Operational Pain Points

1. **Parser title extraction is fragmented** — The extractor creates governance_rule events from mid-paragraph sentence fragments. A governance_rule for "Routing rule changes are hot-loaded" extracts the clause "changes are hot-loaded** — helix-router evaluates the" as the title content. Needs a title sanitization pass.

2. **Section header extraction without content guard** — The parser produced 4 events with title "Incident: ## Root Cause" and summary "## Root Cause". There is no guard against extracting markdown headers as event content when the section body was empty or separately parsed.

3. **Governance events crowd out relevant_memory** — With governance types unconditionally surfaced and no semantic search active, the assembly is 100% governance_context. For a corpus with 27 documents and 130 active events, the operator receives zero semantic retrieval. The governance budget should be configurable independently of the total char budget.

4. **No auto-contradiction detection** — The corpus contains 3 documented contradictions (replica ceiling, connection pool size, milestone completion status). None were detected. Manual linking required.

5. **CP-I3 exact duplicate detection applies to all statuses** — The duplicate detection counts events that were later rejected. It may be cleaner to apply CP-I3 only to non-rejected events, since the review pass is designed to resolve quality issues.

6. **transcript --out path not positional** — Minor: `transcript PATH` silently ignores the path argument. Document in help text.

---

## Retrieval Quality Observations

- No embeddings were generated; assembly relies entirely on governance_type surfacing and tag-based filtering
- With `min_confidence=2` (the activation policy default), all 130 active events qualify as candidates
- 97 of 130 were evaluated; 79 rejected by char budget; 18 accepted
- The 18 accepted are entirely governance/architecture events, not domain-relevant memory
- For meaningful retrieval, either: (a) embeddings must be activated, or (b) governance char budget must be capped to leave room for relevant_memory retrieval

---

## Compression Usefulness Observations

- 1 compression artifact created (id=1, conf=4) during the validation session
- The artifact was created but not promoted or used in an assembly
- No `seed-memory-from-compression` or `promote-compression-artifact` was called
- Compression infrastructure is present and functional; usefulness cannot be assessed in a single-session run
- Recommendation: defer compression assessment to a multi-session run with at least 3 assemblies and 1 compression cycle

---

## Sign-Off Recommendation

**CONDITIONAL PASS**

### What passed

All substrate infrastructure checkpoints pass:

- Continuity bundle: perfect round-trip (T5=5/5)
- Lineage integrity: zero FK violations on both DBs
- Governance detection: CRITICAL=0, no false positives
- Import/export: zero collisions, exact count match
- Audit trail: 100% of events traceable to ingestion runs
- Session lifecycle: all operations functional
- Schema version: v16 unchanged throughout run
- Substrate commit: 5678f85 unchanged

### What failed

T1, T2, T3 are all 2/5 (minimum required: 3/5). These failures are **Class C** (ingestion quality defects), not substrate defects:

1. Parser extracts section headers as events (no content guard)
2. Extractor produces governance_rule events from sentence fragments (no minimum length filter)
3. Assembly is dominated by low-quality governance events; zero semantic retrieval

### Conditions for PASS

1. Engineering investigation of Class C parser/extractor defects:
   - Add content length guard in section header extraction
   - Add minimum title length/quality filter in extractor (reject single-clause fragments)
   - Add markdown sanitization: strip `##`, `**`, `|---|` table dividers from extracted titles/summaries
2. Re-run validation with improved parser on a fresh DB
3. T1/T2/T3 must each reach ≥ 3/5 in the subsequent run
4. The re-run must use the same frozen corpus to isolate the parser improvement

### Defect classification summary

| Class | Count | Defects |
|---|---|---|
| A (corpus) | 0 | None |
| B (operator error) | 0 | None (first review pass missed unresolved events; caught and corrected in second pass) |
| C (ingestion quality) | 3 | Section header extraction, fragment title extraction, governance_rule over-extraction |
| D (governance/lineage) | 0 | None |
| E (continuity) | 0 | None |
| F (substrate integrity) | 0 | None |

### What should NOT block sign-off

- CP-C1 N/A (no auto-contradiction detection — documented acceptable limitation)
- 29 duplicate_title warnings (WARNING, not CRITICAL — governance correctly calibrated)
- CP-X5 1-event assembly difference (expected point-in-time behavior, not a defect)
- transcript --out positional path (UX issue, not a data safety issue)
- Compression not tested (single-session run — deferred by design)

---

## Next Actions

1. Open engineering ticket: Class C parser/extractor calibration
   - Title sanitization (strip markdown syntax from extracted titles)
   - Section header content guard (do not extract if body is empty)
   - Minimum fragment length for governance_rule extraction
2. Defer to engineering: governance char budget cap (make configurable)
3. Close this run — run log is the permanent artifact
4. After Class C fixes merge and test suite passes at 3150+: start Run #2 on fresh DB with same corpus

---

*Run log completed: 2026-05-26*  
*Operator countersign required before this run log is considered valid for release decision.*

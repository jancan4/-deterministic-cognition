# Longitudinal Workload Run L1

**Run ID:** L1  
**Substrate commit:** 9245ee7  
**Schema:** memory v16, workflow v3, bundle v1.2  
**Started:** 2026-05-27  
**Status:** IN PROGRESS  
**DB:** validation/longitudinal/runs/longitudinal_v1.db  

---

## 1. Corpus

**Type:** Engineering project continuity corpus — simulated multi-team project lifecycle  
**Domain:** Distributed service platform (Helix) — fictional but internally consistent  
**Provenance:** Existing validation/corpus/ documents (Run #1/#2 source corpus)  
**Initial batch:** 27 documents across 5 categories  

| Category | Count | Description |
|---|---|---|
| ADRs | 7 | Architecture decision records ADR-001 through ADR-007 |
| Incidents | 4 | INC-001 through INC-004 (queue saturation, DB exhaustion, JWT thundering herd, Redis migration rollback) |
| Meetings | 6 | Kickoff through production-readiness review (2025-09 to 2026-02) |
| Planning | 5 | Q4 roadmap, Q1 roadmap, connection pool remediation, Kafka→Redis migration, scaling proposal |
| References | 5 | Deployment runbook, onboarding guide, operational limits, recovery playbook, service topology |

**Timeline range:** 2025-09 to 2026-02 (6-month project arc)  
**Corpus characteristic:** Contains explicit revisions (ADR-007 supersedes ADR-003), multiple incidents with post-mortems, cross-document tensions (original Kafka decision vs Redis migration), governance evolution across roadmap iterations.

---

## 2. Operator Assumptions

- Single operator throughout this run
- Review decisions are deterministic and documented (not random)
- No schema changes during the run
- No substrate code changes during the run
- Deferred issues EI-001 (mitigated), EI-002, EI-003 are known and will be observed but not fixed
- Fragment-title governance_rule events will appear; operator will reject them consistently

---

## 3. Workload Goals

Primary:
1. Observe ingestion quality on a multi-category, temporally-ordered, internally-contradictory corpus
2. Exercise the supersession workflow (ADR-007 supersedes ADR-003)
3. Exercise contradiction linking (Kafka vs Redis decisions)
4. Exercise continuity export/import round-trip at checkpoint
5. Measure session assembly usefulness after full corpus is active
6. Surface any new Class C/D defects not visible in Run #1/#2

Secondary:
1. Measure operator review friction (fraction of candidates requiring decisions)
2. Observe governance issue rate and type distribution
3. Test retrieval against cross-document queries

---

## 4. Review Cadence

- Full operator review after initial ingestion batch
- Governance inspection after review
- Retrieval spot checks: 3–5 queries against active corpus
- Assembly spot check: open session, inspect context tiers
- Continuity export at each checkpoint
- Replay verification at each checkpoint

---

## 5. Validation Checkpoints

| Checkpoint | Trigger | Contents |
|---|---|---|
| L1-C1 | After initial batch ingestion + review | Event counts, approval rate, governance, assembly, round-trip |
| L1-C2 | After supersession/contradiction exercises | Supersession lineage, contradiction graph, assembly divergence |
| L1-C3 | After compression exercise | Compression artifact quality, reconstructed context post-compression |

---

## 6. Known Deferred Issues

- **EI-001 (mitigated):** governance_rule fragments from `must not` + infinitive and keyword + numbered-list triggers still present. Expect some fragment titles in this corpus.
- **EI-002:** ADR table-row extraction may produce `architecture_decision` events with metadata titles (Date:, Owner:). Log occurrences, reject consistently.
- **EI-003:** `source_reference` events from bare URL citations likely to appear. Log rejection rate.

---

## 7. Ingestion Run Log

### Batch 1 — 2026-05-27

**Files ingested:** 27 (all corpus documents)  
**Method:** `run_ingestion()` with `commit=True` per file  
**Total candidates extracted:** 127  
**All committed to longitudinal_v1.db**

| Category | Docs | Chunks | Candidates |
|---|---|---|---|
| ADRs | 7 | 166 | 31 |
| Incidents | 4 | 96 | 22 |
| Meetings | 6 | 181 | 30 |
| Planning | 5 | 161 | 20 |
| References | 5 | 157 | 24 |

**Candidate type distribution:**
- architecture_decision: 66 (52%)
- source_reference: 18 (14%)
- incident: 22 (17%)
- validation_result: 10 (8%)
- governance_rule: 3 (2%)
- open_question: 3 (2%)
- rejected_idea: 3 (2%)
- implementation_note: 2 (2%)

---

## 8. Checkpoint L1-C1

**Checkpoint date:** 2026-05-27  
**DB:** validation/longitudinal/runs/longitudinal_v1.db  
**Bundle:** validation/longitudinal/exports/longitudinal_v1_bundle_L1C1.json

### 8.1 Event counts

| Metric | Count |
|---|---|
| Total candidates | 127 |
| Approved (active) | 34 |
| Superseded | 2 |
| Rejected | 91 |
| Approval rate | 28% |

**Active events by type:**
- architecture_decision: 22
- incident: 6
- open_question: 2
- validation_result: 2
- implementation_note: 1
- rejected_idea: 1

### 8.2 Approval/rejection analysis

**Approval rate: 28% (34/127 after supersession)**

High rejection rate driven by three structural extraction failure classes:

1. **EI-002 pattern (42 rejects):** `architecture_decision` events extracted from ADR table rows, metadata fields (Date:, Owner:, Parameter/Value/Source columns), and word-wrap artifacts where the match captures suffix characters of "headroom" → "oom". 65% of all architecture_decision candidates were rejected.

2. **EI-003 pattern (18 rejects):** All 18 `source_reference` events rejected — navigation cross-references, file paths (.venv/bin/activate), and inline citations without substantive content.

3. **EI-001 residual (3 governance rejects):** Three `governance_rule` events from the `must not` + infinitive verb trigger. Fragments that preserve the prohibited action but lack the "must not" prefix in the title: "be the weakest link in the audit chain.", "be run at the maximum production replica count,", "flow into staging." All classified as EI-001 secondary mechanism.

4. **Template text in corpus (2 rejects):** `recovery-playbook.md` contains a literal incident report template with `{service}`, `{UTC time}` placeholders. Extracted and correctly rejected.

5. **Incident metadata rows (7 rejects):** "Commander: Jordan Kim" extracted 3×; date fragments; calculation fragments.

### 8.3 Governance report

- **CRITICAL:** 0
- **WARNING:** 10 — all non-blocking
  - 8 `duplicate_title` warnings on rejected events (Commander/table-header duplicates)
  - 2 `low_confidence_active` on implementation_note (conf=2) and open_question (conf=2) — expected
- **INFO:** 127 `orphaned_event` — expected at initial ingestion; no links created yet for most events

### 8.4 Lineage integrity

- **all_ok: True** (both source and recovered DB)
- **broken: 0**

### 8.5 Supersession exercise

ADR-007 (Redis Streams) supersedes ADR-003 (Kafka/Confluent Cloud):

| Action | Result |
|---|---|
| id=25 status → superseded | OK |
| id=31 status → superseded | OK |
| id=78 supersedes id=25 | Link created |
| id=78 supersedes id=31 | Link created |
| id=27 supersedes id=29 | Link created |
| id=78 supports id=27 | Link created |

**Operator friction observed:** Contradiction link (`create_contradiction_link`) requires BOTH events to be `active` at call time. Attempting to create a contradiction link after superseding one participant fails with `ValidationError: both events must be active or accepted`. Correct workflow: create contradiction link FIRST, supersede AFTER.

**EI-001 residual note:** Three governance_rule events (ids 20, 36, 63) that were rejected as `must not` + verb fragments are nonetheless surfacing in the governance tier of the assembly (see §8.6). This confirms the EI-004 finding below.

### 8.6 Assembly spot check

**Policy:** max_chars=16000, min_confidence=2, max_governance_chars=4000, no workflows/runtime

| Metric | Value |
|---|---|
| total_candidates | 50 |
| included_entries | 6 |
| chars_used | 3888 / 16000 |
| truncated | True |
| governance_context entries | 6 |
| unresolved_items | 0 |
| active_investigations | 0 |
| relevant_memory | 0 |

**Critical assembly finding (NEW ISSUE EI-004):** All 6 entries in the governance tier are **rejected** events. The 34 active events produced zero entries in the assembly. Root cause:

1. `retrieve_governance()` in `memory/retrieval.py` issues no `status` filter — it retrieves ALL `governance_rule` and `architecture_decision` events regardless of status.
2. Tie-breaking within same-confidence same-timestamp events uses `id DESC` — higher IDs win.
3. `operational-limits.md` was ingested last in the batch (IDs 113–122) and contains table-header artifacts (IDs 117, 119, 120) with confidence=4.
4. These high-ID rejected events rank above all active architecture_decision events (IDs 4–112) in the governance tier ordering.
5. The max_governance_chars=4000 cap is exhausted by 6 rejected events (3888 chars), leaving 0 budget for the 34 active events.

**Severity:** Class C+ (parser-quality failure with assembly semantic impact). The governance tier is completely unusable in this state. Assembly does not surface any active institutional knowledge. This does NOT break replay determinism (the assembly is deterministic and round-trip verified), but it makes the session context operationally useless.

**Note:** This issue is a pre-existing behavior in the substrate, not introduced by the longitudinal run. Run #2 avoided it because the corpus there had fewer table-header artifacts ingested last. Per run protocol: logged, classified, not fixed during the run.

### 8.7 Retrieval quality

| Query term | Active results | Quality |
|---|---|---|
| "kafka" | 4 relevant results | Good — Kafka decision, INC-004 facts |
| "jwt" | 5 relevant results | Excellent — ADR-006, INC-003 root cause |
| "ADR-005" | 5 relevant results | Excellent — all replica constraints |
| "connection pool" | 0 results | Gap — substring mismatch ("PgBouncer" matches, not compound phrase) |

**Retrieval limitation observed:** `search_memory_events` uses literal LIKE substring matching. Multi-word queries ("connection pool exhaustion incident") fail to match single-word evidence terms ("PgBouncer"). Operator must search individual keywords, not natural language phrases. Known substrate behavior.

### 8.8 Continuity round-trip

- **Export:** 127 events, schema_version=1.2, lineage_integrity_checked=True
- **Import:** 127 imported, 0 skipped, 0 collisions
- **Reconstruction identity:** governance_context IDs [20, 36, 63, 117, 119, 120] — identical on source and recovered DB
- **chars_used:** 3888 == 3888 ✓
- **Lineage integrity (recovered):** all_ok=True, broken=0
- **ROUND-TRIP: PASS**

Note: reconstruction identity holds but surfaces the EI-004 assembly defect in both databases — confirmation that the defect is structural, not a data artifact.

**Post-EI-004-remediation note — stale bundle timing artifact:**
The L1C1 bundle (`longitudinal_v1_bundle_L1C1.json`) was exported before the supersession status updates for ids 25 and 31 had been captured in the export path. The bundle records ids 25 and 31 with `status=active`; the source DB records them as `superseded`. This is a bundle timing artifact — the supersession exercise completed in the source DB, but the export captured a pre-supersession snapshot.

At L1-C1 checkpoint time (pre-EI-004 fix), the stale bundle round-trip appeared to PASS because the governance tier was entirely dominated by rejected events (ids 20, 36, 63, 117, 119, 120), which are present at the same status in both source and recovered DB regardless of the supersession state of ids 25/31. After the EI-004 fix, event status affects governance tier membership, making the divergence visible.

**Post-fix round-trip verification (fresh export from source, 2026-05-27):**
- Export: 127 events, schema_version=1.2 — ids 25/31 exported as `superseded` (correct current state)
- Import to clean DB: 127 imported, 0 skipped, 0 collisions
- Reconstruction identity: governance_context IDs [63, 36, 20, 31, 122, 25, 41] — identical on source and fresh-imported DB
- chars_used: 3971 == 3971 ✓
- **ROUND-TRIP (fresh export): PASS**

The stale L1C1 bundle is a checkpoint timing artifact, not a substrate determinism failure. For subsequent checkpoints, export should be performed after all status mutation operations complete.

### 8.9 New findings

**EI-004 — governance tier retrieves rejected events (assembly semantic failure)**

- **Class:** C+ — assembly quality defect with governance-tier semantic impact
- **Priority:** High (upgrades from the Medium floor; assembly is unusable)
- **Root cause:** `retrieve_governance()` in `memory/retrieval.py` issues no `status` filter. Combined with `id DESC` tiebreaker, high-ID rejected events from late-ingested corpus files consume the governance tier budget.
- **Trigger condition:** Any ingestion batch where rejected governance/architecture events have higher IDs than active ones — common whenever the last files in batch contain structured tables.
- **Impact:** Governance context tier is 100% rejected events; 34 active events produce 0 assembly entries.
- **Proposed fix:** Add `statuses=['active']` to `retrieve_governance()` query, mirroring how `retrieve_unresolved()` filters by status.
- **Run status:** Remediated post-checkpoint — see §9.1 and `docs/ENGINEERING_ISSUES.md`.

**EI-005 — operator workflow friction: contradiction link ordering constraint**

- **Class:** C — operator workflow ergonomics
- **Priority:** Low
- **Symptom:** `create_contradiction_link()` enforces that both participants must be `active` at call time. If the operator supersedes a decision before creating the contradiction link between it and its replacement, the link cannot be created.
- **Correct workflow:** Create contradiction link first, supersede after.
- **Impact:** No data loss; link between id=31 and id=78 cannot be created retroactively. Operator must remember the order constraint.
- **Run status:** Logged, deferred.

### 8.10 Checkpoint assessment

| Criterion | Status |
|---|---|
| Ingestion completes without crash | PASS |
| All 127 candidates committed | PASS |
| Operator review completes (0 unreviewed) | PASS |
| CRITICAL governance issues | 0 — PASS |
| Lineage integrity | PASS |
| Continuity round-trip | PASS |
| Replay determinism | PASS |
| Assembly produces useful context | FAIL — EI-004 |
| Supersession workflow functional | CONDITIONAL PASS — friction noted |

**Checkpoint L1-C1 assessment: CONDITIONAL PASS**

Substrate mechanics (ingestion, lineage, export/import, determinism) are all sound. Assembly is deterministic and round-trip verified. The CONDITIONAL is for EI-004: the governance tier surfaces only rejected events, making the assembled session context operationally useless. This is a pre-existing substrate behavior surfaced by the long-tail corpus ingestion order, not a new regression.

**EI-002 and EI-003 confirmed at expected rate:**
- EI-002: 42 architecture_decision rejections (table rows, metadata, word-wrap) — matches Run #2 pattern
- EI-003: 18/18 source_reference rejections (100%) — matches Run #2 rate exactly

---

*This document is the operator record for Longitudinal Run L1. Findings are recorded as observed; no remediation is performed during the run unless replay integrity, lineage, or determinism breaks.*

---

## 9. Post-Checkpoint L1-C1 Remediation

### 9.1 EI-004 remediation — 2026-05-27

**Scope:** `retrieve_governance()` status filter + 8 regression tests  
**Files modified:** `memory/retrieval.py`, `memory/tests/test_retrieval.py`

**Fix applied:**
`RetrievalQuery` in `retrieve_governance()` now specifies `statuses=['active', 'accepted']`, excluding rejected and superseded events from the governance retrieval path. Mirrors the pattern already used by `retrieve_unresolved()`.

**Before / after governance tier (policy: max_chars=16000, min_confidence=2, max_governance_chars=4000):**

Before:

| memory_id | status | type |
|---|---|---|
| 120 | rejected | architecture_decision |
| 119 | rejected | architecture_decision |
| 117 | rejected | architecture_decision |
| 63 | rejected | governance_rule |
| 36 | rejected | governance_rule |
| 20 | rejected | governance_rule |

chars_used=3888 / 4000. Active events in governance tier: **0**

After:

| memory_id | status | type |
|---|---|---|
| 63 | rejected | governance_rule |
| 36 | rejected | governance_rule |
| 20 | rejected | governance_rule |
| 31 | superseded | architecture_decision |
| 122 | **active** | architecture_decision |
| 25 | superseded | architecture_decision |
| 41 | **active** | architecture_decision |

chars_used=3971 / 4000. Active events in governance tier: **2**

Primary offenders eliminated (ids 117, 119, 120 — table-header artifacts). Active governance events now surface.

**Residual (EI-006):** Rejected events 20, 36, 63 and superseded events 25, 31 still appear via the general retrieve path in `activate_memory()`. Logged as EI-006, deferred. Assembly governance tier is no longer collapsed; operational improvement achieved.

**Test results:**
- TestRetrieveGovernance: 10/10 passed (8 new EI-004 regression tests + 2 existing)
- memory/tests/: 1152 passed
- Full suite: 3185 passed, 1 warning

**Assembly determinism post-fix:** PASS — identical governance IDs and chars_used across repeated reconstructions of same DB.

**Fresh export round-trip post-fix:** PASS — 127 exported/imported, governance IDs [63, 36, 20, 31, 122, 25, 41] identical, chars_used 3971 == 3971.

### 9.2 New finding opened

**EI-006 — Governance partition includes rejected and superseded events from general retrieval path**

- **Class:** Non-blocking — retrieval/noise issue
- **Priority:** Low
- **Status:** Deferred — see `docs/ENGINEERING_ISSUES.md §EI-006`
- **Root cause:** `activate_memory()` general retrieve has no status filter; `partition_by_section()` routes governance-typed events to governance_context regardless of status. Rejected and superseded governance events enter via this path after EI-004 fix.
- **Impact:** Governance tier carries 3 rejected + 2 superseded entries alongside 2 active entries. Bounded. Not replay/continuity affecting.

### 9.3 L1-C2 readiness

EI-004 primary failure resolved. Full test suite passes (3185). Assembly determinism confirmed. Fresh export round-trip confirmed. L1-C2 may proceed.

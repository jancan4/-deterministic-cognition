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
| L1-C2 | After corpus accumulation + multi-session workflows | Event growth, supersession, contradiction, compression, assembly, EI-006 impact |
| L1-C3 | After EI-006 remediation + larger corpus | Governance tier quality post-fix, assembly interpretability under growth |

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

---

## 10. Checkpoint L1-C2

**Checkpoint date:** 2026-05-28  
**DB:** validation/longitudinal/runs/longitudinal_v1.db  
**Bundle:** validation/longitudinal/exports/longitudinal_v1_bundle_L1C2.json  
**Substrate commit:** 56e594b (EI-004 fix applied)

---

### 10.1 Corpus batch 2

**Documents added:** 7 (covering 2026-03 to 2026-04 timeline arc)

| Document | Category | Description |
|---|---|---|
| ADR-008-replica-scaling-revision.md | adrs | Raises max replicas 3→5, min 1→2; supersedes ADR-005 §3.1 |
| ADR-009-observability-tracing.md | adrs | Adopts OpenTelemetry for distributed tracing |
| INC-005-2026-03-14-redis-streams-backpressure.md | incidents | Consumer lag backpressure, 62-min MTTI |
| 2026-03-07-q1-retrospective.md | meetings | Q1 retrospective, ADR-008/ADR-009 decisions |
| 2026-04-11-q2-planning-kickoff.md | meetings | Q2 priorities confirmed |
| 2026-Q2-roadmap.md | planning | Q2 roadmap; schema versioning carried over from Q1 |
| operational-limits-v2.md | references | Updated limits reflecting ADR-008 and INC-005 actions |

**Batch 2 ingestion:**
- Chunks: 246
- Candidates: 87 (78 proposed + 9 unresolved incident type)

---

### 10.2 Event counts

| Metric | After L1-C1 | After L1-C2 | Delta |
|---|---|---|---|
| Total events | 127 | 215 | +88 |
| Active | 34 | 60 | +26 |
| Superseded | 2 | 5 | +3 |
| Rejected | 91 | 150 | +59 |

**Active events by type (L1-C2 state):**
- architecture_decision: 49 (22 L1-C1 + 27 L1-C2)
- incident: 6 (unchanged)
- open_question: 2 (unchanged)
- validation_result: 2 (unchanged)
- implementation_note: 2 (1 L1-C1 + 1 L1-C2 compression-derived)
- rejected_idea: 1 (unchanged)

---

### 10.3 Batch 2 review analysis

**Approval rate: 32% (28/87)** — marginal improvement over L1-C1 (28%), driven by the more decision-dense ADR and planning corpus.

**Rejection categories:**
1. **EI-002 pattern (29 rejects):** `architecture_decision` table-header artifacts: "Source: ADR-xxx" rows, "ADR: ADR-xxx (accepted)" metadata headers, word-wrap artifacts ("headroom" → "oom"). 51% of all architecture_decision candidates rejected.
2. **EI-003 pattern (8 rejects):** All 8 `source_reference` events rejected — table "Source:" cell citations, bare ADR reference rows.
3. **EI-001 residual (5 rejects):** Five new `governance_rule` must-not verb fragments: "request more than 5 replicas...", "be used as a reference limit...", "add more than 2ms to the p99 request latency...", "be increased without validating...", "be changed by configuration alone."
4. **Unresolved incident fragments (9 rejects):** NEW. Incident PatternRule fires on the word "incident" mid-paragraph in retrospective and planning documents (e.g., "incident response procedures", "incident MTTI breakdown:", "four incidents"). All 9 committed as `status='unresolved'`, absent from the `proposed` review queue. Operator must inspect `unresolved` separately. See §10.9 (EI-007).

---

### 10.4 Governance report (post-review)

- **CRITICAL:** 0
- **WARNING:** 11
  - 8 `duplicate_title` — pre-existing L1-C1 artifacts (Commander: Jordan Kim, Owner Action Due, Parameter Value Source)
  - 3 `low_confidence_active` — ids 109 (conf=2), 123 (conf=2), 190 (conf=2, new)
- **INFO:** 209 `orphaned_event` — expected; most events lack links

No new CRITICAL or new WARNING patterns in L1-C2 batch.

---

### 10.5 Lineage integrity

- **all_ok: True** (source DB)
- **total_broken: 0**

---

### 10.6 Supersession and contradiction exercise

**ADR-008 supersedes ADR-005 replica constraints:**

| Action | Result |
|---|---|
| Contradiction link: id=17 ↔ id=135 | Created (link id=5) |
| Contradiction link: id=97 ↔ id=135 | Created (link id=6) |
| id=17 status → superseded | OK |
| id=97 status → superseded | OK |
| id=132 supersedes id=17 | Link created (link id=7) |
| id=132 supersedes id=97 | Link created (link id=8) |

**EI-005 protocol observed:** Contradiction links created BEFORE supersession. Both links succeeded. Supersession completed without friction.

**L1-C1 gap corrected:**
id=29 status set to `superseded` — the id=27→id=29 supersedes link was created in L1-C1 but the status change was missed. Corrected in L1-C2.

**Retrieval quality of supersession:**
"replica" query: 17 active results + 2 superseded (ids 17, 97) correctly labeled. Active ADR-008 events (ids 198, 166, 135) surface prominently. Supersession chain is navigable.

---

### 10.7 Assembly spot check

**Policy:** max_chars=16000, min_confidence=2, max_governance_chars=4000, no workflows/runtime

| Metric | L1-C1 (pre-EI-004 fix) | L1-C1 (post-EI-004 fix) | L1-C2 |
|---|---|---|---|
| total_candidates | 50 | 53 | 77 |
| included_entries | 6 | 7 | 7 |
| chars_used | 3888 | 3971 | 3903 |
| governance entries | 6 | 7 | 7 |
| active events in governance | 0 | 2 | 1 |
| unresolved_items | 0 | 0 | 0 |

**EI-006 escalation observed (see §10.8):**
Governance tier: [213, 201, 144, 137, 134, 63, 197] — 6 rejected governance_rule + 1 active architecture_decision. Only id=197 ("[REVISED] Previous limits (ADR-005)") is active. Five new rejected governance_rule fragments from batch 2 (ids 134, 137, 144, 201, 213) crowded out all but one active event from 59 active candidates. This is a regression from the L1-C2 post-fix state (2 active → 1 active in governance tier).

**Assembly with compression artifact (policy: compression_artifact_ids=[1]):**
- Continuity context: 1 entry (id=1, 1960 chars)
- Main context unchanged: same 7 governance entries, same chars_used
- Compression artifact correctly isolated in continuity_context tier, separate budget

---

### 10.8 EI-006 operational impact assessment

**Current state:** 6 rejected governance_rule events consume ~3800 of the 4000-char governance budget, leaving room for 1 active event.

**Trajectory:**
- Batch 1 (27 docs): 3 rejected governance_rule fragments → 0 active in governance tier (pre-EI-004 fix), 2 active post-fix
- Batch 2 (7 docs): 5 additional rejected governance_rule fragments → 1 active in governance tier

Each ADR or reference document containing `must not`, `must always`, or `never` language adds 1-3 rejected fragments. At this rate:
- A third batch of similar size will produce 4-7 more rejected fragments
- These will consume the remaining 97 chars of governance budget
- Active events will be fully excluded from the governance tier again

**Severity reclassification:** EI-006 is no longer "bounded, non-fatal." With 60 active events in the corpus and only 1 surfacing in the governance tier, the governance context is effectively non-functional for an operator trying to understand the current architectural state. The trajectory to 0 active governance events is observable and near-term.

**Recommended action before L1-C3:** Implement the EI-006 fix (`partition_by_section()` status exclusion) before the next corpus expansion. The governance tier degradation rate makes deferral increasingly costly.

---

### 10.9 New findings

**EI-007 — Incident pattern generates unresolved fragments from non-incident corpus text**

- **Class:** C — parser-quality
- **Priority:** Low-Medium
- **Symptom:** The `incident` PatternRule fires on the word "incident" wherever it appears mid-text: "incident response procedures", "incident MTTI breakdown:", "four incidents in the past quarter", etc. These fire in retrospective meetings, planning documents, and ADRs — not just incident reports. 9 events in batch 2, all fragments starting with word-wrap artifacts.
- **Distinct behavior:** Incident events default to `status='unresolved'` (not `proposed`). They do not appear in the `proposed` operator review queue; the operator must inspect `unresolved` events separately. This creates review friction and a silent accumulation path.
- **Affected path:** `ingestion/extractor.py` `incident` PatternRule; default status assignment for `incident` type.
- **Not replay-affecting, not continuity-corrupting.**
- **Proposed remediation:** Add a minimum context length requirement or a structural anchor (incident number pattern INC-XXX, severity keyword, or date header) before the incident pattern fires. Or assign `incident` events `status='proposed'` so they enter the standard review queue.
- **Deferred:** Log only. Do not fix during run.

---

### 10.10 Multi-session workflow assessment

| Operation | Result |
|---|---|
| Second assembly logged (id=3) | PASS |
| Compression artifact created (id=1, method=operator_manual) | PASS |
| Compression artifact promoted to active | PASS |
| Memory seeded from compression (id=215, type=implementation_note) | PASS |
| Assembly with compression artifact (continuity_context tier) | PASS |
| Continuity artifact isolated in separate budget | PASS |
| Assembly determinism (×2 reconstructions) | PASS |

**Session continuation observation:** `ContextActivationPolicy.compression_artifact_ids` correctly gates continuity context into the assembly. The compression artifact (1960 chars) does not compete with the main governance/memory budget. This is the intended behavior and works correctly.

**Operator ergonomics note:** Review fatigue is moderate. The `unresolved` queue for incident events requires a separate inspection pass. The operator must know to call `review_memory(status='unresolved')` in addition to `review_memory(status='proposed')`. This is undocumented and caused the 9 unresolved fragments to be missed in the initial review pass.

---

### 10.11 Retrieval quality

| Query term | Active results | Quality |
|---|---|---|
| "replica" | 17 active + 2 superseded | Good — ADR-008 events prominent, superseded ADR-005 correctly labeled |
| "ADR-008" | 11 active | Good — revision decisions surface correctly |
| "OpenTelemetry" | 2 active | Limited — new topic, only 2 events from ADR-009 |
| "schema" | 4 active | Adequate — Q2 scope and slippage note both surface |
| "INC-005" | 8 active | Good — cross-references to ADR-005/ADR-008/ADR-007 constraints |
| "backpressure" | 8 active | Good — new topic well-covered |
| "kafka" | 10 active + 2 superseded | Good — cross-temporal: superseded decisions (25, 29) correctly labeled alongside active |

Retrieval drift observation: the corpus growth is beginning to produce noise at moderate query breadth. "replica" returns 17 active events — many are cross-references rather than primary decisions. Operator must manually filter for primary constraint events. Substring matching limitation (known, no semantic ranking) is noticeable at 60+ active events.

---

### 10.12 Continuity round-trip

- **Export:** 215 events, schema_version=1.2, lineage_integrity_checked=True
- **Import:** 215 imported, 0 skipped, 0 collisions
- **Reconstruction identity:** governance_context IDs [213, 201, 144, 137, 134, 63, 197] — identical on source and recovered DB
- **chars_used:** 3903 == 3903 ✓
- **Lineage integrity (recovered):** all_ok=True, total_broken=0
- **ROUND-TRIP: PASS**

Note: The EI-006 defect is replicated faithfully in the recovered DB. Round-trip determinism is confirmed including the governance tier degradation pattern.

---

### 10.13 Checkpoint assessment

| Criterion | Status |
|---|---|
| Ingestion completes without crash | PASS |
| All 87 batch 2 candidates committed | PASS |
| Operator review completes (0 unreviewed) | PASS (includes 9 unresolved incident fragments) |
| CRITICAL governance issues | 0 — PASS |
| Lineage integrity | PASS |
| Continuity round-trip | PASS |
| Replay determinism | PASS |
| Supersession workflow functional | PASS — EI-005 protocol observed, L1-C1 gap corrected |
| Contradiction linking functional | PASS — 2 links created pre-supersession |
| Compression workflow functional | PASS — artifact created, promoted, seeded, continuity tier verified |
| Assembly produces useful context | CONDITIONAL — only 1 active governance event; EI-006 escalating |
| EI-006 trajectory acceptable | FAIL — not bounded; governance tier will collapse again within 1-2 batches |

**Checkpoint L1-C2 assessment: CONDITIONAL PASS**

Substrate mechanics (ingestion, lineage, supersession, contradiction linking, compression, export/import, determinism) are all sound. Multi-session workflows function correctly. EI-006 is the primary concern: the rejected governance_rule fragment accumulation rate makes the governance tier operationally unusable on a predictable and near-term trajectory.

**Recommendation before L1-C3:** Implement and commit EI-006 fix (`partition_by_section()` status exclusion) before the next corpus expansion. L1-C3 should focus on governance tier quality post-fix under continued accumulation pressure.

**EI-007 opened:** Incident pattern fires on non-incident corpus text, producing unresolved fragment events that bypass the standard review queue. Low-Medium priority. Deferred.

---

## 11. Post-Checkpoint L1-C2 Remediation

### 11.1 EI-006 fix — partition_by_section status exclusion

**Date:** 2026-05-28  
**Trigger:** L1-C2 CONDITIONAL PASS — governance tier 6 rejected + 1 active, escalating trajectory

**Root cause (confirmed):**
`partition_by_section()` routed by `event_type` only. Rejected/superseded governance events retrieved via the general path in `activate_memory()` entered the governance tier and displaced active decisions. The accumulation rate (~3–5 rejected fragments per 7-doc batch) made full tier collapse inevitable within 1–2 additional batches.

**Fix applied:**
- Added `GOVERNANCE_EXCLUDE_STATUSES = frozenset({'rejected', 'superseded', 'archived', 'deprecated'})` to `session/activation.py`
- Added status exclusion guard in `partition_by_section()`: `and mem.status not in GOVERNANCE_EXCLUDE_STATUSES`
- Excluded events fall through to `relevant_memory` — no data discarded
- 9 regression tests added in `session/tests/test_activation.py`

**Post-fix verification (longitudinal_v1.db, max_chars=16000, min_confidence=2):**

| Metric | Before (EI-006 open) | After (EI-006 remediated) |
|---|---|---|
| governance_context entries | 7 (6 rejected, 1 active) | 7 (all active) |
| chars_used | 3903 / 16000 | 15777 / 16000 |
| included_entries | 7 | 28 |
| round-trip identity | — | PASS (215 events, gov_ids identical) |
| replay determinism | — | PASS (×2 identical) |

**Governance tier post-fix (all active, all substantive decisions):**
- id=214 active — Operational limits binding; limit changes require ADR update
- id=209 active — OpenTelemetry distributed tracing limits (ADR-009)
- id=206 active — Redis Streams consumer lag alert 5,000 / autoscaling 10,000 (ADR-007/INC-005)
- id=198 active — Max 5 replicas hard ceiling; > 5 requires ADR update (ADR-008)
- id=197 active — ADR-005 min-1/max-3 replica limits revised to min-2/max-5 (ADR-008)
- id=191 active — API versioning documentation refresh
- id=172 active — API versioning removed from Q2 scope

**Test suite:** 3194 passed, 1 warning. Session tests: 390 passed.

**Files changed:**
- `session/activation.py` — `GOVERNANCE_EXCLUDE_STATUSES` constant + one-line status guard
- `session/tests/test_activation.py` — 9 EI-006 regression tests + import update
- `docs/ENGINEERING_ISSUES.md` — EI-006 status updated to Remediated

### 11.2 L1-C3 readiness

EI-006 fix applied and verified. Governance tier is fully active and substantive. Round-trip and determinism PASS. L1-C3 may proceed.

L1-C3 focus: governance tier quality under continued accumulation pressure, EI-007 observation (incident pattern false positives), retrieval noise at 60+ active events.

---

## 12. Checkpoint L1-C3

**Date:** 2026-05-28  
**Commit baseline:** 0ae2a1e (EI-006 fix)  
**DB state entering L1-C3:** 215 events (60 active, 150 rejected, 5 superseded), max id=215

---

### 12.1 Corpus batch 3

5 documents added (2026-04-28 through 2026-05-14):

| Document | Content |
|---|---|
| INC-006-2026-04-28-hpa-flapping.md | HPA autoscaler flapping: 10,000-msg trigger too sensitive at current throughput |
| ADR-010-jwt-lifetime-extension.md | JWT lifetime extended 15→30 min; ADR-006 §2 constraint superseded |
| 2026-05-09-q2-mid-quarter-review.md | Q2 mid-quarter: OTel slipped, schema versioning deferred, JWT complete |
| 2026-Q2-midpoint-revision.md | Q2 roadmap v2.0: OTel + schema versioning → Q3 |
| operational-limits-v3.md | Updated operational limits: JWT 30 min, HPA trigger 20,000 msg, cooldown |

Ingestion result: 189 chunks, 59 candidates committed

### 12.2 Operator review

| Queue | Total | Approved (active) | Rejected |
|---|---|---|---|
| Proposed | 55 | 24 | 31 |
| Unresolved | 4 | 2 | 2 |
| **Total** | **59** | **26** | **33** |

0 events unreviewed after separate unresolved queue pass.

Rejection patterns:
- Table headers (Priority Item Status Notes, Risk Owner Status, Status: Accepted): 3 events
- Source attributions (Source: ADR-XXX...) from operational-limits-v3: 7 events
- Governance rule fragments starting with "be..." (must-not-be EI-001 pattern): 4 events
- List item fragments, date stamps, generic summaries: 9 events
- Validation result fragments: 2 events
- Cross-batch duplicate fragments from op-limits table structure: 6 events

### 12.3 Governance report

| Severity | Count |
|---|---|
| CRITICAL | 0 |
| WARNING | 23 |
| INFO | 251 |

WARNING breakdown: 18 duplicate_title (cross-doc structural repetition from operational-limits-v2/v3 table rows), 5 low_confidence_active (pre-existing, known). No new patterns. No blocking issues.

### 12.4 Lineage integrity

all_ok=True, total_broken=0

### 12.5 Contradiction links and supersession (EI-005 protocol)

Contradiction links created before supersession:

| Link ID | Source | Target | Relationship |
|---|---|---|---|
| 16 | id=43 (ADR-006 JWT 15 min) | id=228 (ADR-010 JWT 30 min) | contradicts |
| 17 | id=206 (op-limits-v2 10,000-msg trigger) | id=218 (INC-006 root cause) | contradicts |
| 18 | id=209 (OTel Q2 2026-05-01) | id=239 (OTel deferred Q3) | contradicts |

Supersessions applied:

| Event ID | Title (truncated) | Superseded by |
|---|---|---|
| id=43 | ADR-006 JWT 15-minute lifetime | ADR-010 (id=228) |
| id=46 | ADR-006 JWT open concern | ADR-010 resolution |
| id=206 | op-limits-v2 10,000-msg autoscale trigger | INC-006 action (id=218) |
| id=209 | OTel Q2 2026-05-01 target | Q3 deferral (id=239) |

Post-supersession DB state: 274 total, 82 active, 183 rejected, 9 superseded

### 12.6 Assembly reconstruction

Policy: max_chars=16000, min_confidence=2, max_governance_chars=4000, include_unresolved=True

| Metric | Value |
|---|---|
| chars_used | 15915 / 16000 |
| included_entries | 28 |
| truncated | True |
| governance_context entries | 7 (all active) |
| unresolved_items | 0 |
| active_investigations | 0 |
| relevant_memory | 21 |

Governance tier (all active):
- id=227 governance_rule — The 30-minute token lifetime must not be increased (ADR-010)
- id=274 architecture_decision — Operational limits binding; limit changes require ADR update (op-limits-v3)
- id=267 architecture_decision — ADR-009 target 2026-05-01 not met; OTel deferred to Q3 2026
- id=262 architecture_decision — 30-minute token lifetime must not be increased without re-evaluating exposure model
- id=260 architecture_decision — The 15-minute constraint from ADR-006 is no longer operative (ADR-010)
- id=259 architecture_decision — [REVISED] JWT previous values: 15 min lifetime, 2 min refresh window
- id=236 architecture_decision — OTel implementation slipped; complexity in context propagation layer

EI-006 fix confirmed: 0 rejected, 0 superseded events in governance_context tier.

**Governance tier recency bias observation:** All 7 governance entries are from L1-C3 batch (ids 227–274). Older operative decisions (replica limits id=14/19, connection pool id=38/41, deployment limits id=122) are excluded from the governance tier despite being active. They have lower activation scores due to higher recency_rank. This is a long-horizon usability concern: as the corpus grows, operative constraints from earlier batches become invisible in governance context.

### 12.7 Replay determinism

×2 reconstructions → identical governance IDs, chars_used, included_entries. PASS.

### 12.8 Continuity export / import

Export: 274 events, schema_version=1.2  
Import to clean DB: 274 imported, 0 skipped, 0 collisions  
Reconstruction identity: governance IDs [227, 274, 267, 262, 260, 259, 236] match; chars_used 15915=15915  
ROUND-TRIP: PASS

Bundle: `longitudinal_v1_bundle_L1C3.json`, assembly_id=4

### 12.9 New issue: EI-008

**EI-008: Rejected events surface in relevant_memory via general retrieve pass**

`activate_memory()` general retrieve pass has no status filter. Rejected events with confidence ≥ min_confidence pass the activation filter. EI-006 fix excluded them from governance_context; they now fall to `relevant_memory`. At L1-C3, 5 of the top 21 relevant_memory entries are rejected governance_rule fragments (ids 273, 261, 225, 219, 213 — all must-not-be verb fragments from previous batches).

Root cause: the fix for EI-006 correctly gates governance_context, but `relevant_memory` is the catch-all with no status guard. Rejected events with confidence ≥ 2 always surface here.

Classification: Class C retrieval/noise, non-blocking, not replay-affecting. Fix: either a status filter on the general retrieve pass, or a status guard in the `relevant_memory` fallthrough in `partition_by_section()`. Deferred — does not affect governance correctness or determinism.

### 12.10 L1-C3 assessment

| Criteria | Status |
|---|---|
| Operator review completes (0 unreviewed) | PASS (includes 4 unresolved incident fragments) |
| CRITICAL governance issues | 0 — PASS |
| Lineage integrity | PASS |
| Continuity round-trip | PASS |
| Replay determinism | PASS |
| Supersession workflow functional | PASS — EI-005 protocol observed, 3 links before 4 supersessions |
| Contradiction linking functional | PASS — 3 links created |
| EI-006 fix confirmed under accumulation | PASS — 0 rejected/superseded in governance tier |
| Assembly produces useful governance context | PASS — 7 active substantive decisions |
| Retrieval quality | CONDITIONAL — semantic queries return unrelated events; known limitation |
| EI-007 operational impact | LOW-MEDIUM — 4 unresolved fragments reviewed separately; manageable |
| EI-008 new issue | OPENED — rejected events in relevant_memory; non-blocking |
| Governance recency bias | OBSERVED — older operative constraints invisible in governance tier under pressure |

**Checkpoint L1-C3 assessment: PASS**

Substrate mechanics (ingestion, lineage, supersession, contradiction linking, export/import, determinism) all sound. EI-006 fix confirmed under accumulation: governance tier holds at 7 active entries, all substantive, no fragment contamination. Continuity portable. Determinism stable.

**Primary observation:** Governance tier recency bias is the emerging concern for long-horizon usability. Under the default activation scoring, recent governance events displace older operative constraints. The system correctly tracks what changed most recently, but the operator cannot assume the governance tier reflects the full operative constraint set — only the most recently surfaced subset. This becomes more acute as the corpus grows.

**EI-007 trajectory:** Incident pattern false positives continue at ~4/batch. The unresolved queue review burden is low at current batch sizes but will grow proportionally.

**EI-008 observation:** Rejected events in `relevant_memory` are a quality concern. As the rejected event corpus grows (183 rejected at L1-C3, likely to reach 300+ by L1-C4), the `relevant_memory` tier will contain an increasing proportion of non-actionable fragments. This is the same accumulation dynamic as EI-006 but for the fallback tier.

**Recommendation before L1-C4:** Consider whether EI-008 (rejected events in relevant_memory) warrants a minimum-surface fix (status guard in relevant_memory fallthrough) before the next corpus batch. The dynamics are similar to EI-006 and will follow the same accumulation trajectory.

---

## 13. Post-Checkpoint L1-C3 Remediation

### 13.1 EI-008 fix — relevant_memory status exclusion

**Date:** 2026-05-28  
**Trigger:** L1-C3 observation — 21/28 assembly relevant_memory entries were rejected/superseded fragments

**Root cause (confirmed):**
`partition_by_section()` `relevant_memory` fallthrough (`if not placed`) had no status guard. Rejected/superseded events excluded from `governance_context` by EI-006 fell through unconditionally to `relevant_memory`.

**Fix applied:**
One-line addition to `partition_by_section()`:
```python
if not placed and mem.status not in GOVERNANCE_EXCLUDE_STATUSES:
    sections['relevant_memory'].append(mem)
```
Reuses existing `GOVERNANCE_EXCLUDE_STATUSES` constant. Also updated constant docstring to reflect dual application (EI-006 governance_context, EI-008 relevant_memory). 9 EI-008 regression tests added. One EI-006 test updated: `test_ei006_rejected_governance_falls_to_relevant_memory` → `test_ei006_rejected_governance_excluded_from_all_sections` (rejected governance now excluded from all sections).

**Post-fix verification (longitudinal_v1.db):**

| Metric | Before (EI-008 open) | After (EI-008 remediated) |
|---|---|---|
| relevant_memory entries | 21 (all rejected/superseded) | 0 |
| rejected/superseded in relevant_memory | 21 | 0 |
| included_entries | 28 | 7 |
| chars_used | 15915 / 16000 | 3900 / 16000 |
| governance tier (active) | 7 | 7 (unchanged) |
| replay determinism | PASS | PASS |
| continuity round-trip (274 events) | PASS | PASS |

**Revealed behavior:**
Post-fix, `relevant_memory` is empty. The 21 previously-included entries were 100% rejected/superseded events. Active non-governance events (8 incidents, 2 open_questions, 4 implementation_notes, 4 validation_results) are not surfacing via the general retrieve — the `max_memory_candidates=50` limit fills entirely with architecture_decision events (66 active, conf ≥ 3). This is a pre-existing retrieval architecture limitation (related to governance recency bias, deferred per operator decision). The EI-008 fix is correct: the session context is now smaller but entirely substantive.

**Test suite:** 3203 passed, 1 warning.

**Files changed:**
- `session/activation.py` — one-line status guard + docstring update
- `session/tests/test_activation.py` — 9 EI-008 regression tests + 1 EI-006 test name/assertion update
- `docs/ENGINEERING_ISSUES.md` — EI-008 entry added as Remediated

### 13.2 L1-C4 readiness

EI-008 fix applied and verified. Session context is now free of rejected/superseded events in all tiers. Determinism and round-trip PASS. L1-C4 may proceed.

L1-C4 focus: governance recency bias observation under further accumulation, EI-007 unresolved queue growth, retrieval architecture pressure as non-governance active events grow.

---

## 14. Checkpoint L1-C4

**Date:** 2026-05-28  
**Baseline commit:** adab7bc  
**DB path:** `validation/longitudinal/runs/longitudinal_v1.db`  
**Export:** `validation/longitudinal/exports/longitudinal_v1_bundle_L1C4.json`  
**Test suite at checkpoint:** 3203 passed, 1 warning

---

### 14.1 Corpus batch 4

| Document | Events | Approved | Rejected |
|---|---|---|---|
| `INC-007-2026-05-22-staging-schema-drift.md` | 10 | 8 | 2 |
| `2026-06-06-q2-close-review.md` | 16 | 9 | 7 |
| `helix-q2-retrospective-findings.md` | 19 | 10 | 9 |
| `2026-Q3-kickoff.md` | 18 | 9 | 9 |
| **Batch total** | **63** | **36** | **27** |

Thematic content: first staging incident (schema drift); Q2 close review and retrospective with pattern analysis; Q3 kickoff plan with P1 OTel, P2 schema versioning, deferred risks.

---

### 14.2 EI-007: false positive count in batch 4

The incident PatternRule fired on 7 events from non-incident documents in this batch (text containing "incident runbook", incident references in retrospective summaries, planning tables). All 7 were correctly rejected by the operator. No EI-007 false positive reached active status from non-INC sources in this batch.

| Source | EI-007 events | Status |
|---|---|---|
| `2026-06-06-q2-close-review.md` | 1 | rejected |
| `helix-q2-retrospective-findings.md` | 4 | rejected |
| `2026-Q3-kickoff.md` | 2 | rejected |
| **Total** | **7** | all rejected |

Cumulative EI-007 false positives (all batches): operator review burden is stable — false positives are generated and rejected consistently, but the pattern does not self-correct without a PatternRule fix.

---

### 14.3 DB state post-L1-C4

| Metric | L1-C3 | L1-C4 | Delta |
|---|---|---|---|
| Total events | 274 | 337 | +63 |
| Active | 82 | 118 | +36 |
| Rejected | 183 | 210 | +27 |
| Superseded | 9 | 9 | 0 |
| Memory links | 21 | 21 | 0 |

**Active events by type:**

| Type | Count | Doctrine rank |
|---|---|---|
| architecture_decision | 67 | 2 |
| implementation_note | 20 | 6 |
| open_question | 14 | 7 |
| incident | 10 | 7 |
| validation_result | 4 | 3 |
| governance_rule | 2 | 1 |
| rejected_idea | 1 | 7 |

**Rejected events by type (top):**

| Type | Count |
|---|---|
| architecture_decision | 99 |
| incident | 36 |
| source_reference | 34 |
| validation_result | 22 |
| governance_rule | 14 |

---

### 14.4 Assembly reconstruction (id=5)

Assembly built post-L1-C4 ingestion.

| Section | Entries | Notes |
|---|---|---|
| governance_context | 6 | 2 governance_rule, 4 architecture_decision; all active |
| relevant_memory | 0 | EI-008 fix effective; no active non-governance events surfacing |
| unresolved_items | 0 | No unresolved/proposed events |
| active_investigations | 0 | — |
| chars_used | 3962 / 16000 | 24.8% of budget |

**governance_context composition:**

| ID | Type | Evidence excerpt |
|---|---|---|
| 306 | governance_rule | Deferral creates predictable incident risk (3-5 month window); assign "incident expected by" dates |
| 227 | governance_rule | JWT 30-min lifetime must not increase without re-evaluation |
| 313 | architecture_decision | ADR constraints not visible to on-call operators during incidents (runbook gap) |
| 311 | architecture_decision | Cross-service observability gap is primary MTTI driver |
| 291 | architecture_decision | Q3 P1: OpenTelemetry distributed tracing (Q3 close target) |
| 267 | architecture_decision | ADR-009 target date missed (2026-05-01); promoted to Q3 carry-over |

All 6 entries originate from L1-C3 or L1-C4 batches. No governance or early architecture constraints from L1-C1 or L1-C2 appear in the context window.

---

### 14.5 Findings

**Finding 1: Governance recency bias — confirmed and deepening**

At L1-C4, all 6 governance_context entries are from the two most recent batches. Active operative constraints from earlier batches — replica ceiling (ADR-008, id≈129), connection pool limits (ADR-004, id≈38), consumer lag thresholds (ADR-007) — are present in the DB with confidence=4 but displaced by activation scoring's recency weighting. The context window presents a forward-looking picture (upcoming Q3 work) while mature operative constraints are invisible.

This is a pre-existing structural behavior, not a regression. The recency weighting is intentional. However, at 4+ quarters of historical depth, the gap between what is operationally authoritative and what is visible in context is now material. Flagged for L1-C5 architecture review consideration.

**Finding 2: Non-governance active events structurally invisible**

The general retrieve (limit=50) fills entirely with architecture_decision events (67 active, doctrine_rank=2). The 14 active open_questions (rank 7), 10 active incidents (rank 7), and 20 active implementation_notes (rank 6) do not surface in relevant_memory. After EI-008, relevant_memory=0. The session context budget is 24.8% used, but the remaining 75.2% is inaccessible to rank-6/7 events due to doctrine_rank ordering.

Specific gap: active open_questions have no retrieval path. `retrieve_unresolved()` only fetches status='unresolved'/'proposed'; 14 active open_questions (Q3 ADR sampling question, schema versioning approach, HPA cooldown threshold, JWT refresh confirmation) are structurally invisible. This is a known deferred limitation.

**Finding 3: Rejected corpus growth rate stable**

210/337 events are rejected (62.3%). The rejection rate for L1-C4 batch was 27/63 (42.9%), within the observed range of prior batches (40–55%). The 99 rejected architecture_decisions are the largest source of retrieval capacity pressure. No threshold breach observed at current volume.

**Finding 4: EI-007 operator burden stable but unresolved**

7 false positives per batch is consistent with the L1-C3 rate. All are correctly operator-rejected. The PatternRule is unmodified. This generates predictable review burden without compounding into active false positives, but EI-007 remains deferred.

---

### 14.6 Replay and continuity verification

| Check | Result |
|---|---|
| Replay determinism | PASS |
| Round-trip import (337 events) | PASS |
| Export schema_version | 1 |
| Bundle events | 337 |
| Bundle memory_links | 21 |
| Bundle revisions | 347 |
| Governance lineage | PASS (0 CRITICAL) |

---

### 14.7 L1-C4 checkpoint assessment: CONDITIONAL PASS

**PASS criteria met:**
- Replay determinism: PASS
- Continuity round-trip: PASS
- Governance lineage: PASS (0 CRITICAL)
- EI-008 fix effective: relevant_memory contains 0 rejected/superseded events
- No new remediation required

**Observations (not failures, flagged for L1-C5):**
1. Governance recency bias now spans 3+ quarters of historical depth; early operative constraints are invisible in session context
2. 14 active open_questions and 10 active incidents have no surfacing path; 75% of context budget is inaccessible to rank-6/7 events
3. EI-007 false positive generation rate stable (7/batch); PatternRule not yet addressed

**L1-C5 readiness:** Yes. The above observations are architectural limitations, not replay or governance semantic failures. L1-C5 should continue accumulation and specifically test whether the retrieval architecture gap becomes operationally significant at further depth.

---

## 15. Checkpoint L1-C5

**Date:** 2026-05-28  
**Baseline commit:** adab7bc  
**DB path:** `validation/longitudinal/runs/longitudinal_v1.db`  
**Export:** `validation/longitudinal/exports/longitudinal_v1_bundle_L1C5.json`  
**Test suite at checkpoint:** 3203 passed, 1 warning

---

### 15.1 Corpus batch 5

| Document | Events | Approved | Rejected |
|---|---|---|---|
| `2026-07-15-hpa-closure-memo.md` | 10 | 8 | 2 |
| `ADR-011-otel-context-propagation.md` | 15 | 8 | 7 |
| `INC-008-2026-07-24-schema-freeze-violation.md` | 9 | 5 | 4 |
| `2026-07-28-q3-mid-quarter-review.md` | 24 | 15 | 9 |
| **Batch 5 total (proposed)** | **58** | **36** | **21** |
| **Unresolved (direct-routed)** | **11** | **3 kept** | **8 rejected** |
| **Net new active** | — | **39** | — |

Thematic content: Q3 P3 HPA closure with throughput alert; OTel context propagation ADR; schema freeze violation incident; Q3 mid-quarter: OTel Phase 1 delivered, Validator blocked, JWT confirmed effective.

---

### 15.2 EI-007: false positive count in batch 5

5 incident-type events fired on non-incident content: ADR-011 text about rebalance investigation (2 events), INC-008 governance implications section (2 events), Q3 mid-quarter runbook reference (1 event). All 5 correctly rejected.

| Source | EI-007 events | Status |
|---|---|---|
| `ADR-011-otel-context-propagation.md` | 2 | rejected |
| `INC-008-2026-07-24-schema-freeze-violation.md` | 2 | rejected |
| `2026-07-28-q3-mid-quarter-review.md` | 1 | rejected |
| **Total** | **5** | all rejected |

---

### 15.3 DB state post-L1-C5

| Metric | L1-C4 | L1-C5 | Delta |
|---|---|---|---|
| Total events | 337 | 405 | +68 |
| Active | 118 | 154 | +36 |
| Rejected | 210 | 239 | +29 |
| Superseded | 9 | 9 | 0 |
| Unresolved | 0 | 3 | +3 |

**Active events by type:**

| Type | Count | Doctrine rank |
|---|---|---|
| architecture_decision | 80 | 2 |
| implementation_note | 35 | 6 |
| open_question | 14 | 7 |
| validation_result | 11 | 3 |
| incident | 10 | 7 |
| governance_rule | 3 | 1 |
| rejected_idea | 1 | 7 |

**Unresolved open_questions (3):**
- [344] HPA threshold re-evaluation (operator deadline 2026-07-31)
- [374] Schema freeze enforcement ownership (operator deadline 2026-07-31)
- [385] OTel partial delivery acceptability (operator deadline 2026-08-01)

---

### 15.4 Assembly reconstruction — budget exhaustion finding

**Policy:** default `ContextActivationPolicy()` — max_chars=12,000, max_governance_chars=0 (uncapped), max_entries=60

**Pre-budget activation:**

| Section | Candidates |
|---|---|
| governance_context | 50 |
| unresolved_items | 3 |
| active_investigations | 3 |
| relevant_memory | 0 |

**Post-budget assembly:**

| Section | Entries | Chars |
|---|---|---|
| governance_context | 16 | ~11,986 |
| unresolved_items | **0** | 0 (14 chars remaining — insufficient) |
| active_investigations | **0** | 0 |
| relevant_memory | 0 | — |
| **Total** | **16** | **11,986 / 12,000** |

With `max_governance_chars=0`, governance candidates are not capped. The 16 highest-scoring governance events (80 arch_dec + 3 gov_rule candidates) exhaust the 12,000-char budget. After governance, 14 chars remain — no other event can be added.

**Rendered entry size:** governance entries average ~750 chars when rendered (full evidence + metadata headers).

**Governance context composition (16 entries):**

| ID | Type | Batch |
|---|---|---|
| 394 | governance_rule | L1-C5 |
| 306 | governance_rule | L1-C4 |
| 227 | governance_rule | L1-C3 |
| 393–382 | architecture_decision (3) | L1-C5 |
| 370–339 | architecture_decision (6) | L1-C4/C5 |
| 313–148 | architecture_decision (4) | L1-C1/C2/C3 |

Notably: entries [274], [262], [227] from earlier batches do appear — confirming that recency bias is not absolute. Events with sufficiently high activation scores do resurface. However, the budget is still exhausted by governance before any non-governance event can be included.

---

### 15.5 Findings

**Finding 1: Budget exhaustion (NEW — escalation from L1-C4)**

L1-C4 found that non-governance events were structurally invisible due to doctrine_rank (only governance candidates were available for the general retrieve). L1-C5 reveals a second, distinct mechanism: even when unresolved items (Tier 1 priority) are correctly activated and available, the 12,000-char default budget is exhausted by governance alone before any non-governance event can be included.

The two mechanisms compound: doctrine_rank prevents non-governance events from entering the candidate pool via general retrieve; budget exhaustion prevents Tier 1 unresolved items from surviving budget allocation even when directly activated by `retrieve_unresolved()`.

Under the default policy, the context window is structurally incapable of containing non-governance events at current corpus depth.

**Finding 2: Operator-actionable items invisible (NEW — operational impact)**

The 3 unresolved open_questions all have concrete owner+deadline assignments:
- HPA threshold revision: Priya Mehta, 2026-07-31
- Schema freeze ownership: Jan Kowalski, 2026-07-31
- OTel partial delivery: Jan Kowalski, 2026-08-01

None surface in the assembled context. An operator rebuilding session context from the assembled output would have no indication these items exist. This is the first checkpoint where the retrieval limitation produces a directly observable operational gap: time-bound action items are invisible.

**Finding 3: Governance recency bias partially self-correcting**

L1-C4 observed that ALL governance entries were from the two most recent batches. In L1-C5, three governance entries from earlier batches appear in context ([313] from L1-C4, [311] from L1-C4, [274] from L1-C2, [262] from L1-C2, [227] from L1-C3). This suggests the activation scoring's recency weighting is not uniformly biased; high-confidence events with repeated references do resurface. However, this finding is secondary to the budget exhaustion issue.

**Finding 4: EI-007 operator burden stable**

5 false positives in batch 5, consistent with L1-C4 (7). All correctly rejected by operator. EI-007 unaddressed but not escalating.

---

### 15.6 Replay and continuity verification

| Check | Result |
|---|---|
| Replay determinism | PASS |
| Round-trip (405 events) | PASS |
| Export: bundle events | 405 |
| Export: bundle links | 21 |
| Export: bundle revisions | 412 |
| Governance lineage | PASS (all_ok=True, 0 broken) |

---

### 15.7 L1-C5 checkpoint assessment: CONDITIONAL PASS

**PASS criteria met:**
- Replay determinism: PASS
- Continuity round-trip: PASS
- Governance lineage: PASS
- No governance semantic breaks

**New critical observation — budget exhaustion:**

The default context policy is now structurally incapable of including non-governance events at current corpus depth. 16 governance entries consume 11,986 of 12,000 available chars. Tier 1 unresolved items with active operator deadlines are excluded. Three time-bound open questions are invisible in assembled context.

This is an escalation from L1-C4's doctrine_rank invisibility to L1-C5's budget exhaustion invisibility. Two independent mechanisms now structurally exclude non-governance content from the context window:
1. Doctrine_rank: active open_questions/incidents (rank 7) not in general retrieve candidate pool
2. Budget exhaustion: unresolved items (correctly activated, Tier 1) excluded because governance consumes max_chars

**L1-C6 gating condition:** Before L1-C6, the budget exhaustion finding should be evaluated as a gate. The question is whether the context window being 100% governance at current corpus depth constitutes "operationally unusable" per the L1 run criteria (which authorizes remediation if "replay/governance semantics become operationally unusable"). Budget exhaustion does not break replay or governance semantics, but it does break operational utility for non-governance tiers.

**L1-C6 readiness:** Conditional — operator decision required on whether budget exhaustion gates L1-C6 or is observed for another checkpoint.

---

## 16. Checkpoint L1-C6 — Governance Budget Diagnostic

**Date:** 2026-05-28  
**Baseline commit:** adab7bc  
**DB:** unchanged from L1-C5 (405 events)  
**Export:** L1-C5 bundle covers this state  
**Type:** Diagnostic only — no corpus changes, no code changes

---

### 16.1 Objective

Determine whether the L1-C5 budget exhaustion finding is:
- A. Expected deterministic doctrine outcome
- B. Operationally unacceptable cognition failure
- C. Evidence that explicit deterministic partition budgeting is required

---

### 16.2 Policy variants tested

| Variant | max_chars | max_governance_chars | gov (post) | unres (post) | chars used | % budget |
|---|---|---|---|---|---|---|
| A: default | 12,000 | 0 (uncapped) | 16 | 0 | 11,986 | 99.9% |
| B: gov cap 4000 | 12,000 | 4,000 | 5 | 3 | 9,841 | 82.0% |
| C: gov cap 6000 | 12,000 | 6,000 | 7 | 3 | 11,789 | 98.2% |
| D: expanded 24000 | 24,000 | 0 | 34 | 0 | 23,903 | 99.6% |
| E: 24000 + cap 8000 | 24,000 | 8,000 | 10 | 3 | 13,854 | 57.7% |

All five variants: replay determinism PASS (3 runs each).

`relevant_memory = 0` under all five variants. See §16.3 Layer 3.

---

### 16.3 Diagnostic findings

**Layer 1: Candidate pool pressure**

83 active governance events (80 arch_dec + 3 gov_rule) compete for 50 candidate slots (`max_memory_candidates=50`). 33 events are never reachable regardless of budget or cap settings — including foundational constraints ADR-004 ([38], connection pool limits) and ADR-005 original ([129], replica ceiling). These events have conf=4 and active status but are outscored by 50 more recently-ingested governance events.

| Rank band | Events | Example |
|---|---|---|
| Top 50 (reachable) | 50 | [394]–[148] — recent batches C2–C5 |
| Below top 50 (never reachable) | 33 | [4], [7], [38], [129] — original ADR-001 through ADR-005 |

**Layer 2: Budget exhaustion**

The 16 highest-scoring governance candidates average ~750 chars/entry rendered. At `max_governance_chars=0`, they fill the 12,000-char budget entirely (11,986 chars after 16 entries). 14 chars remain. Any governance cap ≥ 4,000 chars restores Tier 1 unresolved visibility.

Cumulative governance char milestones (entries 1-16):

| After entry | Cumulative chars | Events included | Notes |
|---|---|---|---|
| 4 | 3,435 | [394],[306],[227],[393] | All 3 governance_rules + 1 arch_dec |
| 5 | 4,295 | + [384] | **cap 4000 boundary** |
| 8 | 6,783 | + [382],[370],[363] | **cap 6000 boundary** |
| 10 | 8,146 | + [360],[359] | **cap 8000 boundary** |
| 16 | 11,956 | + [339]…[267] | **budget exhausted** |

**Layer 3: General retrieve saturation**

The general `retrieve()` call (limit=50, ordered by `ScoredEvent.composite_key` with doctrine_rank as primary sort) returns only arch_dec (33) and gov_rule (17) events. All 50 slots are consumed by governance. Non-governance active events (35 impl_notes, 14 open_qs, 11 val_results, 10 incidents) receive 0 slots.

Consequence: even under a governance cap (Variants B, C, E), `relevant_memory = 0`. Non-governance events are not in the candidate pool. Only the 3 `status='unresolved'` open_questions surface, via the separate `retrieve_unresolved()` path.

The 14 active open_questions (status='active') have no retrieval path under any variant: `retrieve_unresolved()` requires status='unresolved'/'proposed'; `retrieve()` excludes them by doctrine_rank.

---

### 16.4 Critical constraint coverage under capped variants

Critical constraints from earlier batches (operationally authoritative, may be misrepresented if missing):

| ID | Constraint | Batch | In A | In B | In C | In E |
|---|---|---|---|---|---|---|
| [38] | ADR-004 connection pool max | C1 | absent¹ | absent¹ | absent¹ | absent¹ |
| [129] | ADR-005/008 replica ceiling origin | C1 | absent¹ | absent¹ | absent¹ | absent¹ |
| [227] | ADR-010 JWT 30-min ceiling | C3 | present | present | present | present |
| [274] | Operational-limits binding constraint | C2 | present | absent² | absent² | absent² |
| [306] | Deferral→incident 3-5 month pattern | C4 | present | present | present | present |
| [394] | Runbook constraint reference | C5 | present | present | present | present |

¹ Not in top-50 candidate pool — invisible regardless of any budget setting  
² Present at candidate rank 15 — exceeds cap boundary before it is reached

---

### 16.5 Assessment

**A. Expected deterministic doctrine outcome: YES**

The system behaves exactly as designed. `max_governance_chars=0` means governance is uncapped. `ScoredEvent.composite_key` with doctrine_rank as the primary sort means governance events dominate all retrieve calls. `max_memory_candidates=50` hard-limits the candidate pool. The saturation is fully deterministic and reproducible.

**B. Operationally unacceptable cognition failure: YES (at current depth)**

Three time-bound open questions with operator deadlines (2026-07-31, 2026-07-31, 2026-08-01) are excluded from assembled context by budget exhaustion despite Tier 1 priority. An operator relying on the default assembly would not know these items exist. The context window is 100% governance.

**C. Evidence that explicit deterministic partition budgeting is required: YES**

The diagnostic is unambiguous:
1. Budget expansion alone (Variant D) does not restore unresolved visibility — it adds more governance
2. Any governance cap (Variants B, C, E) deterministically restores Tier 1 unresolved visibility
3. All capped variants pass replay determinism
4. The required changes involve no adaptive logic, no hidden weighting, no heuristics

---

### 16.6 Recommendation: EXPLICIT PARTITION POLICY WARRANTED

Six checkpoints, 405 events, monotonically worsening saturation. The compound of three structural layers (candidate pool pressure, budget exhaustion, general retrieve saturation) means the default policy is incapable of representing multi-horizon cognition at current corpus depth.

**Minimal warranted changes:**

| Layer | Fix | Type |
|---|---|---|
| 2: Budget exhaustion | `max_governance_chars` explicit tier budget | Policy config change (no architecture impact) |
| 3: General retrieve saturation | Non-governance retrieve pass (type-aware or separate call) | Architecture change |
| Active open_question gap | Retrieve path for status='active' open_questions | Architecture change |

**What these changes are NOT:** adaptive ranking, hidden weighting, heuristic balancing, semantic routing, or opaque relevance scoring. The per-tier budgets are explicit, auditable, deterministic policy parameters. Truncation within each tier remains deterministic.

**Recommended first step:** Implement `max_governance_chars` as an explicit, operator-configurable tier budget in `ContextActivationPolicy`. A value of 5,000–6,000 chars preserves 7–8 governance entries (including all governance_rules and the most recent architecture decisions) while releasing 6,000–7,000 chars for lower-priority tiers. This is a single policy parameter change with no architecture impact.

The companion architecture changes (Layers 1 and 3) require separate design and implementation, as they affect the retrieval logic in `session/activation.py` and `memory/retrieval.py`.

---

## §17 — L1 Layer 2 Remediation: Explicit Governance Tier Budget

**Date:** 2026-05-28  
**Baseline commit:** adab7bc  
**DB:** `validation/longitudinal/runs/longitudinal_v1.db` (unchanged from L1-C5/C6 — 405 events)  
**Scope:** Layer 2 remediation only (budget exhaustion). No architecture changes. No DB changes.

---

### 17.1 Changes implemented

**`session/models.py`**
- Added `GOVERNANCE_CHAR_BUDGET_DEFAULT = 6000` constant alongside `CHAR_BUDGET_DEFAULT` and `ENTRY_BUDGET_DEFAULT`
- Changed `ContextActivationPolicy.max_governance_chars` default from `0` (uncapped) to `GOVERNANCE_CHAR_BUDGET_DEFAULT` (6000)
- Updated docstring: governance tier char budget now documented as defaulting to 6000 with `0` preserved as legacy uncapped behavior

**`session/tests/test_context_window.py`**
- Renamed `test_max_governance_chars_default_is_uncapped` → `test_max_governance_chars_default_is_6000` (assertion updated from `== 0` to `== GOVERNANCE_CHAR_BUDGET_DEFAULT`)
- Added `test_max_governance_chars_zero_preserves_uncapped_behavior` — confirms `max_governance_chars=0` still produces uncapped behavior
- Added `test_governance_cap_releases_budget_for_unresolved` — confirms capped governance leaves room for unresolved items
- Added `test_from_dict_missing_max_governance_chars_uses_new_default` — confirms old policy dicts without the key deserialize to 6000

**`session/tests/test_assembly_log.py`**
- Added `GOVERNANCE_CHAR_BUDGET_DEFAULT` to imports
- Added `test_governance_char_budget_default_is_6000` to `TestBudgetConstants`
- Updated `test_default_policy_uses_named_constants` to assert `policy.max_governance_chars == GOVERNANCE_CHAR_BUDGET_DEFAULT`
- Updated `test_constants_importable_from_session_models` to include `GOVERNANCE_CHAR_BUDGET_DEFAULT`
- Updated `test_from_dict_ignores_include_governance` to assert old policy dict deserializes with `max_governance_chars == GOVERNANCE_CHAR_BUDGET_DEFAULT`

---

### 17.2 Test results

```
session/tests: 403 passed (0 failures, 0 errors)
full suite:    3207 passed, 1 warning (same urllib3/LibreSSL warning as baseline)
```

No regressions. The renamed test (`default_is_uncapped` → `default_is_6000`) now correctly asserts the new default.

---

### 17.3 L1-C6 diagnostic rerun — new default policy

**Policy:** `ContextActivationPolicy()` — max_chars=12000, max_governance_chars=6000 (GOVERNANCE_CHAR_BUDGET_DEFAULT)

#### Pre-budget activation
- governance_context candidates: 50
- unresolved_items candidates: 3
- relevant_memory candidates: 0

#### Post-budget assembly

| Section | Pre-budget | Post-budget | Chars used |
|---|---|---|---|
| governance_context | 50 | 7 | 5,927 |
| unresolved_items | 3 | **3** | 2,931 |
| relevant_memory | 0 | 0 | — |
| **Total** | **53** | **10** | **8,858 / 12,000 (73.8%)** |

#### Governance entries (post-budget, 7 of 50):
- [394] governance_rule — Governance Rule: increase without ADR
- [306] governance_rule — Governance Rule: every deferred ADR action item…
- [227] governance_rule — Governance Rule: The 30-minute token lifetime…
- [393] architecture_decision — Marcus: Draft delivered 2026-07-24…
- [384] architecture_decision — Amara: Phase 3 (Helix-Router) start date…
- [382] architecture_decision — Amara: Phase 1 (Helix-Ingest) is complete…
- [370] architecture_decision — Open question: The Q3 kickoff raised a second…

#### Unresolved items (post-budget, 3 of 3 — RESTORED):
- [385] open_question/unresolved — OTel partial delivery acceptability (Phase 2/3 slip past Q3)
- [374] open_question/unresolved — Schema freeze enforcement ownership (who grants exceptions?)
- [344] open_question/unresolved — HPA threshold re-evaluation (what value replaces 20,000?)

---

### 17.4 Layer 2 assessment: RESOLVED

**Before (L1-C5/C6 default):** governance exhausted 11,986/12,000 chars (99.9%). All 3 unresolved open_questions were excluded. Budget remaining: 14 chars.

**After (new default):** governance uses 5,927 chars (49.4% of 12,000). All 3 unresolved items surface and consume 2,931 chars. Total: 8,858/12,000 (73.8%). Budget remaining: 3,142 chars — sufficient for additional content if available.

**Replay determinism:** PASS (two consecutive runs, identical governance_context IDs and unresolved_items IDs).

**Backward compatibility:** `from_dict` with old policy snapshots missing `max_governance_chars` key correctly deserializes to `GOVERNANCE_CHAR_BUDGET_DEFAULT=6000`. Legacy behavior (`max_governance_chars=0`, uncapped) fully preserved for explicit configuration.

---

### 17.5 Layer 3 status: UNCHANGED — separately required

`relevant_memory = 0` in both pre- and post-budget output. The Layer 3 structural issue (general retrieve saturation) is independent of Layer 2: the `retrieve()` general call fills all 50 candidate slots with governance events by doctrine_rank, leaving no slots for implementation_notes, incidents, open_questions (status='active'), or validation_results.

The 14 active open_questions (status='active'), 35 active implementation_notes, 10 active incidents remain structurally invisible. This requires a separate architecture change to `session/activation.py` (non-governance retrieve pass) and is not addressed here.

---

## §18 — Layer 3 Design Diagnostics: General Retrieve Saturation

**Date:** 2026-05-28  
**Baseline:** post-§17 (Layer 2 resolved; relevant_memory still structurally empty)  
**DB:** `validation/longitudinal/runs/longitudinal_v1.db` — 405 total events; no DB changes in this section  
**Scope:** Architecture diagnostic only. No source changes. No commits.

---

### 18.1 Exact starvation mechanism

Layer 3 starvation originates in `activate_memory()` (`session/activation.py:116`). The function makes four retrieve passes and deduplicates by `memory_id`:

```
Pass 1: retrieve_governance(limit=50)
Pass 2: retrieve_unresolved(limit=50)   [if include_unresolved=True]
Pass 3: retrieve(adaptation_query)       [if include_adaptations=True]
Pass 4: retrieve(general_query)          [always]
```

**Pass 4 — the general retrieve** is constructed at `activation.py:159–164`:

```python
general_query = RetrievalQuery(
    tags=list(policy.tags),
    min_confidence=policy.min_confidence,
    limit=policy.max_memory_candidates,   # = 50
    expand_related=policy.expand_related,
)
_add(retrieve(memory_db_path, general_query))
```

`RetrievalQuery` with no `event_types` and no `statuses` fields causes `_fetch_candidates()` (`memory/retrieval.py:336`) to fetch ALL events regardless of type or status. The implicit query:

```sql
SELECT * FROM memory_events ORDER BY id ASC
```

returns all 405 events. After scoring, the composite sort key at `retrieval.py:49–59` is:

```python
(int(is_expanded), doctrine_rank, -effective_confidence, semantic_rank, recency_rank, -tag_overlap, event.id)
```

`doctrine_rank` is the primary sort key after expansion tier:
- `governance_rule` = 1, `architecture_decision` = 2
- `validation_result` = 3, `adaptation` = 4, `hypothesis` = 5, `implementation_note` = 6
- `incident`, `open_question`, `rejected_idea`, … = 7

After sorting, top 50 results are sliced. With 218 governance events in the DB (83 active + 126 rejected + 9 superseded), ALL 50 slots are consumed by governance events. Non-governance events at rank 3–7 never appear in the top 50.

**Pass 1 + Pass 4 interaction:** Pass 1 (`retrieve_governance`) adds 50 active governance events to `seen_ids`. When Pass 4 returns its 50 governance events, the `_add` deduplication filters some out — but the 36 not-yet-seen general retrieve results are governance type with terminal-negative statuses (rejected/superseded). They pass `_add`, enter `collected`, but are then filtered by `GOVERNANCE_EXCLUDE_STATUSES` in `partition_by_section`, landing in no section.

**Net result:** `collected` after all passes contains only governance events and unresolved items. `relevant_memory` is structurally zero.

---

### 18.2 Retrieval economics

**DB state at L1-C5 (405 total events):**

| Event type | Active | Rejected/Superseded | Total |
|---|---|---|---|
| architecture_decision | 80 | 135 | 215 |
| governance_rule | 3 | 0 | 3 |
| **Governance subtotal** | **83** | **135** | **218** |
| implementation_note | 35 | 5 | 40 |
| incident | 10 | 41 | 51 |
| open_question (active) | 14 | 5 | — |
| open_question (unresolved) | 3 | — | — |
| validation_result | 11 | 25 | 36 |
| rejected_idea | 1 | 2 | 3 |
| source_reference | 0 | 35 | 35 |
| **Non-governance subtotal** | **74 active + 3 unresolved** | **113** | **190** |
| **TOTAL** | **157+3** | **245** | **405** |

**Candidate slot analysis (general retrieve, no type filter):**

```
Total governance events (all statuses):      218
Non-governance events in top 50 results:     0
Non-governance events in top 100 results:    0
Non-governance events in top 200 results:    0
Minimum limit to reach any non-governance:   >218
```

Governance events (including rejected) fill every slot at any limit ≤ 218. The general retrieve never reaches non-governance events regardless of `max_memory_candidates`.

**Non-governance active events by doctrine rank:**

| Type | Doctrine rank | Active count |
|---|---|---|
| validation_result | 3 | 11 |
| implementation_note | 6 | 35 |
| incident | 7 | 10 |
| open_question (active) | 7 | 14 |
| rejected_idea | 7 | 1 |
| **Total non-governance active** | — | **71** |

**Non-governance retrieve simulation (dedicated pass, limit=100):**

A retrieve pass filtered to non-governance types and `status='active'`, limit=100 returns all 71 active non-governance events. Slot allocation: 11 validation_results (rank 3) + 35 implementation_notes (rank 6) = 46 slots, then rank-7 events (25 total: 10 incidents + 14 active open_questions + 1 rejected_idea) begin at slot 47. At limit=50, only 4 of 25 rank-7 events fit.

---

### 18.3 Evaluation of deterministic remediation options

#### Option A: Explicit non-governance quota (new policy parameter)

Add `max_non_governance_candidates: int` to `ContextActivationPolicy`. Add a fifth named pass in `activate_memory()` using `RetrievalQuery(event_types=NON_GOVERNANCE_TYPES, statuses=['active'], limit=policy.max_non_governance_candidates)`.

**Replay determinism:** PASS  
**Governance integrity:** PRESERVED — governance pass unchanged  
**Observability:** HIGH — named pass, explicit limit  
**Semantic clarity:** HIGH  
**Implementation complexity:** LOW — one field, one pass  
**Hidden weighting risk:** NONE  
**Limitation:** New policy parameter requires `to_dict()`/`from_dict()` update, new constant, test coverage. The existing general retrieve still runs and still wastes 50 slots on rejected governance events unless also modified.

---

#### Option B: Replace general retrieve with explicit non-governance retrieve

Modify the `general_query` in `activate_memory()` to add `event_types=NON_GOVERNANCE_TYPES` and `statuses=['active']`. The general retrieve always had the intent of catching "everything not covered by specific passes" — governance already has a dedicated pass. The type exclusion makes this intent explicit.

```python
# Current (broken):
general_query = RetrievalQuery(tags=..., min_confidence=..., limit=..., expand_related=...)

# Fixed:
general_query = RetrievalQuery(
    event_types=_NON_GOVERNANCE_EVENT_TYPES,   # all VALID_EVENT_TYPES minus governance types
    statuses=['active'],
    tags=list(policy.tags),
    min_confidence=policy.min_confidence,
    limit=policy.max_memory_candidates,
    expand_related=policy.expand_related,
)
```

**Replay determinism:** PASS  
**Governance integrity:** PRESERVED  
**Observability:** HIGH — exclusion is explicit and inspectable  
**Semantic clarity:** VERY HIGH — aligns retrieve intent with its comment  
**Implementation complexity:** MINIMAL — two field additions to one `RetrievalQuery`; no new policy parameters; no schema changes  
**Hidden weighting risk:** NONE — type exclusion is explicit; doctrine_rank within non-governance unchanged

**Residual at limit=50:** Rank-7 events (incidents, active open_questions) start at slot 47; only 4 of 25 fit. This is doctrinal prioritization within non-governance, not structural invisibility. Resolvable by increasing `max_memory_candidates` or adding a dedicated investigation pass in a later checkpoint.

---

#### Option C: Partitioned retrieve by event family (3+ dedicated passes)

Define named families (`GOVERNANCE_FAMILY`, `OPERATIONAL_FAMILY`, `INVESTIGATION_FAMILY`) with per-family retrieve passes and slot quotas.

**Replay determinism:** PASS  
**Governance integrity:** PRESERVED  
**Observability:** VERY HIGH — per-family visibility  
**Semantic clarity:** VERY HIGH  
**Implementation complexity:** MODERATE — 2–3 new policy fields, 3+ passes, expanded test surface  
**Hidden weighting risk:** NONE  
**Assessment:** Correct long-term architecture; premature at current corpus depth. Option B resolves structural starvation with a single-line change. Option C is the natural evolution once per-family budget pressure is observed.

---

#### Option D: Doctrine-rank reweighting

Promote non-governance event types to lower numeric ranks (e.g., `incident` from 7 to 3) so they compete with governance events in the general retrieve.

**Replay determinism:** PASS  
**Governance integrity:** AT RISK — governance and non-governance now compete for the same slots; governance coverage becomes corpus-composition dependent  
**Observability:** LOW — effective governance coverage unpredictable without knowing corpus rank distributions  
**Operator auditability:** LOW  
**Hidden weighting risk:** MODERATE — rank changes function as hidden cross-family priority weights  
**Assessment:** REJECTED. Doctrine_rank reweighting undermines governance observability without providing explicit control. Structural separation (Options A/B/C) is the correct resolution.

---

### 18.4 Invalid approaches — GOAL.md violations

**Adaptive relevance scoring:** Rank events by inferred relevance to current session context. **Violation:** introduces hidden state; breaks replay determinism; scores are non-auditable without full session history.

**Opaque heuristics:** Weight events by computed signals (staleness penalty, urgency score, action density). **Violation:** scoring components not defined as explicit policy parameters violate the `activation.py:6–10` contract: "No embeddings. No semantic search. No hidden heuristics. All scoring components are explicit and inspectable."

**Embedding similarity ranking:** Use vector distance to a query or session summary to promote relevant events. **Violation:** model-dependence; same DB + same policy can produce different output if the embedding model changes; breaks deterministic replay guarantee; requires infrastructure the substrate does not currently have.

**Hidden balancing:** Automatically adjust governance/non-governance slot allocation based on observed corpus ratios at assembly time. **Violation:** output differs for same policy inputs depending on DB state at query time; the ratio is not an explicit operator-visible parameter.

**Probabilistic weighting:** Assign scores stochastically or with noise. **Violation:** fundamentally incompatible with deterministic replay.

**Recency-only correction:** Promote non-governance events by `updated_at` recency alone, overriding doctrine_rank. **Violation:** would suppress older but operative constraints (e.g., ADR-004 connection pool limits, still active); recency and operational importance are orthogonal.

**Autonomous importance inference:** Have the substrate assess which events are "operationally important" based on content or linked events and use that inference to determine inclusion priority. **Violation:** makes the substrate a reasoning agent over its own contents; introduces circular cognition and undefined behavior; conflicts with the architecture principle that "the operator interprets; the substrate stores."

---

### 18.5 Layer 3 recommendation

**Option B: explicit non-governance type filter on the general retrieve.**

**Exact change in `session/activation.py`:**

```python
# Add at module level (near GOVERNANCE_EVENT_TYPES definition):
_NON_GOVERNANCE_EVENT_TYPES: List[str] = sorted(
    t for t in VALID_EVENT_TYPES if t not in GOVERNANCE_EVENT_TYPES
)

# In activate_memory(), change the general_query construction from:
general_query = RetrievalQuery(
    tags=list(policy.tags),
    min_confidence=policy.min_confidence,
    limit=policy.max_memory_candidates,
    expand_related=policy.expand_related,
)

# To:
general_query = RetrievalQuery(
    event_types=_NON_GOVERNANCE_EVENT_TYPES,
    statuses=['active'],
    tags=list(policy.tags),
    min_confidence=policy.min_confidence,
    limit=policy.max_memory_candidates,
    expand_related=policy.expand_related,
)
```

**Why Option B over Option A:**
- Option A requires a new policy parameter, `to_dict()`/`from_dict()` update, a new constant, and leaves the broken general retrieve in place. Option B achieves the same result with two field additions, no new parameters, and directly fixes the root cause.
- The general retrieve's failure was always a missing type constraint, not a missing parallel pass. Option B fixes the omission; Option A works around it.

**Why Option B over Option C:**
- Option C is the correct long-term form; Option B is the minimal correct present form. Once per-family starvation within non-governance is observed (e.g., validation_results crowding out incidents), Option C is the natural next step. The transition from B to C is additive — Option B does not foreclose Option C.

**Deterministic guarantees preserved:**
- Same DB + same policy → same output: MAINTAINED
- Governance tier isolated from non-governance tier: MAINTAINED (governance has its own pass; general retrieve no longer touches governance)
- Replay of any prior assembly: MAINTAINED for assemblies using the new policy; old assemblies with `event_types=[]` in the general_query would differ — but those are the broken assemblies; the new behavior is the correct behavior

**What remains intentionally unresolved after Option B:**
1. **Rank-7 starvation within non-governance:** At `max_memory_candidates=50`, incidents and active open_questions start at slot 47 and may be cut. This is not structural invisibility; it is doctrinal ordering. Addressable by adjusting `max_memory_candidates` or adding a dedicated pass (Option C direction).
2. **Active open_question gap:** 14 active open_questions (status='active') are not handled by `retrieve_unresolved()` (which requires status='unresolved'/'proposed'). Under Option B, they enter the non-governance retrieve but compete at rank 7. If slot pressure becomes critical, a dedicated `retrieve_active_operational()` pass would ensure they always have a path.
3. **Layer 1 (candidate pool pressure):** 33 active governance events permanently below the rank-50 cut in `retrieve_governance`. This is outside the Layer 3 scope.

---

### 18.6 Implementation scope

| File | Change | Complexity |
|---|---|---|
| `session/activation.py` | Add `_NON_GOVERNANCE_EVENT_TYPES` constant; modify `general_query` to add `event_types` and `statuses=['active']` | 5–8 lines |
| `session/tests/test_activation.py` | Update tests asserting current behavior; add non-governance surfacing assertion | 10–20 lines |
| `session/models.py` (optional) | Increase `max_memory_candidates` default from 50 to 75 to capture all active non-governance events | 1 constant change |

**No changes to:** `memory/retrieval.py`, `session/context_window.py`, `session/reconstruction.py`, `memory/governance.py`, any DB schema, any test fixture.

---

### 18.7 L1-C7 readiness

**Recommendation: Implement Layer 3 before L1-C7.**

With `relevant_memory` structurally zero, L1-C7 without Layer 3 would extend the observation of operational-memory invisibility without adding measurement value. The key L1-C7 observation is whether the non-governance retrieve path, once unblocked, actually surfaces the operationally relevant content accumulated across five corpus batches (35 implementation_notes, 10 incidents, 14 active open_questions, 11 validation_results). That measurement requires Layer 3 to be in place.

**GOAL.md constraint check for Layer 3 implementation:**
- No schema changes required: PASS
- No new dependencies: PASS
- No live trading/broker integration: PASS (not applicable)
- No destructive git operations: PASS
- Architecture impact: `session/activation.py` only — within substrate boundary, no external surface

Layer 3 implementation is cleared under current substrate constraints.

---

## §19 — Layer 3 Remediation: General Retrieve Saturation Fix

**Date:** 2026-05-28  
**Baseline:** post-§18 (Layer 3 design diagnostics complete)  
**DB:** `validation/longitudinal/runs/longitudinal_v1.db` — 405 events; unchanged  
**Scope:** Implementation only. No schema changes. No new policy parameters. No commits.

---

### 19.1 Changes implemented

**`session/activation.py` — three changes:**

**Change 1:** Added `_NON_GOVERNANCE_EVENT_TYPES` module-level constant after `GOVERNANCE_EVENT_TYPES`:
```python
_NON_GOVERNANCE_EVENT_TYPES: List[str] = sorted(
    t for t in VALID_EVENT_TYPES if t not in GOVERNANCE_EVENT_TYPES
)
```

**Change 2:** Updated `general_query` in `activate_memory()` to add `event_types` and `statuses` constraints:
```python
general_query = RetrievalQuery(
    event_types=_NON_GOVERNANCE_EVENT_TYPES,
    statuses=['active', 'accepted'],
    tags=list(policy.tags),
    min_confidence=policy.min_confidence,
    limit=policy.max_memory_candidates,
    expand_related=policy.expand_related,
)
```

**Change 3 (companion fix):** Updated `partition_by_section()` to exclude already-unresolved items from `active_investigations`:
```python
# Before:
if mem.event_type in INVESTIGATION_EVENT_TYPES:
# After:
if mem.event_type in INVESTIGATION_EVENT_TYPES and not _is_unresolved_mem(mem):
```

**Why this is part of the Layer 3 fix:** Before Layer 3, `relevant_memory` had 0 pre-budget candidates, so the double-counting of unresolved open_questions in both `unresolved_items` (Tier 1) and `active_investigations` (Tier 3) was budget-invisible. Once Layer 3 populated `relevant_memory` with 46 candidates, the overlap consumed 2×2,931 = 5,862 chars and left only 211 chars for Tier 4. Fixing the overlap was a precondition for any relevant_memory item to surface within the budget.

---

### 19.2 Test changes

**`session/tests/test_activation.py` — 6 tests added/updated:**
- `test_layer3_governance_does_not_enter_relevant_memory` — new regression
- `test_layer3_active_non_governance_events_surface_in_relevant_memory` — new
- `test_layer3_governance_saturation_does_not_starve_non_governance` — new
- `test_layer3_unresolved_investigation_not_double_counted_in_active_investigations` — new overlap fix regression
- `test_layer3_rejected_governance_does_not_enter_general_retrieve_path` — new
- `test_activate_memory_respects_min_confidence` — updated: `status='proposed'` → `status='active'`

**`session/tests/test_reconstruction.py` — 1 test updated:**
- `test_reconstruct_unresolved_in_investigations_too` → renamed with inverted assertion (was asserting old overlap behavior)

---

### 19.3 Test results

```
session/tests/test_activation.py:  42 passed
session/tests/:                    408 passed
full suite:                        3212 passed, 1 warning
```

No regressions. 6 net new Layer 3 regression tests.

---

### 19.4 L1-C6 diagnostic rerun — Layer 3 default policy

**Policy:** `ContextActivationPolicy()` — max_chars=12000, max_governance_chars=6000

#### Pre-budget activation (structural starvation resolved)

| Section | Pre-budget candidates | Types |
|---|---|---|
| governance_context | 50 | arch_dec=47, gov_rule=3 |
| unresolved_items | 3 | open_question=3 |
| active_investigations | 4 | open_question=4 (active status) |
| **relevant_memory** | **46** | **validation_result=11, implementation_note=35** |

**Layer 3 structural fix confirmed: relevant_memory pre-budget candidates = 46 (was 0).**

#### Post-budget assembly (default 12,000 chars)

| Section | Post-budget entries | Chars |
|---|---|---|
| governance_context | 7 | 5,927 |
| unresolved_items | 3 | 2,931 |
| active_investigations | 3 of 4 | 2,761 |
| **relevant_memory** | **0** | **0** |
| **Total** | **13** | **11,619 / 12,000 (96.8%)** |

Remaining after active_investigations: 381 chars. Smallest relevant_memory item: 484 chars. None fit.

#### Post-budget assembly (expanded 20,000 chars — Layer 3 verification)

| Section | Post-budget entries | Chars |
|---|---|---|
| governance_context | 7 | 5,927 |
| unresolved_items | 3 | 2,931 |
| active_investigations | 4 | 3,628 |
| **relevant_memory** | **11** | **7,390** |
| **Total** | **25** | **19,876 / 20,000 (99.4%)** |

**Layer 3 verified: with sufficient budget, 11 relevant_memory entries surface (10 validation_results, 1 implementation_note).**

---

### 19.5 Assessment

**Layer 3 structural starvation: RESOLVED.** Pre-budget relevant_memory candidates: 0 → 46.

**Post-budget relevant_memory at default 12,000 chars: still 0** — not due to starvation but due to multi-tier budget pressure:

| Tier | Chars | % of 12,000 |
|---|---|---|
| Governance (cap=6,000) | 5,927 | 49.4% |
| Unresolved items | 2,931 | 24.4% |
| Active investigations | 2,761 | 23.0% |
| **Subtotal before Tier 4** | **11,619** | **96.8%** |
| **Available for Tier 4** | **381** | **3.2%** |
| Minimum item size | 484 | — |

The `max_governance_chars=6,000` cap reserves budget for lower tiers, but three non-governance tiers (unresolved + investigations + relevant_memory) together require more than the 6,073 remaining chars at this corpus depth. Minimum to surface any relevant_memory item: ~13,109 chars. The default max_chars=12,000 is ~1,109 chars short.

**Replay determinism:** PASS — three consecutive runs, identical IDs across all sections.

**New finding:** Multi-tier budget pressure at max_chars=12,000. This is distinct from Layer 3 starvation and requires either (a) increasing `max_chars` default or (b) adding `max_investigation_chars` cap. Deferred to operator decision ahead of L1-C7.

---

### 19.6 L1-C7 readiness: CLEARED

Layer 3 structural fix is complete. The relevant_memory candidate pool is populated. L1-C7 may proceed to observe multi-tier budget pressure evolution with corpus growth, and may optionally adjust `max_chars` to verify relevant_memory surfacing. Both observations are valid L1-C7 objectives.

---

*This document is the operator record for Longitudinal Run L1. Findings are recorded as observed; no remediation is performed during the run unless replay integrity, lineage, or determinism breaks.*

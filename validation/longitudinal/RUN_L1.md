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

*This document is the operator record for Longitudinal Run L1. Findings are recorded as observed; no remediation is performed during the run unless replay integrity, lineage, or determinism breaks.*

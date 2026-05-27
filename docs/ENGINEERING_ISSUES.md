# Engineering Issue Backlog

Tracked here: deferred defects and improvement requests that are non-blocking for the current substrate baseline but require engineering resolution before the next major validation milestone.

**Substrate baseline:** 9245ee7  
**Last updated:** 2026-05-27 (EI-004 remediated; EI-006 opened)

---

## Classification key

- **Class C** — ingestion quality defect: substrate mechanics intact, parser/extractor output incorrect
- **non-blocking** — does not block sign-off or operation of the current baseline
- **deferred** — explicitly deferred from Run #1 and Run #2 remediation scope
- **parser-quality** — root cause is in `ingestion/extractor.py`, `ingestion/candidates.py`, or pattern rules
- **not replay/governance defect** — does not affect lineage integrity, continuity portability, or governance detection

---

## EI-001 — governance_rule pattern over-extraction

**Class:** C — parser-quality  
**Priority:** Medium  
**Status:** Partially mitigated — root cause mechanism fixed; secondary fragment sources remain  
**Opened:** 2026-05-27 (identified Run #1, confirmed Run #2)  
**Remediation applied:** 2026-05-27 — `ingestion/extractor.py` + 9 regression tests in `ingestion/tests/test_extractor.py`

**Symptom:**
`governance_rule` events are extracted with titles that are mid-paragraph sentence fragments. Examples from Run #2 corpus:
- "Governance Rule: evaluation was single-threaded."
- "Governance Rule: via Kubernetes rollout strategy."
- "Governance Rule: flow into staging."
- "Governance Rule: set is currently 47 rules."

These pass the 15-char content filter (Fix 2) because they are long enough, but they are not standalone governance statements — they are clauses extracted from the middle of a larger sentence.

**Root cause:**
The `PatternRule` for `governance_rule` uses the pattern `rule[:\s]+([^\n]{5,120})`. This captures any text after the word "rule" on the same line, including continuations of prior sentences where "rule" appears mid-clause (e.g., "the routing rule evaluates the..." → captures "evaluates the...").

**Remediation applied (2026-05-27):**
Narrowed the four bare-word alternations in `_build_rules()` to require an explicit colon:
- `rule[:\s]+` → `rule:\s*`
- `policy[:\s]+` → `policy:\s*`
- `constraint[:\s]+` → `constraint:\s*`
- `governance\s+rule` → `governance\s+rule:\s*`

This eliminates triggering on "the routing rule evaluates...", "rule set is...", "rule evaluation was..." and any other mid-sentence usage where "rule" appears without an explicit statement prefix colon.

Nine regression tests added covering: mid-sentence suppression (4 tests) and colon-form preservation (5 tests). All 3177 tests pass.

**Remaining fragment sources (not fixed by this remediation):**
Spot-check of the docs corpus (25 files) after the fix shows 19 `governance_rule` candidates. Fragments persist from two distinct mechanisms not addressed by EI-001:

1. **`must not` + infinitive verb capture**: patterns like "must not be the weakest link in the audit chain." produce titles starting with a bare infinitive verb. The captured text is technically the prohibited action (which is useful) but the standalone title reads as a sentence fragment. Example: "Governance Rule: be the weakest link in the audit chain." These require a `must not` prefix to be interpretable.

2. **`keyword` trigger on poorly-titled chunks**: the `KeywordRule` for "no live capital" fires on a chunk whose first sentence is "1." (a numbered list item), producing "Governance Rule: 1." The keyword is correct but `_title_from_text()` picks up the list numbering as the first sentence. This is a title-extraction quality problem distinct from pattern over-triggering.

3. **Code block extraction**: `policy:` or `constraint:` preceding Python class attributes (e.g., `Policy:\n    events_per_snapshot: int = 50`) fires and extracts the code line as the title. This overlaps with the EI-002 class of structured-content extraction failures.

**Acceptance criterion (original):**
On the Run #2 frozen corpus, the number of governance_rule events with fragmentary titles drops to < 20% of all governance_rule events. Status: **not verified** against the Run #2 corpus (source markdown files not available for re-extraction). Targeted root cause mechanism is eliminated; acceptance criterion requires re-measurement on the next corpus run.

**Regression guard:**
`test_governance_rule_must_pattern` and `test_governance_rule_keyword_no_live_capital` in `ingestion/tests/test_extractor.py` continue to pass. 9 new regression tests added for the narrowed pattern.

---

## EI-002 — architecture_decision metadata row extraction

**Class:** C — parser-quality  
**Priority:** Low  
**Status:** Deferred — non-blocking  
**Opened:** 2026-05-27 (identified Run #2)

**Symptom:**
`architecture_decision` events are extracted with titles that are metadata rows from ADR decision tables rather than decision content. Examples from Run #2 corpus:
- "Architecture Decision: Date: 2025-10-01"
- "Architecture Decision: Sam Webb: "Who owns the event store schema?...""
- "Architecture Decision: Owner Action Due" (table column header, rejected — id=61, 69, 129, 136)

These pass extraction and some pass the quality filter. The rejected variants were caught by the operator review pass, but the retained variants remain as active events with misleading titles.

**Root cause:**
ADR documents use structured tables for decision metadata (Date, Owner, Action, Due date). The `architecture_decision` pattern fires on any text following "decided", "decision", or "ADR:" including table rows. There is no guard against extracting table-structured metadata as decision content.

**Not fixed in Run #2 remediation because:**
Fix 3 markdown sanitization strips table dividers (`|---|`) but does not prevent extraction of table cell content as event titles. Fixing requires either a structural parser for markdown tables or a guard that detects table-row context before applying the decision pattern.

**Proposed remediation (to be validated):**
1. In `_is_table_row(line)`, detect lines that match `|...|...|` structure and suppress pattern matching within them.
2. In `extract_from_chunk`, skip chunks where the majority of non-empty lines are table rows.
3. Alternatively, add a post-extraction heuristic: if the title contains `Date:`, `Owner:`, `Action:`, `Due:` as a prefix after the event-type prefix, discard.

**Acceptance criterion:**
On the Run #2 frozen corpus, zero `architecture_decision` events with titles matching the pattern `Architecture Decision: [A-Z][a-z]+:` (metadata key prefix) are extracted.

**Regression guard:**
`test_architecture_decision_keyword_adr` and `test_architecture_decision_pattern` in `ingestion/tests/test_extractor.py` must continue to pass.

---

## EI-003 — source_reference over-extraction

**Class:** C — parser-quality  
**Priority:** Low  
**Status:** Deferred — non-blocking  
**Opened:** 2026-05-27 (identified Run #2)

**Symptom:**
18 `source_reference` events were extracted from the Run #2 corpus and all 18 were rejected by operator review. The events correspond to inline URL citations, bibliography entries, and "See:" references — structural citation markers rather than substantive institutional knowledge.

**Root cause:**
The `source_reference` pattern fires on any line containing a URL or a "See:" prefix. These are common in reference material, deployment runbooks, and incident reports as navigation aids, not as knowledge claims. The pattern has no minimum context requirement — it fires on a bare URL with no surrounding sentence.

**Not fixed in Run #2 remediation because:**
The rejection rate of 100% (18/18) was caught and handled by the operator review pass. Suppressing `source_reference` entirely would be too aggressive — legitimate source references with meaningful context (e.g., "See [paper X] for the derivation of the regime classifier confidence threshold") should be captured. The fix requires context sensitivity, not pattern removal.

**Proposed remediation (to be validated), choose one:**
1. **Confidence floor**: assign `source_reference` a default confidence of 1 (currently 2 or 3). Events at confidence 1 are filtered by the default `min_confidence=2` activation policy, making them invisible to assembly without reaching rejection status.
2. **Context requirement**: require the URL or citation to appear in a sentence with at least one non-stop-word verb or noun before the URL. Bare-URL lines and table-of-contents entries would not qualify.
3. **Opt-in extraction**: treat `source_reference` as an opt-in event type that requires an explicit keyword prefix ("source:", "reference:", "citation:") rather than a URL alone.

**Acceptance criterion:**
On the Run #2 frozen corpus, `source_reference` rejection rate in operator review drops from 100% to ≤ 30%.

**Regression guard:**
`test_source_reference_pattern` in `ingestion/tests/test_extractor.py` must continue to pass with a well-formed "See: URL" pattern.

---

## EI-006 — Governance partition includes rejected and superseded events from general retrieval path

**Class:** Non-blocking — retrieval/noise issue  
**Priority:** Low  
**Status:** Deferred — non-blocking  
**Opened:** 2026-05-27 (identified during EI-004 remediation, Longitudinal Run L1)

**Symptom:**
After the EI-004 fix to `retrieve_governance()`, the governance context tier still surfaces a bounded number of rejected and superseded events. Observed in `longitudinal_v1.db` post-EI-004-fix: events with status `rejected` (ids 20, 36, 63 — EI-001 `must not` fragments, conf=3) and `superseded` (ids 25, 31 — pre-ADR-007 Kafka decisions) appear alongside active governance events in the assembled governance tier.

**Root cause:**
`activate_memory()` in `session/activation.py` runs two retrieval passes:

1. `retrieve_governance()` — fixed by EI-004 to return only `active` and `accepted` events.
2. A general `RetrievalQuery` with no `event_types` and no `statuses` filter — returns all event types at all statuses that meet `min_confidence`. This pass is designed to surface residual events not covered by specific retrieval paths.

After deduplication by `seen_ids`, `governance_rule` and `architecture_decision` events retrieved by the general pass (but excluded from the governance pass because they are rejected/superseded) are added to the activation set. `partition_by_section()` then routes all events with `event_type in GOVERNANCE_EVENT_TYPES` to `governance_context` without checking status. Rejected and superseded governance-typed events therefore appear in the governance tier.

**Affected code paths:**
- `session/activation.py` — `activate_memory()` general retrieval query (lines ~153–159): no `statuses` filter
- `session/activation.py` — `partition_by_section()` (lines ~192–193): routes by `event_type` only, not by `status`

**Current operational impact:**
The governance tier is semantically noisy. Rejected and superseded entries consume governance tier budget and may displace additional active events. Impact is bounded: at the standard `max_governance_chars=4000` policy, the L1 corpus produces a 7-entry governance tier with 2 active, 3 rejected (small `must not` verb fragments), and 2 superseded entries.

**Why EI-004 fixed the primary blocker:**
EI-004's root cause was `retrieve_governance()` returning unfiltered results ordered by `id DESC`, allowing late-ingested high-ID rejected table-header artifacts (ids 117, 119, 120, conf=4) to consume the entire governance tier budget and produce 0 active events in assembly. The EI-004 fix eliminates that specific path. The residual events (20, 36, 63, 25, 31) enter via the general retrieval path, are lower-confidence fragments, and do not exhaust the budget — active events now surface.

**Why EI-006 is deferred:**
- The governance tier is no longer collapsed (0 active → 2 active after EI-004).
- The residual rejected/superseded entries are bounded and predictable.
- The issue does not affect lineage integrity, replay determinism, or continuity portability.
- Fixing requires modifying `partition_by_section()` routing logic, which has broad test coverage implications. Conservative approach: defer pending a dedicated retrieval-partitioning review.

**Recommended future remediation:**
Add a status exclusion guard in `partition_by_section()` in `session/activation.py`:

```python
EXCLUDE_FROM_GOVERNANCE = frozenset({'rejected', 'superseded', 'archived', 'deprecated'})

if mem.event_type in GOVERNANCE_EVENT_TYPES and mem.status not in EXCLUDE_FROM_GOVERNANCE:
    sections['governance_context'].append(mem)
```

This preserves `active`, `accepted`, `unresolved`, and `proposed` governance events while excluding terminal-status events. Requires regression tests covering: rejected governance_rule excluded from tier, superseded architecture_decision excluded, unresolved governance_rule still included.

**Acceptance criterion:**
After fix, governance context tier contains zero events with status in `{'rejected', 'superseded', 'archived', 'deprecated'}` under any activation policy.

**Not replay-affecting, not continuity-corrupting.**

---

## Not in scope for this backlog

The following are **not** tracked here because they are architectural decisions, not defects:

- No auto-contradiction detection — documented acceptable limitation in `validation/SIGN_OFF.md §7`
- `relevant_memory=0` in assembly without semantic search — expected behavior given tier ordering and corpus size; mitigations documented in RUN_20260527_OPERATOR.md
- Uniform confidence 3 for `governance_rule` — extractor design; calibration requires a separate confidence-assignment strategy

---

*Issues in this file are non-blocking, deferred, and do not affect replay integrity, governance detection, or continuity portability.*  
*They must be resolved before the next major corpus expansion (> 50 documents) or external operator onboarding.*

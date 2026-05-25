# Memory Governance Architecture

**Status:** Implemented — Milestone 3  
**Date:** 2026-05-21  
**Governance:** AI may propose. Quant validation must validate. Risk engine has final veto.

---

## Why Memory Governance Matters

A memory layer that can only store and retrieve is incomplete. Without governance, it accumulates:

- **Stale cognition** — Events marked `active` or `proposed` that were last touched months ago. The world has changed; the memory has not. Retrieval injects outdated beliefs into reasoning.
- **Ontology drift** — Over time, the same concept gets encoded under multiple titles, confidence levels, and statuses. The knowledge graph becomes inconsistent. Retrieval returns contradictory signals for the same question.
- **Contradictory active doctrine** — Two governance rules that explicitly contradict each other, both in `accepted` status, both retrieved. The downstream system has no way to resolve the conflict programmatically.
- **Unresolved memory sprawl** — Open questions accumulate without resolution. After 90 days, an unresolved event is not an open question — it is institutional neglect. It will continue to be retrieved and presented as current uncertainty indefinitely.
- **Recursive retrieval pollution** — An event with 20 `related_ids` expands retrieval to include 20 additional events. If those events are low-confidence, deprecated, or speculative, the context window fills with noise. Governance must cap fanout.
- **Epistemic fragmentation** — Adaptation events with no linked validation results. Events that claim high confidence with no evidence field. Architecture decisions superseded months ago that still appear in retrieval results.

Memory governance is the mechanism that makes these problems visible — not hides them, not resolves them automatically, but makes them explicit so humans can act.

---

## Retrieval Pollution Risks

Retrieval pollution is the most operationally dangerous failure mode of a memory layer.

It occurs when context assembly injects events that:
1. Have been superseded but still appear in `active` or `accepted` status
2. Are low-confidence speculations treated as confirmed knowledge because their status was never downgraded
3. Are deprecated doctrine that active events still link to, creating invisible dependencies
4. Are orphaned fragments with no relationship to any other event, consuming budget with no epistemic value
5. Carry stale timestamps — the `updated_at` field is the primary recency signal; stale events appear recent if never touched

The governance layer addresses each of these with a specific detection function:
- `detect_stale_memory` — surfaces cases 1 and 4 (active events never updated)
- `detect_low_confidence_active` — surfaces case 2 (speculations in active status)
- `detect_deprecated_linked` — surfaces case 3 (deprecated events still referenced)
- `detect_orphans` — surfaces case 4 (disconnected fragments)

The retrieval filter (`filter_events` with `RetrievalFilter`) provides a deterministic mechanism to exclude deprecated or low-confidence events from context assembly. Crucially, filtering is explicit and caller-controlled. The retrieval layer never silently hides events.

---

## Ontology Drift Risks

Ontology drift occurs when the same concept is represented by multiple events with inconsistent titles, types, or confidence levels — without any explicit supersession relationship between them.

Signs of ontology drift:
- Duplicate titles in `memory_events` (detected by `detect_duplicate_title`)
- Multiple `governance_rule` events with `contradicts` links between them, both `accepted` (detected by `detect_conflicts`)
- `adaptation` events not backed by any `validation_result` (detected by `detect_adaptation_lineage_gap`)

Drift is insidious because it is invisible at the event level. No single event is wrong. The problem is the relationship between events — or the absence of it.

The governance layer's reports aggregate these signals. A weekly run of `build_governance_report` gives a snapshot of the current epistemic state and flags where drift is occurring.

---

## Epistemic Hygiene Principles

Five principles govern the design of the governance layer:

**1. Identification, not mutation.**  
The governance layer identifies issues and recommends actions. It does not resolve them. It does not supersede events. It does not delete anything. Every governance function is read-only at the database level.

**2. Severity, not urgency.**  
Issues are classified as `info`, `warning`, or `critical`. This is an epistemic classification, not a time-based escalation. `critical` means the issue is structurally unsafe — two contradicting accepted governance rules, an active event with confidence 1. `warning` means degraded epistemic quality. `info` means worth knowing, not worth acting on immediately.

**3. Determinism.**  
The same database state always produces the same governance report. Governance logic is pure SQL plus deterministic Python sort keys. No randomness, no hidden heuristics, no embedding-based similarity in the issue detection pipeline.

**4. Explicit rationale.**  
Every `GovernanceIssue` carries a `rationale` explaining why the issue was flagged and a `recommended_action` explaining what a human should do. The system does not issue unexplained alerts.

**5. Human approval required.**  
The governance layer may recommend superseding an event, adding evidence, or linking an adaptation to a validation result. It does not perform any of these actions. Resolution requires a human to call `service.update_status`, `service.link_memory_events`, or similar write operations explicitly.

---

## Why Deletion Is Dangerous

Memory deletion is permanently off the table for the following reasons:

**Audit integrity.** The memory layer is an audit trail of institutional cognition. Deleting an event removes evidence of what was believed, when, and by whom. An auditor cannot reconstruct the state of knowledge at a given point in time if events can be deleted.

**Supersession is safer.** An event that is wrong should be marked `superseded` or `rejected`, with a revision entry explaining why. This preserves the fact that the belief existed, when it changed, and on what basis. Deletion provides none of this.

**Cascading unknowns.** Deleting event A removes any `memory_links` rows that reference A. Other events may reference A in `related_ids_json`. Deletion creates invisible holes in the knowledge graph that cannot be detected — because the evidence of A's existence is gone.

**Reproducibility.** A deterministic export from the same database should be byte-identical across machines. Deletion is an irreversible mutation that cannot be undone in the export. `memory_revisions` provides a full change history, but only for status and version changes — not for deleted rows.

The correct resolution lifecycle is: `proposed → active/accepted → superseded/deprecated → archived`. Events transition through governed states. They are never removed.

---

## Replayable Governance

All governance functions are fully replayable. Given the same database at the same logical state, `build_governance_report` produces the same report — modulo the `generated_at` timestamp field, which records when the report was run.

The sort key for issues in the report is:
```
(severity_rank, issue_type, memory_id)
```

All three components are deterministic:
- `severity_rank` is a dict lookup on a closed vocabulary
- `issue_type` is a string from a closed vocabulary
- `memory_id` is an autoincrement integer

The report is exportable to dict via `to_dict()`. An auditor can checksum reports, diff them over time, and reconstruct the epistemic state of the memory layer at any point when reports were captured.

Review queues follow the same principle. `review_unresolved` always returns events in `created_at ASC` order. `review_stale` returns events in `updated_at ASC` order. `review_low_confidence_active` returns events in `(confidence ASC, id ASC)` order. All orderings are deterministic.

---

## Detection Function Reference

| Function | What It Detects | Default Severity |
|---|---|---|
| `detect_stale_memory` | Active/proposed events not updated in `warning_days` | warning / critical |
| `detect_conflicts` | Contradicts-linked events both in active/accepted status | critical |
| `detect_orphans` | Events with no links and not referenced by any other event | info |
| `detect_missing_evidence` | Validation results or high-confidence accepted events with no evidence | warning |
| `detect_low_confidence_active` | Active/accepted events with confidence ≤ threshold | warning / critical |
| `detect_unresolved_aging` | Unresolved events older than `warning_days` | warning / critical |
| `detect_deprecated_linked` | Deprecated events still linked from active memory | warning |
| `detect_duplicate_title` | Multiple events with identical titles | warning |
| `detect_excessive_fanout` | Events with more related_ids than `max_fanout` | info |
| `detect_adaptation_lineage_gap` | Active/accepted adaptations with no linked validation_result | warning |

---

## Retrieval Filter Reference

`filter_events(events, RetrievalFilter(...))` is a pure function — no database access, no side effects.

| Parameter | Effect |
|---|---|
| `exclude_deprecated=True` | Drops events with status `deprecated` |
| `suppress_unresolved=True` | Drops events with status `unresolved` |
| `min_confidence_active=N` | Drops active/accepted events with confidence < N |

Filtering does not suppress the governance report. Callers are responsible for running `build_governance_report` separately if they need visibility into what was filtered and why.

---

## Review Queue Reference

| Function | Returns | Ordering |
|---|---|---|
| `review_unresolved` | Unresolved events aged beyond threshold | `created_at ASC` |
| `review_stale` | Active/proposed events not updated within threshold | `updated_at ASC` |
| `review_conflicts` | GovernanceIssue list for contradicting active pairs | `memory_id ASC` |
| `review_low_confidence_active` | Active/accepted events below confidence threshold | `(confidence ASC, id ASC)` |
| `review_deprecated_linked` | Deprecated events linked from active memory | `id ASC` |
| `get_review_queue` | Combined ReviewQueue of all categories | per-category ordering |

---

## Operational Governance Verification (Phase 9A)

Phase 9A extended the governance layer with three read-only detectors that verify structural integrity of the execution lineage — the relationships between cognition sessions, context assemblies, activation decisions, and activation log transitions. These detectors operate on live database state and are always read-only.

### Detectors

#### `detect_fired_decisions_without_assembly(db_path) -> List[dict]`

Scans `activation_decision_log` for rows with a fire outcome that have no associated context assembly record. An activation decision that fired without an assembly record indicates either a missing assembly row or a decision that bypassed the standard assembly pathway. Returns a list of raw decision rows that lack a linked assembly.

Table guard: if `activation_decision_log` or `context_assembly_log` is absent, returns an empty list without error.

#### `detect_orphaned_transitions(db_path) -> List[dict]`

Scans `activation_log_transitions` for rows whose `cognition_session_id` does not reference any row in `cognition_sessions`. An orphaned transition indicates a transition recorded against a session that no longer exists or was never committed. Returns a list of raw transition rows with no valid session parent.

Table guard: if either table is absent, returns an empty list without error.

#### `check_lineage_integrity(db_path) -> LineageIntegrityReport`

Runs four foreign-key checks and returns a `LineageIntegrityReport`:

| Check | Description |
|---|---|
| `activation_decisions_without_assembly` | Fired decisions with no assembly record |
| `orphaned_transitions` | Transitions with no parent session |
| `assemblies_without_session` | Assembly records with no parent session |
| `decisions_without_session` | Decision records with no parent session |

`LineageIntegrityReport` fields: `checked` (bool), `all_ok` (bool), `broken_count` (int), `details` (list of per-check result dicts).

### Integration with `build_governance_report`

```python
build_governance_report(
    db_path,
    ...,
    detect_execution_lineage_issues=True,   # default True
)
```

When `detect_execution_lineage_issues=True`, `build_governance_report` runs `check_lineage_integrity` and appends a `governance_issue` for each broken FK relationship found. The issue carries `severity='critical'`, `issue_type='lineage_integrity_broken'`, and a `rationale` identifying which check failed and how many rows were affected.

### Governance Verification CLI Commands

| Command | Description |
|---|---|
| `governance-report` | Run `build_governance_report` and print the full issue list. `--format json` produces machine-readable output. |
| `verify-assembly` | Run `verify_context_assembly_divergence` for one assembly and report divergence details. |
| `verify-session` | Run `verify_session_timeline_divergence` for one session and report divergence details. |
| `lineage-integrity` | Run `check_lineage_integrity` and report broken FK counts and details. Always read-only. |

All four commands are read-only. None mutate database state under any circumstances.

#### Usage

```bash
python -m memory.cli governance-report --db memory.db
python -m memory.cli governance-report --db memory.db --format json

python -m memory.cli verify-assembly --db memory.db --assembly-id 7
python -m memory.cli verify-session --db memory.db --session-id 3

python -m memory.cli lineage-integrity --db memory.db
```

`lineage-integrity` exits with code 0 when all checks pass, code 1 when any broken relationship is found.

---

## Future Governance Extensions

**Confidence decay schedule**  
A future extension could automatically flag events whose confidence should decay over time — for example, `regime_observation` events older than 12 months that have not been superseded. This would require a decay schedule per `event_type`, which must be quant-validated before implementation. The detection function pattern is already established; adding decay detection requires only a new SQL query and a new `issue_type`.

**Tag taxonomy governance**  
If a closed tag vocabulary is introduced (see `MEMORY_LAYER_ARCHITECTURE.md`), governance could detect events using tags outside the approved vocabulary — `invalid_tag` issues. This would require a `memory_tags` reference table.

**Evidence quality scoring**  
A future extension could classify evidence quality — distinguishing a link to a document from a link to a run ID from a narrative description. Evidence quality could feed into confidence recommendations. This requires a structured `evidence` field (currently free-form text).

**Governance report export**  
`GovernanceReport.to_dict()` is already machine-readable. A future `governance export` CLI command could persist a report to a JSON file with a timestamp, enabling trend analysis across report runs — tracking whether the governance burden is growing or shrinking over time.

**Automated remediation proposals**  
A future layer could generate draft `update-status` or `link` commands for each issue — not execute them, but produce a machine-readable remediation plan that a human can review and approve. This preserves the human-approval principle while reducing the mechanical overhead of resolution.

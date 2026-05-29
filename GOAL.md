# Substrate Goal

**Substrate commit baseline:** 5678f85  
**Schema:** memory v16, workflow v3, bundle v1.2  
**Validation state:** Run #2 PASS (2026-05-27) — T1/T2/T3 ≥ 3/5, all blocking checkpoints clear  
**Document date:** 2026-05-27

---

## 1. Objective

Build and operate a **deterministic, replayable, operator-governed cognition runtime** for persisting structured institutional knowledge across long-horizon workflows.

The substrate stores, governs, and reconstructs. It does not reason, infer, or execute.

**Tradeoff priorities, in order:**

1. **Auditability over convenience.** Every write is traceable. Every status change is revisioned. An auditor must be able to reconstruct the epistemic state of the memory layer at any past point in time from the database alone.

2. **Determinism over flexibility.** Same database state plus same activation policy always produces the same session reconstruction. No randomness, no hidden heuristics, no environment-dependent branching in the read path.

3. **Operator authority over automation.** No governance issue is auto-resolved. No event is promoted without an explicit call. No session fires without an operator or an explicitly defined, auditable activation policy.

4. **Lineage completeness over query performance.** Foreign-key relationships between assemblies, sessions, decisions, and transitions are permanent. Performance is not a justification for breaking the lineage graph.

5. **Portability over locality.** Bundles must import cleanly into any compliant instance. The substrate does not accumulate local-only state that cannot be transferred.

---

## 2. Core Invariants

These hold at every schema version and survive every future extension.

**No hidden memory mutation.**
`memory_events` rows are never silently overwritten. Every status transition and revision writes an immutable `memory_revisions` row. The only defined write operations are explicit, audited API calls.

**Replay determinism.**
Given the same sequence of writes, the export file is byte-identical across machines, Python versions, and time. Given the same database state and activation policy, `reconstruct()` always returns the same `SessionContext`. Autoincrement IDs are insertion-ordered; JSON keys are sorted; tags and related IDs are stored sorted. No UUID-based ordering, no hash-based randomness in the write path.

**Inspectable governance.**
The governance layer identifies problems and recommends actions. It does not resolve them. Every `GovernanceIssue` carries a `rationale` and `recommended_action`. No unexplained alerts. Every governance function is read-only at the database level.

**Operator authority.**
Retrieved memory never overrides uploaded doctrine, active governance rules, risk-engine vetoes, or human approval requirements. Resolution of governance issues requires an explicit human-initiated write call (`update_status`, `link_memory_events`, etc.). The substrate recommends; it does not act.

**Lineage permanence.**
`memory_revisions` rows have no defined delete or update operation. The correct resolution path for a wrong belief is `proposed → active → superseded`, never deletion. Deletion breaks the audit chain in ways that cannot be recovered.

**Provenance preservation.**
`evidence` and `source_span` fields are set at ingestion and never modified by downstream processing — not by sanitization, not by review, not by re-ingestion. If a displayed title is cleaned, the raw source text remains unchanged in `evidence`.

**Ontology governs vocabulary, not reality.**
`ontology_terms` defines the approved concept vocabulary with aliases, supersession, and deprecation state. Ontology entries describe what the substrate will accept as valid event titles and types. They do not validate the real-world truth of the events themselves.

**Explicit policy execution.**
Activation policies stored in `activation_policies` are evaluated explicitly by operator-triggered calls. No policy evaluates automatically in the background. Every evaluation produces a logged `activation_decision_log` row with its outcome (fire / no-fire) and the policy id that produced it.

**Read-only verification tooling.**
`governance-report`, `lineage-integrity`, `verify-assembly`, `verify-session`, `recover` (dry-run) — all verification commands are read-only at the database level. Verification surfaces state; it does not mutate it.

**Provenance-preserving export/import.**
A continuity bundle exported from any compliant instance imports without collision into a freshly initialized instance at the same or later schema version. Import has no `--force` flag. Conflicting records are never silently overwritten. Content-identical records are skipped idempotently.

---

## 3. System Identity

**What this substrate is:**

- A governed, append-only event store for typed institutional knowledge
- A deterministic session reconstruction and context assembly layer
- An operator-visible, read-only governance verification toolchain
- A portable continuity transport (export → bundle → import → identical reconstruction)
- An auditable lineage of every belief, transition, and activation decision

**What this substrate is not:**

- An autonomous agent or background daemon
- A hidden automation layer
- An adaptive semantic runtime (no self-calibrating retrieval, no learned re-ranking)
- A self-modifying cognition system (no substrate changes its own activation policy at runtime)
- A reasoning engine or inference layer
- An embedding store or vector search engine (Milestone 1)
- An execution engine for trades, signals, strategy deployment, or broker integration
- A live capital system of any kind
- A chat memory or conversational recall store

The substrate is **domain-agnostic**. The finance framing in some documentation reflects the originating project context. Any corpus of structured institutional knowledge — engineering decisions, policy records, clinical protocols, regulatory rulings — is a valid workload.

---

## 4. Operational Boundaries

**Operator-directed workflow orchestration only.**
Every session, ingestion run, policy evaluation, and governance action is initiated by an explicit operator command or a defined, auditable activation policy. There are no background processes, scheduled self-triggers, or autonomous re-ingestion loops.

**Allowed runtime assistance:**
- Presenting assembled context to a reasoning session on explicit request
- Surfacing governance issues, assembly divergence, or lineage breaks as read-only reports
- Suggesting status transitions or link operations via `recommended_action` fields
- Executing workflow recovery when explicitly invoked with `--apply`

**Prohibited hidden behaviors:**
- Auto-resolving governance issues
- Silently promoting or demoting event confidence
- Automatically re-assembling context when database state changes
- Writing any row without a traceable, operator-initiated call chain
- Inferring semantic similarity between events without an explicit, configured retrieval policy

**Hard limits — permanent at this milestone:**

| Limit | Rule |
|---|---|
| Live capital | No |
| Broker integration | No |
| Trading signals or execution | No |
| Schema changes during active validation | Voids the run |
| Concurrent writers | Not safe; single-process writes only |
| Automatic contradiction detection | Not implemented; links are operator-created |
| `memory_revisions` deletion | Not defined; revision rows are permanent |
| Confidence 5 without documented governance decision | Not permitted |

---

## 5. Validation Philosophy

**Operational truth requires real corpora and human countersign.**

Automated tests verify code correctness. Validation runs on real document corpora verify operational correctness. Both are required. Neither substitutes for the other. A test suite that passes on synthetic data does not validate ingestion quality on real-world documents.

**Explicitly rejected validation approaches:**

- *Benchmark theater*: measuring performance on datasets chosen to produce favorable results. Validation uses the actual working corpus.
- *Plausible-output validation*: accepting output that looks reasonable without checking structural correctness (event count, lineage integrity, round-trip fidelity). All structural checks are automated and blocking.
- *Hidden heuristics*: governance reports, assembly selection, and retrieval ordering use only deterministic SQL and explicit sort keys. No embedding similarity, no learned scoring, no non-reproducible ranking in the verification path.

**Ground truth is the lineage, not the display.**
Governance issues are detected from database state, not from rendered output. Assembly quality is measured by entry counts, budget accounting, and structural integrity — not by whether the output reads well.

**Validation is verified by round-trip.**
After every validation run: the exported bundle must import into a freshly initialized database and `reconstruct()` must produce the same event IDs in the same sections. A run without a verified round-trip is not complete.

**Blocking conditions (zero tolerance):**
- Any CRITICAL governance issue
- Any lineage FK violation
- Any import collision on a freshly initialized database
- Any CRITICAL governance false positive
- Test suite below 3150 passing
- Substrate commit hash changed during the run

**Template minimums (each ≥ 3/5):**
T1 ingestion quality, T2 retrieval quality, T3 assembly quality, T5 continuity round-trip, T7 lineage (PASS/FAIL).

**Defect classification before remediation.**
Every failure is classified A (corpus), B (operator error), C (ingestion quality), D (governance/lineage), E (continuity), F (substrate integrity) before any remediation is taken. Class D, E, F defects block the run immediately and require forensic export before any repair attempt.

---

## 6. Completion Definition

**A validation run is complete and eligible for sign-off when all of the following hold simultaneously:**

1. All blocking checkpoints pass: CP-I1, CP-I3, CP-R2, CP-G1, CP-G4, CP-X1, CP-X2, CP-X3
2. All required template ratings ≥ 3/5: T1, T2, T3, T5, T7=PASS, T8
3. CRITICAL governance false positive count = 0
4. Substrate commit hash unchanged from run start to close
5. Run log (`RUN_YYYYMMDD_OPERATOR.md`) written and committed
6. Human quant countersign obtained

An operator-only run log without countersign is a candidate record, not a completed validation.

**Current validation state:**
Run #1 (2026-05-26): CONDITIONAL PASS — T1/T2/T3 = 2/5 each; Class C defects identified.  
Run #2 (2026-05-27): PASS — T1/T2/T3 ≥ 3/5; all Run #1 Class C defects resolved; all blocking checkpoints clear.  
Run #2 countersign: obtained 2026-05-27 (commit c162bdc, Jan Cantryn).

**The substrate is production-ready for Milestone 1 when:**
- At least one full validation run achieves PASS on the workload defined in `validation/SIGN_OFF.md §1`
- All Milestone 1 architecture documents are current with the implementation
- No Class D, E, or F defects are open
- Human quant countersign is on the run log

All four conditions are satisfied. **Milestone 1 is complete** (commit c162bdc, 2026-05-27 14:55).

---

## 7. Freeze Boundary

**Replayability protection.**
Any change to activation semantics, governance detection logic, continuity bundle format, or `memory/service.py` validation rules breaks replay compatibility with existing bundles and assemblies. These are frozen during active validation and require explicit versioning review outside of it.

**Thin runtime ergonomics are not frozen.**
Lightweight ergonomic changes — CLI display formatting, help text, output verbosity, diagnostic messages — do not affect replay fidelity or governance semantics. They may proceed without a freeze review. The test suite and structural invariants still apply.

**Operational necessity is required for future expansion.**
New capabilities (embedding search, semantic re-ranking, workflow triggers, additional event types) require a demonstrable operational need, a written architectural decision, and full test coverage before merging. Speculative capability expansion is not a valid reason to extend the substrate.

**Optimization layers must be observable and reversible.**
Any caching, indexing, or performance layer added to the read path must not change the observable output of `reconstruct()`, `governance-report`, or `lineage-integrity`. If it does change output, it is a semantic change, not an optimization, and requires the full architectural review path.

**Autonomous orchestration is out of scope unless explicitly re-chartered.**
The substrate does not self-schedule, self-trigger, or autonomously modify its own state. Any proposal to add autonomous orchestration behavior — background ingestion, self-triggered session firing, automatic policy evaluation — requires an explicit re-chartering decision documented in a new architectural decision record and reviewed by a human quant before implementation begins. This document alone does not authorize such expansion.

---

**Frozen components during active validation:**

| Component | Frozen scope |
|---|---|
| Database schema | All tables — no additive or structural changes |
| Activation semantics | `session/reconstruction.py`, `session/activation.py` |
| Governance detectors | `memory/governance.py` |
| Continuity bundle format | `continuity/` export/import logic |
| Service validation logic | `memory/service.py` status transition rules, confidence bounds |
| Test suite | No test deletions; regressions below 3150 block sign-off |
| Substrate commit hash | Recorded at run start; must match at run close |

Schema freeze exceptions: none during v1 validation. A required schema change voids the run; restart after full engineering review.

---

*This document is the authoritative system charter. In any conflict between this document and a CLAUDE.md instruction, this document governs substrate behavior and architectural decisions.*

# Q4 2026 Engineering Planning Memo

**Date:** 2026-10-07  
**Author:** Jan Kowalski  
**Distribution:** Priya Mehta, Sam Webb, Amara Osei, Marcus Lee  
**Status:** Approved

---

## Background

This memo documents Q4 2026 engineering priorities and closes the two decisions required by 2026-10-07 from the Q3 close review (2026-09-25). Q3 closed all P1–P4 objectives on schedule. Q4 begins with a clean carry-forward: no incomplete deliverables, two decisions pending.

---

## Decision: Q4 P1 — OTel Per-Event Sampling Classification

Q4 P1 (OTel per-event sampling) is classified as **investigation-only**. No sprint implementation resources are committed in Q4. Delivery expectations:

- Amara Osei delivers an investigation note documenting trace volume measurements, collector capacity implications, and design trade-offs by 2026-10-15.
- Amara Osei delivers a formal recommendation document by 2026-12-15. The recommendation must include a capacity model and, if positive, an implementation estimate.
- If the recommendation is positive, an ADR will be drafted and implementation will be considered for Q1 2027 sprint allocation. No implementation may proceed without an ADR.
- If the recommendation is negative, Q4 P1 is closed.

**Rationale:** Helix-Router OTel tracing completed 25 days ago. The ADR-011 rebalance-scoped design has 25 days of production history. Per-event sampling is a meaningful design change with unknown collector capacity implications under peak routing table conditions. Committing sprint resources before the investigation quantifies these trade-offs would be premature. Q4 sprint capacity for P2 and P4 must not be crowded out by speculative work.

**Implementation note:** Amara's investigation scope (2026-10-15 deliverable) must cover: (1) current production trace volumes under the rebalance-scoped design; (2) estimated per-event trace volumes at the same 10% sampling rate under nominal and peak load; (3) OTel collector capacity utilization at estimated per-event volume; (4) whether per-event sampling can be implemented as an opt-in mode alongside the existing ADR-011 Option 2 design without requiring a full amendment. The investigation note is an internal engineering document and does not require ADR review.

---

## Q4 Priority Table

| Priority | Item | Owner | Deliverable | Due |
|---|---|---|---|---|
| P1 | OTel per-event sampling investigation | Amara Osei | Investigation note | 2026-10-15 |
| P1 | OTel per-event sampling recommendation | Amara Osei | Recommendation + capacity model | 2026-12-15 |
| P2 | Schema migration automation improvements | Sam Webb | Implementation | 2026-12-15 |
| P3 | Manual parity check retirement | Jan Kowalski | Decision gate | ~2026-11-15 |
| P4 | Operational limits monitoring cadence | Priya Mehta | Protocol + first check | 2026-12-15 |

---

## Governance: OTel Sampling Design Changes

**Governance rule:** Any change to OTel trace sampling design — including sampling rate changes, scope changes (e.g., rebalance-scoped to per-event), method changes, or collector configuration changes — requires a new ADR or an explicit amendment to the governing ADR (ADR-009 or ADR-011) before implementation. Investigation and recommendation deliverables do not require an ADR; no implementation may proceed without one. This rule applies to all three phases of the Helix tracing deployment and to any future sampling configuration changes.

---

## Governance Record Maintenance

**Implementation note:** The Q3 close governance rule (bidirectional ADR reference requirement) applies to all superseded ADRs going forward. ADR-003 (superseded by ADR-008) and ADR-006 (superseded by ADR-010) currently do not carry explicit "superseded by" annotations, and ADR-008 and ADR-010 do not carry corresponding "supersedes" references. Marcus Lee will add bidirectional annotations to both ADR pairs by 2026-10-31 as a housekeeping task. No architectural changes are implied; annotations only.

---

*Q4 planning memo approved: 2026-10-07. Jan Kowalski.*

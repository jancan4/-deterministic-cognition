# OTel Per-Event Sampling: Q4 P1 Investigation Note

**Date:** 2026-10-15  
**Author:** Amara Osei  
**Distribution:** Jan Kowalski, Sam Webb  
**Status:** Preliminary investigation note — not an ADR

---

## Scope

This note evaluates OTel per-event trace sampling as an alternative to the current rebalance-scoped design (ADR-011 Option 2). It covers production trace volume measurements, collector capacity implications, design trade-offs, and a preliminary recommendation. A formal recommendation document is due 2026-12-15.

---

## Current Design Baseline

The rebalance-scoped trace design (ADR-011 Option 2) has been in production since 2026-09-12 (33 days). A trace ID is generated per rebalance event; all routing decisions within a rebalance event are child spans sharing that trace ID. The 7-day post-deployment validation (Phase 3 completion note, 2026-09-12) confirmed correct behavior.

**Implementation note:** Measured production trace volumes under the rebalance-scoped design as of 2026-10-15 (trailing 30-day window): 5–15 sampled traces per minute at nominal load. Each sampled trace contains 2–15 routing-decision child spans (mean: 8). The OTel collector processes approximately 40–180 total spans per minute under this design. Collector utilization is well within capacity at current load. Error retention is 100%; no sampling gaps observed.

---

## Per-Event Sampling — Volume Analysis

Under per-event sampling, each routing decision (currently a child span within a rebalance trace) becomes an independent root span in its own trace. Cross-decision correlation within a rebalance event is not achievable without a separate out-of-band correlation mechanism.

**Implementation note:** Estimated per-event trace volume at 10% sampling rate: 50–200 traces per minute under nominal load. This is 10–40× the current rebalance-scoped volume (5–15 traces/minute). The increase arises because each routing decision becomes an independently sampled event rather than a child span within an already-sampled rebalance trace. Under peak routing table conditions (15 decisions per rebalance, elevated rebalance rate during high-throughput periods), per-event volume could reach 250+ traces per minute.

**Implementation note:** The OTel collector capacity limit is not formally specified in ADR-009 or ADR-012. The Phase 3 validation confirmed stable collector operation at 40–180 spans per minute. Per-event mode at 50–200 single-span traces per minute represents a 1.5–5× increase in trace count. This volume is unlikely to saturate the collector at nominal load, but performance under peak routing table conditions (15 decisions/rebalance, sustained high throughput) has not been validated. A staging load test under these conditions is required before per-event mode can be recommended for production.

---

## Design Trade-Off Analysis

**Validation result:** Per-event vs. rebalance-scoped sampling — preliminary comparison (2026-10-15):

| Dimension | Rebalance-scoped (ADR-011 Option 2, current) | Per-event |
|---|---|---|
| Sampled traces/minute (nominal) | 5–15 | 50–200 (estimated) |
| Sampled traces/minute (peak load) | 10–30 (estimated) | 250+ (estimated) |
| Cross-decision correlation | Yes (within rebalance event) | No (each decision independent) |
| Collector load vs. current baseline | Baseline | 10–40× (nominal); higher at peak |
| Staging load test required | N/A (deployed, validated) | Yes — peak routing table conditions |
| ADR-009 sampling rate applicability | Direct (10% of rebalance events) | Requires rate re-evaluation |
| ADR-011 alignment | Full (Option 2) | Requires amendment or ADR-013 |

The cross-decision correlation property is the primary reason ADR-011 selected Option 2. Correlation allows debugging of rebalance-cycle anomalies — for example, detecting that a routing decision late in a rebalance cycle produces different latency than early decisions. Per-event sampling loses this property.

---

## Architecture Decision Planning

**Architecture decision:** Per-event OTel sampling ADR scope (ADR-013 candidate, Jan Kowalski, 2026-10-15):
If the 2026-12-15 recommendation is positive, ADR-013 is required before any implementation. ADR-013 must address: (1) whether per-event sampling replaces or supplements the rebalance-scoped design; (2) the required staging load test and collector capacity validation; (3) sampling rate for per-event mode (may differ from the 10% rebalance-scoped rate); (4) cross-decision correlation strategy or explicit acknowledgment that the property is not required. No implementation may proceed without ADR-013 approval. Status: planning — pending investigation outcome.

---

## Preliminary Recommendation

The rebalance-scoped design (ADR-011 Option 2) should be maintained as the general production sampling design. The cross-decision correlation property is a deliberate architectural choice that is operationally useful for rebalance-cycle anomaly diagnosis. Per-event sampling as a wholesale replacement is not recommended.

Two factors prevent a stronger negative conclusion at this stage. First, the collector capacity under peak routing table conditions is unvalidated. If a staging load test confirms sufficient headroom, per-event mode may be viable as a configurable option. Second, there is a potential use case for per-event tracing during targeted investigation windows — enabling it for 15-minute intervals when debugging a specific routing anomaly. This use case does not require a general replacement of the rebalance-scoped design.

---

## Open Question

**Open question:** Should a configurable per-event sampling mode be implemented as an opt-in diagnostic feature, allowing engineers to enable per-event tracing for a specified time window (e.g., 15 minutes, toggled via configuration) without replacing the default rebalance-scoped design? An opt-in mode would provide routing-decision-level granularity during targeted investigations without committing to per-event sampling as the general production design. Collector capacity implications for time-bounded opt-in use are expected to be acceptable but must be validated in staging. Owner: Amara Osei, due: 2026-12-15 (recommendation deadline).

---

*Investigation note: preliminary findings as of 2026-10-15. Formal recommendation due 2026-12-15. Amara Osei.*

# Trace Sampling Change Staging Mandate — Q4 2026 Governance Decision

**Date:** 2026-11-28  
**Author:** Jan Kowalski  
**Reviewed by:** Sam Webb, Amara Osei  
**Distribution:** Engineering, Architecture Review  
**Status:** Governance decision — effective immediately

---

## Scope

This document records a governance decision arising from the ADR-013 planning process and the OTel per-event sampling investigation (Amara Osei, 2026-10-15 through 2026-12-15). It establishes a mandatory staging validation period for any change to the OTel trace sampling configuration in production, and records the current ADR-013 status assessment.

---

## Governance Decision: Trace Sampling Staging Mandate

**Governance rule:** Before any change to OTel trace sampling parameters is deployed to production — including sampling rate changes, per-event mode enablement, collector configuration changes, and any ADR-013 implementation — the change must be validated in the staging environment for a minimum of 7 calendar days under representative load conditions. The staging validation must confirm: (1) collector stability at peak routing table load, (2) no trace header overflow, (3) no sampling gaps in error-path events, and (4) sampled trace volume within the bounds projected by the ADR change. Evidence of staging validation must be recorded as an implementation note before any production deployment is approved.

**Rationale:** The rebalance-scoped trace design (ADR-011 Option 2) was validated in staging before the 2026-09-12 production deployment. The ADR-012 header buffer compliance requirement was verified in staging. A consistent staging gate prevents the OTel collector from being destabilized by production changes that have not been load-tested. This is particularly relevant for ADR-013, where the per-event mode volume is estimated at 10–40× the current rebalance-scoped baseline. A 7-day staging period under peak routing table conditions (15 decisions/rebalance, elevated rebalance rate) is required to confirm collector behavior before any production deployment.

**Scope:** Applies to Helix-Router, Helix-Processor, and Helix-Commander OTel configurations. Does not apply to alerting threshold tuning or dashboard changes that do not affect trace sampling behavior.

**Effective:** 2026-11-28.

---

## ADR-013 Status Assessment

**Architecture decision:** ADR-013 candidate status — November 2026 assessment (Jan Kowalski, 2026-11-28):

ADR-013 remains in planning status, blocked on the 2026-12-15 recommendation from Amara Osei's per-event sampling investigation. The staging load test is scheduled for 2026-12-01. If staging results are available before 2026-12-15 and show collector capacity headroom, the recommendation may be to proceed with ADR-013 scoped to an opt-in configurable per-event mode.

ADR-013, if approved, must address all four items from the ADR-013 candidate scope note ([575]): (1) per-event mode relationship to rebalance-scoped design, (2) staging load test requirements and collector capacity validation, (3) sampling rate for per-event mode, (4) cross-decision correlation strategy. The staging mandate above (7-day minimum) is now a binding pre-condition for any ADR-013 production deployment, not an option.

**Implementation note:** ADR-013 staging gate binding pre-condition (2026-11-28):

The 7-day staging mandate established in this document is a binding pre-condition for ADR-013 production deployment. Any ADR-013 implementation plan must include an explicit staging validation phase of at least 7 days under peak routing table conditions. This pre-condition must be documented in ADR-013 when written.

---

## Relationship to Existing Governance

The staging mandate complements and does not supersede existing governance:

- Governance Rule [548]: "Any change to OTel trace sampling design — including sampling rate, per-event vs. rebalance-scoped mode — requires a formal ADR." The staging mandate adds a staging validation requirement on top of the ADR requirement; it does not reduce the ADR requirement.
- ADR-012 [517]: Header buffer compliance must be re-verified as part of any staging validation. Helix-Commander's 2048-byte buffer (ADR-012 minimum) may require expansion before any per-event mode deployment.

---

*Governance decision recorded 2026-11-28. Effective immediately. Jan Kowalski.*

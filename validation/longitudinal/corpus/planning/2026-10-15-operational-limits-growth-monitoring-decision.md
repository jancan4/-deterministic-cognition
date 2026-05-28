# Operational Limits: Throughput Growth Monitoring Decision

**Date:** 2026-10-15  
**Author:** Priya Mehta  
**Distribution:** Jan Kowalski, Sam Webb  
**Status:** Approved

---

## Background

The Q3 close review (2026-09-25) raised the question of whether a monthly growth-rate monitoring check should be formalized in the operational review cadence. The operational-limits-v4 re-evaluation condition (>50% baseline change in peak throughput) is anchored to the 13,100 messages/minute peak established when v4 was approved (2026-08-21). The condition triggers at approximately 19,600 messages/minute (13,100 × 1.5). This memo documents the decision and records the first monitoring check.

---

## Decision: Formalize Monthly Throughput Monitoring

Monthly production throughput monitoring is formalized as a standing operational procedure effective 2026-10-15.

**Architecture decision:** Helix Throughput Growth Monitoring Protocol (approved 2026-10-15, Jan Kowalski):
- Production peak throughput (messages/minute, 1-hour trailing peak window) must be recorded on the first business day of each calendar month.
- Recorded values are maintained in the platform operational review log alongside the re-evaluation trigger from operational-limits-v4.
- If the trailing 3-month compound monthly growth rate exceeds 5%, the operational limits threshold re-evaluation must be initiated within 30 days. This supersedes the informal quarterly cadence for monitoring purposes; the >50% baseline trigger from operational-limits-v4 remains as the hard re-evaluation gate.
- If current peak throughput reaches 80% of the re-evaluation trigger threshold (approximately 15,700 messages/minute), the re-evaluation must be initiated regardless of growth rate or calendar schedule.
- Owner: platform engineering lead (Priya Mehta, Q4 2026). Transferred to successor at each quarter close.

---

## First Monitoring Check — October 2026

**Validation result:** Helix throughput monitoring — October 2026 baseline (2026-10-15):
- Production peak throughput (trailing 30-day 1-hour peak): 13,600 messages/minute
- Q2 close peak (2026-06-30): 11,900 messages/minute
- Q3 mid-quarter peak (2026-07-28 review): 12,500 messages/minute
- Q3 close peak (2026-09-25): 13,100 messages/minute
- Trailing 3-month compound monthly growth rate (Jul–Oct): approximately 4.4% month-over-month
- Projected threshold crossing at 4.4%/month: approximately 8–9 months (June–July 2027)
- 80% warning level (15,700 messages/minute) at 4.4%/month: approximately 4 months (February 2027)
- Re-evaluation condition (19,600 messages/minute): NOT YET TRIGGERED — current peak 13,600 is 3.8% above the 13,100 operational-limits-v4 baseline; trigger at 50% above baseline
- Status: NOMINAL. No re-evaluation required. Monthly check confirmed operational.

---

## Governance Rule

**Governance rule:** Production peak throughput must be reviewed monthly against the HPA autoscaling threshold re-evaluation condition from operational-limits-v4. If the trailing 3-month compound monthly growth rate exceeds 5%, or if current peak reaches 80% of the re-evaluation trigger threshold (~15,700 messages/minute), the threshold re-evaluation must be initiated within 30 days and must not be deferred to the next quarterly review. Owner: platform engineering lead.

---

## Forward Projection

At the observed 4.4% monthly growth rate from the first monitoring check, the throughput trajectory is:

- November 2026: ~14,200 messages/minute
- February 2027: ~15,500 messages/minute (approaching 80% warning level)
- May 2027: ~17,800 messages/minute
- July 2027: ~19,300 messages/minute (approaching re-evaluation trigger)
- August 2027: ~20,200 messages/minute (threshold crossed)

**Implementation note:** The 4.4% monthly growth rate is derived from three quarter-end measurements (Q2 close, Q3 mid, Q3 close) and the October first check. This is a directional estimate. Growth rate will be recalculated each month as new data points accumulate. If two consecutive months show growth above 5%, the 30-day re-evaluation window in the governance rule above applies immediately; do not wait for the 80% level to be reached.

---

*Monitoring decision approved: 2026-10-15. Priya Mehta, Jan Kowalski.*

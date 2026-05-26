---
author: substrate-demo
date: 2026-05-25
source_type: doctrine
---

# Substrate Governance Doctrine

This document records the governance rules, architecture decisions, and operational
constraints that govern the deterministic cognition substrate.

## Architecture Decisions

We decided to use SQLite as the canonical persistence layer for all memory events.
SQLite provides deterministic ordering, ACID transactions, and zero-dependency
deployment. All writes go through service.py with no bypass path available.

We chose to use content-addressed identity for source documents. Each registered
source file receives a SHA-256 identity derived from its raw bytes, enabling
deduplication across ingestion runs without a central registry lookup.

ADR: The continuity bundle format uses JSON with sort_keys=True to guarantee
byte-identical output across Python versions and platforms.

## Governance Rules

Policy: No live capital deployment without quant validation and explicit human approval.

Operators must not modify memory events directly in the database. All writes must
route through service.py to preserve the revision audit trail.

Governance rule: The risk engine has final veto on all strategy-affecting decisions.
Human approval is required before any geopolitical regime change is accepted.

Must always record the created_by field on every write. Anonymous writes are rejected.

## Implementation Notes

Note: The memory_revisions table is append-only. Rows are never deleted or updated
after creation. This is a deliberate constraint, not an oversight.

Warning: Direct SQLite writes that bypass service.py will corrupt the revision trail
and cannot be detected by the governance layer.

Note: All timestamps must be UTC ISO-8601 in the format YYYY-MM-DDTHH:MM:SSZ.
Local time zones are never stored.

Technical note: The bundle checksum covers all manifest fields except the
checksum_sha256 field itself, which is excluded to avoid circularity.

## Open Questions

What threshold of confidence decay should trigger an automatic governance escalation?

Should the compression confidence rating (1-5) be mandatory or optional for promoted
artifacts?

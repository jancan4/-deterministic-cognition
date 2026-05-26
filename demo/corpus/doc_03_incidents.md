---
author: substrate-demo
date: 2026-05-25
source_type: implementation_brief
---

# Incident Log and Rejected Approaches

This document records operational incidents, rejected implementation ideas,
and open questions that require operator attention.

## Incidents

Incident: During Phase 9B development, the test_no_warning_when_artifact_present_in_target
fixture failed with IntegrityError because the real compression_artifacts table has
many NOT NULL columns. Root cause: CREATE TABLE IF NOT EXISTS was a no-op since init_db
had already created the full table. Resolution: DROP and recreate a minimal stub.

Incident: Phase 9A test_cli_execute_not_fired_no_assembly_printed had a hidden third
assertion at line 571 that referenced stdout outside its defining scope. Root cause:
30-line read window cut off at line 570. Resolution: removed the orphaned assertion.

Post-mortem: An early version of the bundle checksum function did not include the
manifest fields in the content dict. This allowed manifest tampering without
checksum invalidation. Root cause: manifest was added after the checksum function
was written. Resolution: manifest_without_checksum is now included in the content dict.

## Rejected Ideas

Rejected because it creates unauditable state: allowing direct database writes that
bypass service.py validation. Every write must go through the service layer.

Decided against using UUIDs for memory event IDs. The autoincrement INTEGER primary
key provides deterministic, ordered, compact IDs that are stable across imports
when identity preservation semantics are applied.

Won't use embedding similarity as the primary retrieval mechanism. Deterministic
tag and confidence filters are always available regardless of embedding model state.
Embeddings are an optional enrichment layer, not a retrieval prerequisite.

Rejected because it breaks backward compatibility: changing the bundle checksum
function to exclude old-format bundles. The existing versioned dispatch handles
v1.0, v1.1, and v1.2 without breaking existing bundle files.

We decided against auto-repairing import collisions. A collision indicates a genuine
conflict between source and target state. Automatic resolution would hide data
divergence that the operator needs to investigate.

## Open Questions

When should a compression artifact be superseded versus invalidated?

How should the substrate handle a source document that is re-ingested after its
content has changed? Should the old ingestion run be linked to the new one?

What is the appropriate retention period for activation_decision_log rows?

Should governance-report be run automatically at export time, or only on demand?

## Source References

See: docs/CONTINUITY_BUNDLE_ARCHITECTURE.md for canonical bundle import semantics.
Ref: docs/RECOVERY_HANDBOOK.md for incident response procedures.
See: docs/MEMORY_GOVERNANCE_ARCHITECTURE.md for detection function reference.

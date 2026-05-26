---
author: substrate-demo
date: 2026-05-25
source_type: research_note
---

# Research Notes: Deterministic Retrieval and Compression

This document records hypotheses, experimental runs, and validation results
from the substrate research programme.

## Hypotheses

Hypothesis: if the context assembly budget is set below 15 events, retrieval
quality degrades significantly for queries spanning multiple evidence layers.

Hypothesis: compression artifacts derived from sessions with high unresolved
issue counts will have lower average compression_confidence ratings than
artifacts from clean sessions.

We posit that deterministic replay of the lineage event log produces
byte-identical execution state across platforms when timestamps are excluded
from the replay logic paths.

## Experiments

We backtested the retrieval filter against 500 historical assemblies and found
that suppress_unresolved=True reduces assembly size by 23% without discarding
any governance-rule events.

A/B test comparing retrievals with min_confidence=1 versus min_confidence=3:
retrieval at min_confidence=3 eliminated 34% of candidate events while retaining
all events with confidence 4 or 5.

We tested the bundle checksum stability by exporting the same database state
150 times across three platforms. Result: all 150 bundles produced identical
checksum_sha256 values.

Pilot study of Phase 6D compression-derived proposed filter: events sourced from
compression_artifact: prefix and with status=proposed are excluded by default
from bundle exports, reducing bundle size by an average of 8%.

## Validation Results

Result: Sharpe ratio of assembly quality improved by 1.4x after introducing
the doctrine priority ranking for retrieval.

Validated: lineage integrity checks pass on all 3150 hermetic tests. Zero FK
violations detected across test suite.

Result: The import collision detection catches 100% of content-addressed mismatches
in the test suite. Zero partial imports have been observed.

Drawdown in retrieval quality after governance rule events are excluded: -0.02,
which is within acceptable bounds.

## Regime Observations

Regime: the substrate is currently in a stable-doctrine phase. All governance
rules are in accepted or active status. No contradicts-linked pairs exist in
the active event set.

Risk-off posture applies to any schema migration: no migration may reduce the
number of rows in any table. Additive-only constraint enforced by convention.

## Adaptation Events

Adapted to multi-version source document tracking after observing that re-ingesting
a modified file without version tracking caused ambiguous provenance attribution.

Parameter update: the bundle_id derivation was updated in schema v1.2 to use the
manifest_without_checksum content dict, ensuring all recovery metadata fields are
covered by the checksum.

Recalibration: the memory event confidence scale was refined so that confidence=5
is reserved exclusively for governance-backed decisions with explicit human approval.

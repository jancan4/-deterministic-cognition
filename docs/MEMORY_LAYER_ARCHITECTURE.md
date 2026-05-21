# Memory Layer Architecture

**Status:** Implemented — Milestone 1  
**Date:** 2026-05-21  
**Governance:** AI may propose. Quant validation must validate. Risk engine has final veto.

---

## Event Lineage Philosophy

The memory layer stores `memory_events`, not chat logs.

Each event is a discrete, structured, typed unit of institutional knowledge with an explicit lineage:

- it was created at a specific time by a specific author
- it carries a confidence rating (1–5) representing evidential weight, not sentiment
- its status evolves through a governed transition path, not silent mutation
- every status change writes an immutable `memory_revisions` record that cannot be deleted
- relationships between events are explicit rows in `memory_links`, not inferred from proximity

This is event sourcing applied to institutional cognition. The current state of any belief is a deterministic function of the event log. Nothing is hidden, overwritten, or silently superseded.

---

## Why Structured Cognition, Not Chat Logs

Chat logs are linear, untyped, and non-replayable. A chat log cannot answer:

- Which decision was made and on what evidence?
- Who validated it?
- Was this hypothesis ever invalidated?
- What was the confidence level at decision time?
- Which open questions are still unresolved?

Structured memory events answer all of these directly. Each event has a `event_type` drawn from a closed vocabulary, a `source` that traces it to a document or session, a `confidence` integer that quantifies epistemic weight, and a `status` that reflects its current governance state.

The system stores governed state, not recall. There is a categorical difference:

| Property | Chat log | Memory event |
|---|---|---|
| Typed | No | Yes — 12 approved types |
| Queryable by type | No | Yes |
| Status-tracked | No | Yes — 8 approved statuses |
| Revisioned on change | No | Yes — `memory_revisions` |
| Confidence-rated | No | Yes — integer 1–5 |
| Linked to other beliefs | No | Yes — `memory_links` |
| Exportable deterministically | No | Yes |
| Subordinate to governance | Unclear | Explicit |

A chat log grows. A memory layer is governed. The distinction matters when the output of a reasoning system is used to inform decisions on research pipelines that must remain auditable.

---

## Why Deterministic Replay Matters

The pipeline this memory layer serves is itself deterministic. `narrative_dynamics`, `macro_regime_classifier`, and `market_state_field` produce the same output for the same input, always. This is not a performance property — it is a governance property. An auditor can replay any run and verify every number.

The memory layer must respect the same contract:

- UTC-only timestamps in ISO-8601 format
- All tables exported ordered by `id` ascending — autoincrement IDs are insertion-ordered, so this is also chronological
- JSON keys sorted alphabetically (`sort_keys=True`) — no dependency on dict ordering across Python versions
- Tags and related IDs stored sorted — the same set of tags always produces the same JSON
- No UUIDs, no hash-based ordering, no randomness anywhere in the write path

The consequence: given the same sequence of writes, the export file is byte-identical across machines, Python versions, and time. An auditor can checksum the export. A second system can ingest it. A diff between two exports reveals exactly what changed.

Non-deterministic memory would corrode the auditability of a deterministic pipeline. The memory layer must not be the weakest link in the audit chain.

---

## Relationship to the FX Pipeline Layers

The FX pipeline defines four evidence layers (from `docs/MARKET_STATE_ONTOLOGY.md`):

```
L0  Raw observations    — parsed_articles, rejected_articles
L1  Evidence signals    — narrative_mentions, article_signals, narrative_polarity
L2  Derived metrics     — narrative_dynamics, polarity confidence
L3  Inferred states     — macro_regime, market_state_field, currency_attribution
```

The memory layer sits orthogonally to these four layers. It does not process articles or score regimes. It stores:

- the decisions made about how L1–L3 are computed (architecture decisions)
- the governance rules that constrain L3 output (governance rules)
- open questions about L2 decay models (open questions)
- validation results confirming L3 regime labels against price data (validation results)
- incidents where the pipeline produced unexpected output (incidents)
- the rationale for calibration changes (adaptation)

It is the institutional memory of the people and systems that govern the pipeline, not a component of the pipeline itself.

---

## Governance Constraints (Non-Negotiable)

These constraints are permanent. They survive every future extension.

1. Retrieved memory never overrides uploaded doctrine, governance policy, or risk-engine vetoes.
2. A `hypothesis` event_type never implies validated fact. Status must reach `accepted` before any downstream use.
3. Confidence 5 (`authoritative / governance-backed`) may only be assigned to entries with a documented governance decision behind them.
4. `memory_revisions` rows are immutable. No delete or update operation is defined on them.
5. The memory layer has no execution path. It stores intent, evidence, and rationale. It does not trigger actions.
6. No broker integration. No live capital. No trading signals.

---

## Future Extension Points

The following extensions are anticipated but intentionally deferred to preserve simplicity at Milestone 1.

**Full-event revision tracking**  
`update-status` currently records only status and version changes. A future `update` command could record any field change, storing the full old/new event JSON in `memory_revisions`. The table schema already supports this — `old_value_json` and `new_value_json` are unconstrained JSON blobs.

**Tag taxonomy**  
Tags are currently free-form strings. A future `memory_tags` table could define a closed vocabulary with descriptions, enabling tag validation and tag-based governance groupings.

**Evidence chain tracing**  
`related_ids_json` links events to other events by ID. A future query could traverse this graph to produce an evidence chain for any `validation_result` or `architecture_decision`, surfacing all supporting hypotheses and experiments.

**Import / merge**  
The deterministic JSON export format was designed to be machine-readable. A future `import` command could ingest an export file and merge events into a target database, using `id`-based deduplication. This would enable distributed memory across sessions or machines.

**Calibration linkage**  
When Calibration v3 is implemented in the main pipeline, `validation_result` events in the memory layer could carry references to the specific calibration runs that produced them. This would close the audit loop between the pipeline's internal calibration table and the external memory layer.

**Per-event confidence decay**  
A future layer could flag events whose confidence should decay over time — for example, a `regime_observation` from 18 months ago that has not been superseded. This would require a decay schedule per event_type, which must be quant-validated before implementation.

None of these extensions change the core schema. They add tables or commands. The three-table structure (`memory_events`, `memory_links`, `memory_revisions`) is stable.

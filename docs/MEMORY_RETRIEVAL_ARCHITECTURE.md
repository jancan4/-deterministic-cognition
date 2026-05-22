# Memory Retrieval Architecture

**Status:** Implemented — Milestone 2  
**Date:** 2026-05-21  
**Governance:** AI may propose. Quant validation must validate. Risk engine has final veto.

---

## Retrieval Philosophy

The external memory layer stores institutional knowledge. Retrieval is the mechanism by which that knowledge is made available for reasoning — but retrieval must preserve the same determinism and governance properties as the storage layer.

The system answers: given a query, which stored events are most relevant, and in what order should they be presented?

That answer must be:
- **Deterministic** — the same query against the same database always produces the same ranked list
- **Governance-aware** — governance rules and architecture decisions outrank lower-priority signals regardless of confidence or recency
- **Auditable** — the ranking is a pure function of explicit, inspectable fields: `event_type`, `confidence`, `updated_at`, tag overlap, and `id`
- **Bounded** — the context builder enforces a character budget so assembled context does not exceed the capacity of downstream systems

---

## Why Deterministic Retrieval

Non-deterministic retrieval would corrode the auditability of the pipeline the memory layer serves.

If two runs of the same agent query with the same inputs and receive different context windows — because an embedding model produced subtly different nearest-neighbour orderings — then the agent's output is not reproducible. An auditor cannot replay the reasoning.

Determinism is achieved through a composite sort key that is a pure function of structured fields:

```
(is_expanded_flag, doctrine_rank, -confidence, recency_rank, -tag_overlap, event_id)
```

Each component is deterministic:
- `is_expanded_flag` — boolean, derived from whether the event was found via related-ID expansion
- `doctrine_rank` — integer from `DOCTRINE_PRIORITY` dict, a closed mapping from `event_type` to rank
- `confidence` — integer 1–5, stored as-is
- `recency_rank` — integer assigned by sorting `updated_at` descending (ties broken by ID, which is insertion-ordered)
- `tag_overlap` — integer count of shared tags between the event and the query's tag list
- `event_id` — integer primary key, unique per event, resolves all remaining ties

The composite key is sortable without randomness at every position.

---

## Why Embeddings Are Deferred

Semantic similarity search via embeddings was considered and explicitly deferred. The reasons:

1. **Non-determinism.** Embedding models are updated over time. A query that returns event A before event B today may reverse that ordering after a model update — with no change to the stored data. This is incompatible with the audit contract.

2. **External dependency.** Embeddings require either a local model (large, version-pinned) or an API call (network dependency, latency, cost). The memory layer has zero network calls. Introducing embeddings would break this.

3. **Governance opacity.** A neural similarity score is not inspectable. The governance model requires that a human can trace *why* an event was ranked where it was. A doctrine rank and a confidence integer are traceable. A cosine similarity score is not.

4. **The vocabulary is closed.** The 12 `event_type` values, 8 `status` values, and free-form tag system already provide sufficient dimensionality for structured filtering. Tag-overlap scoring provides query-time relevance without a model.

Embeddings remain a future extension path. If implemented, they must be versioned, pinned, and produce a deterministic score given a fixed model version — or their output must be stored alongside events so that retrieval remains reproducible.

---

## Governance-Aware Context Injection

Retrieval is not neutral. The order in which information is presented to a reasoning system affects its outputs. A governance rule that is buried after 15 lower-priority events may be underweighted. A hypothesis presented before the governance rule that constrains it may appear authoritative.

The doctrine priority ranking is the remedy. It encodes the governance hierarchy of the FX system:

| Rank | Event Type | Rationale |
|------|-----------|-----------|
| 1 | `governance_rule` | Non-negotiable constraints. Must be visible first. |
| 2 | `architecture_decision` | Structural choices that constrain all downstream work. |
| 3 | `validation_result` | Empirical confirmation. Evidence, not speculation. |
| 4 | `adaptation` | Approved calibration changes. Reflects current operational state. |
| 5 | `hypothesis` | Unvalidated speculation. Must appear after confirmed evidence. |
| 6 | `implementation_note` | Detail. Subordinate to architecture decisions. |
| 7+ | All others | Contextual. No privileged position. |

This ranking is hardcoded, not configurable at runtime. Allowing a query to override the doctrine priority would allow a caller to suppress governance rules — which is the scenario the governance model is designed to prevent.

---

## Replayable Cognition Assembly

The context builder converts a ranked list of `ScoredEvent` objects into a structured text or dict representation suitable for injection into a prompt or system message.

Assembly properties:
- **Section order is fixed.** The six sections appear in the same order regardless of which events are present: GOVERNANCE CONTEXT → ARCHITECTURE CONTEXT → ACTIVE QUESTIONS → RECENT ADAPTATIONS → RELATED EXPERIMENTS → RELEVANT MEMORY EVENTS. This reflects the governance priority hierarchy in a human-readable structure.
- **Budget enforcement is greedy from the front.** Events are filled into sections in composite-key order. When the char budget is exhausted, remaining events are dropped. The most important events (lowest composite key) are always included first.
- **Empty sections are omitted from text output.** `to_text()` only renders sections that have at least one entry. `to_dict()` preserves all six section slots for structural consistency.
- **The output is deterministic.** Given the same `ScoredEvent` list and the same budget, `to_text()` and `to_dict()` produce identical output on every call.

---

## Related-Event Expansion

Primary retrieval returns events that directly match the query. Expansion adds events that are *connected* to primary results via:
1. `related_ids_json` — explicit ID references stored on the primary event
2. `memory_links` rows — directed relationships in the `memory_links` table

Expanded events are flagged `is_expanded=True` and receive `is_expanded_flag=1` in their composite key. This guarantees that all primary results sort before all expanded results, regardless of their confidence or doctrine rank.

Expansion is bounded by the `expand_related` flag on `RetrievalQuery`. It is disabled for the `retrieve_unresolved`, `retrieve_adaptations`, and `retrieve_governance` convenience functions, which target a specific event type or status and do not benefit from lateral expansion.

---

## Relationship to the Storage Layer

`retrieval.py` is read-only. It issues only `SELECT` queries against the database. It does not write, update, or delete any rows. It does not call `service.py` functions for writes.

The `_fetch_candidates` and `_expand_related` functions open their own SQLite connections directly rather than routing through `service.py`. This avoids the overhead of full `MemoryEvent` dataclass construction for rows that will be filtered out before scoring, and keeps the retrieval module dependency-free from the write path.

---

## Future Extension Points

**Embedding-based reranking (deferred)**  
A future layer could compute a semantic similarity score and inject it into the composite key — but only if the score is versioned, pinned, and stored alongside the event so that retrieval remains reproducible. The composite key tuple is designed to accommodate additional fields.

**Temporal decay**  
A future scoring component could penalise events whose `updated_at` timestamp is older than a configurable threshold. This would require a decay schedule per `event_type`, which must be quant-validated before implementation.

**Cross-database retrieval**  
If multiple memory databases are maintained (per-session, per-project), a future aggregation layer could merge ranked results from multiple sources. The deterministic sort key would still apply within each source; merge ordering would require an additional tiebreak on the source identifier.

**Calibration linkage**  
When Calibration v3 is implemented in the main pipeline, `validation_result` events retrieved by the memory layer could carry references to specific calibration run IDs. This would close the audit loop between the pipeline's internal state and the external memory layer's evidence chain.

# Semantic Extraction Interface Architecture

## Why This Layer Exists Before Local Models

The ingestion pipeline already has deterministic rule-based extraction
(`ingestion/extractor.py`). Rules produce `CandidateMemoryEvent` objects
that an operator can review and commit to `memory_events`.

The next step in intelligence maturity is **local model inference** —
running a small language model (e.g. Phi-3 Mini, Mistral-7B) entirely
on-device, with no network calls, to extract richer signals from source
text. But local model integration has two hard prerequisites:

1. **Stable contracts** — the rest of the system must not change when the
   model changes. Swapping Phi-3 for Mistral must not touch the memory
   layer, the ingestion layer, or the continuity layer.

2. **Validation boundary** — model outputs are unverified text. They need
   a deterministic validator that enforces structure, confidence bounds,
   span ranges, and provenance requirements *before* any result touches
   the rest of the system.

This `semantic/` package defines those contracts now, so that:

- Rule-based extractors can already produce `SemanticExtractionResult`
  objects and have them validated through the same path as future model
  outputs.
- Future local model adapters have a clear, stable interface to implement.
- The operator review workflow (candidates → commit) is unchanged.

---

## Model-Agnostic Semantic Contracts

The package defines four layers:

```
semantic/models.py      — data structures (no logic)
semantic/validators.py  — deterministic validation (no side effects)
semantic/contracts.py   — factories and integration bridge (no writes)
```

### Data structures (`models.py`)

| Type | Purpose |
|---|---|
| `SemanticTask` | One extraction request (type + input text + provenance anchor) |
| `SemanticSpan` | Character offsets (start, end) into `input_text` |
| `SemanticProvenance` | Source attribution — always requires `extraction_method` |
| `SemanticLabel` | A categorical tag/label with confidence and rationale |
| `ExtractedEntity` | A named entity with type, span, confidence |
| `ExtractedClaim` | A factual/evaluative claim with polarity and confidence |
| `ExtractedRelation` | A directed (subject, predicate, object) triple |
| `SemanticExtractionResult` | Full output of one task: labels + entities + claims + relations |

### Confidence model

Integer 1–5, matching `memory_events.confidence`. This is intentional:
results that become candidates inherit the same confidence scale used
throughout the memory layer.

### Provenance

Every `SemanticExtractionResult` carries a `SemanticProvenance` object
with:
- `extraction_method` (required, non-empty) — e.g. `"rule_based"`,
  `"keyword"`, `"local_model:phi3-mini-4k"`.
- `source_id` (required when the task is source-bound).
- `source_span` (optional; character offsets in the source document).
- `model_id` (reserved for future local model identifiers).

Provenance is never optional. Every result must declare how it was produced.

### Approved task types

```python
SEMANTIC_TASK_TYPES = (
    'tagging',
    'polarity_classification',
    'entity_extraction',
    'claim_extraction',
    'relation_extraction',
    'summary_extraction',
    'clustering_hint',
    'memory_candidate_classification',
)
```

Any result with a task type outside this set is rejected by `validate_result()`.

---

## Validation Boundary (`validators.py`)

All validators are pure functions — no database access, no network, no
model calls. They enforce:

| Rule | Enforced by |
|---|---|
| `task_type` must be an approved value | `validate_task()` |
| `input_text` must not be empty | `validate_task()` |
| `confidence` must be integer 1–5 | `validate_confidence()` |
| Labels must have non-empty strings | `validate_label()` |
| Spans must satisfy `0 <= start < end <= len(input_text)` | `validate_span()` |
| Provenance `extraction_method` must be non-empty | `validate_provenance()` |
| Provenance `source_id` required when task is source-bound | `validate_provenance()` |
| `result.task_id` must match `task.task_id` | `validate_result()` |
| Polarity must be `positive/negative/neutral/uncertain` | `validate_claim()` |

The validator is the **only place where unverified model output is
sanitised**. A future local model adapter must pass its output through
`validate_result(result, task)` before the result is used anywhere else.

---

## Future Local Model Adapter Path

A local model adapter is a thin wrapper that:

1. Receives a `SemanticTask` from `make_task()`.
2. Runs inference locally (no network, no cloud API).
3. Parses the model's output into `SemanticExtractionResult` fields.
4. Calls `validate_result(result, task)` to verify the output structure.
5. Returns the validated `SemanticExtractionResult`.

```python
# Future adapter (not implemented yet)
class Phi3MiniAdapter:
    MODEL_ID = 'phi3-mini-4k-instruct'

    def extract(self, task: SemanticTask) -> SemanticExtractionResult:
        raw_output = self._run_inference(task.input_text, task.task_type)
        result = self._parse(raw_output, task)         # adapter-specific
        validate_result(result, task)                  # governed boundary
        return result
```

Changing from Phi-3 to Mistral means replacing only `Phi3MiniAdapter`.
The `SemanticTask`, `SemanticExtractionResult`, validators, and downstream
candidate/memory layers are unchanged.

---

## Why Results Become Candidates, Not Memory Truth

`SemanticExtractionResult` objects are **proposals**, not facts. The same
architectural decision applies here as in the rule-based ingestion pipeline:

- A model may hallucinate, misclassify, or over-generalise.
- An operator (or a future automated review step with its own governance)
  must validate the extraction before it becomes a `memory_event`.
- Continuity bundles contain committed memory — never raw model output.

The integration bridge is `result_to_candidate()` in `contracts.py`:

```
SemanticExtractionResult
        │
        ▼ result_to_candidate()
CandidateMemoryEvent  (status='proposed', committed_id=None)
        │
        ▼ operator review / ingestion.candidates.commit_candidates()
memory_events  (canonical truth)
```

`result_to_candidate()` is the **only** function that crosses from the
semantic layer into the ingestion layer. It performs no database writes.
The caller controls whether and when to commit.

---

## Connection to Ingestion and Continuity Bundles

### Ingestion

`result_to_candidate()` returns a `CandidateMemoryEvent` — the same type
produced by rule-based extraction. The existing `commit_candidates()` and
`record_run()` functions work unchanged. Semantic extraction is an
additional source of candidates, not a parallel write path.

### Continuity bundles

Continuity bundles snapshot `memory_events` + `source_documents` +
`ingestion_runs`. Because semantic results become candidates first, and
candidates only become `memory_events` after operator commit, semantic
extraction results never appear directly in bundles. The provenance chain
(`source_id → ingestion_run → memory_event`) is preserved regardless of
whether the candidate originated from a rule or a model.

---

## Determinism Guarantees

- `derive_task_id(task_type, input_text, source_id)` → same inputs → same
  16-char hex task_id.
- `SemanticExtractionResult.to_json()` uses `json.dumps(sort_keys=True,
  ensure_ascii=True)` — same result object → same JSON string.
- Validators have no state — repeated calls produce identical results.
- No timestamps or random values appear in task_id or result content
  (only in `created_at` / `extracted_at` fields, which are metadata).

---

## Invariants

- No function in `semantic/` writes to any database.
- No function in `semantic/` makes network calls.
- No function in `semantic/` loads or runs a model.
- `result_to_candidate()` sets `status='proposed'` and `committed_id=None`
  — unconditionally.
- Confidence is always integer 1–5.
- `extraction_method` is always non-empty.
- Invalid task types, empty inputs, bad confidence, and out-of-range spans
  all raise `SemanticValidationError` before any downstream code runs.

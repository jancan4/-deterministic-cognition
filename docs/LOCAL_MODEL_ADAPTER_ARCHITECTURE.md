# Local Model Adapter Architecture

## Why Adapters Exist Before Inference

The semantic extraction interface layer (`semantic/`) defines stable
contracts for extraction tasks and results. But a contract without a
caller is half a system. The adapter layer answers: *who calls the
contract, and how is that call governed?*

Local model adapters bridge the deterministic semantic contracts and
whatever inference engine is eventually chosen — `llama.cpp`, Ollama,
`transformers`, a subprocess, or a quantised GGUF model file. By defining
this bridge *before* choosing a model, we guarantee:

1. The governance architecture is never shaped by model-specific quirks.
2. Model selection is a deployment decision, not an architecture decision.
3. Every inference result passes through the same validation boundary,
   regardless of which model produced it.
4. Tests run in milliseconds with stub adapters while the real inference
   infrastructure is still being evaluated.

---

## Replaceable Model Philosophy

```
SemanticTask
    │
    ▼ task_to_request(task, model_name, model_version)
LocalModelRequest  ──────────────────────────────────────┐
    │                                                     │
    ▼ adapter.execute(request)                            │ same envelope
LocalModelResponse  ──────────────────────────────────────┘
    │
    ▼ response_to_semantic_result(response, task)
SemanticExtractionResult  (validated)
    │
    ▼ result_to_candidate(result, task, event_type, title)
CandidateMemoryEvent  (status='proposed', committed_id=None)
    │
    ▼ operator review + commit_candidates()
memory_events  (canonical truth)
```

Every arrow in this chain is a governed, validated boundary. Replacing
`Phi3MiniAdapter` with `MistralAdapter` changes only the adapter
implementation. The request envelope, response envelope, semantic result,
and candidate path are unchanged.

### What changes when you change the model

| Layer | Changes? |
|---|---|
| `SemanticTask` / `SemanticExtractionResult` | No — stable contracts |
| `LocalModelRequest` / `LocalModelResponse` | No — same envelopes |
| `validate_result()` / `validate_response()` | No — same validation |
| `LocalModelAdapter` subclass | Yes — only this |
| `ModelCapabilitySet` declaration | Yes — capability limits may differ |
| `execute_with_policy()` wrapper | No — unchanged |
| `memory_events` write path | No — always through `commit_candidates()` |

---

## Governance-Safe Model Boundaries

### No automatic commits

No model response is ever written directly to `memory_events`. The path
is always:

```
adapter.execute() → SemanticExtractionResult → CandidateMemoryEvent(proposed)
```

An operator (human or governed automated reviewer) must call
`commit_candidates()` explicitly.

### Validation at every boundary

```
1. validate_request(request)         — before adapter.execute()
2. check_model_supports(adapter, ...)— capability check
3. adapter.execute(request)          — model call (stub in this milestone)
4. validate_response(response, req)  — response structure and consistency
5. response_to_semantic_result()     — semantic validation via validate_result()
6. CandidateMemoryEvent              — ingestion-layer validation
```

A malformed or hallucinated model output is caught at step 4 or 5, before
it touches the semantic or memory layer.

### Explicit capabilities — no implicit inference

Every adapter declares its `ModelCapabilitySet` explicitly. An adapter is
never assumed to support a task type. If `check_model_supports()` returns
a `CapabilityError`, the execution is refused before any inference runs.

Each capability also declares `max_input_chars`: input that exceeds this
limit is refused at the governance layer, not silently truncated by the
model.

---

## Candidate-vs-Truth Doctrine

A `SemanticExtractionResult` is a proposal, not a fact. The same
doctrine applies here as in rule-based extraction:

- Models can hallucinate, over-generate, or misclassify.
- Model outputs are proposals of what *might* be true, not assertions of
  what *is* true.
- Canonical truth lives only in `memory_events` after operator review.
- Continuity bundles contain committed memory — never raw model output.

This is enforced mechanically: `result_to_candidate()` unconditionally
sets `status='proposed'` and `committed_id=None`. There is no other way
to produce a candidate from a semantic result.

---

## Execution Policy

`ModelExecutionPolicy` governs every adapter call:

| Field | Default | Purpose |
|---|---|---|
| `timeout_seconds` | 30 | Max wall-clock time for one execute() call |
| `max_retries` | 0 | Retry count on transient ModelContractError |
| `retry_delay_seconds` | 1.0 | Pause between retries (adapter-enforced) |
| `deterministic_mode` | True | Request reproducible output (e.g. temperature=0) |

`execute_with_policy()` captures full execution metadata in
`ModelExecutionResult`:

```json
{
  "request_id": "a1b2c3d4e5f60708",
  "adapter_name": "phi3-mini",
  "adapter_version": "1.0.0",
  "started_at": "2026-01-01T12:00:00Z",
  "completed_at": "2026-01-01T12:00:01Z",
  "duration_ms": 312.5,
  "timeout_applied": false,
  "retry_count": 0,
  "success": true,
  "error": null
}
```

This metadata is preserved as lineage context alongside the candidate.

---

## Determinism Guarantees

- `derive_request_id(model_name, model_version, task_type, input_text)` →
  same inputs → same 16-char hex id.
- `LocalModelRequest.to_json()` and `LocalModelResponse.to_json()` use
  `json.dumps(sort_keys=True, ensure_ascii=True)`.
- `StubModelAdapter.execute()` produces identical output for identical
  input — no randomness, no state.
- `EchoModelAdapter.execute()` derives labels from input text via
  deterministic regex — same text → same labels.

---

## OllamaAdapter — Implemented Real Inference Adapter

`models/ollama_adapter.py` implements `OllamaAdapter`, the first real-inference
adapter in this system. It calls the Ollama HTTP API (`/api/generate`) on a
local process and parses the structured JSON response into `LocalModelResponse`.

### Determinism and replay semantics

Ollama inference is **provenance-preserved but NOT bitwise deterministic**.

| Property | Deterministic? | How |
|---|---|---|
| `request_id` | Yes | sha256(model_name + NUL + version + NUL + task_type + NUL + input_text)[:16] |
| Prompt template | Yes | Fixed per task_type in `_PROMPT_TEMPLATES` |
| `prompt_template_hash` | Yes | sha256(template)[:16] |
| `request_payload_hash` | Yes | sha256(json.dumps(payload))[:16] |
| Token sequence | No | Depends on model version, quantization, hardware |
| Extracted labels / entities / claims | No | Derived from token sequence |

`ModelCapability.deterministic_mode_supported = False` for OllamaAdapter.
This is an honest declaration: even with `temperature=0` and `seed=42`,
different Ollama versions or quantisation formats may produce different tokens.

**Canonical truth is the recorded artifact in the semantic ledger**
(`raw_output_json`, `normalized_result_json`), not a re-query of Ollama.
Replaying a run means reading the ledger row, not regenerating tokens.

### Provenance metadata

Every `execute()` call populates `LocalModelResponse.metadata` with a complete
provenance dict:

```json
{
  "adapter_name": "ollama",
  "adapter_version": "1.0.0",
  "provider": "ollama",
  "runtime_version": "0.3.0",
  "model_name": "phi3:mini",
  "model_digest": "sha256:...",
  "model_family": "phi3",
  "temperature": 0.0,
  "seed": 42,
  "num_predict": 512,
  "prompt_template_hash": "a1b2c3d4e5f60708",
  "request_payload_hash": "b2c3d4e5f6070809",
  "input_hash": "c3d4e5f607080901",
  "started_at": "2026-01-01T12:00:00Z",
  "completed_at": "2026-01-01T12:00:01Z",
  "duration_ms": 312.5,
  "ollama_eval_count": 42,
  "ollama_eval_duration_ns": 1000000,
  "parse_error": null,
  "raw_output": {
    "raw": "<full ollama response text>",
    "model": "phi3:mini",
    "done": true,
    "eval_count": 42,
    "ollama_response_payload": { ... }
  }
}
```

`raw_output` is passed through to `record_run(raw_output=...)` and
persisted in `semantic_execution_runs.raw_output_json`.

### Import safety

`models/ollama_adapter.py` is safe to import without `requests` installed.
`OllamaAdapter` raises `ModelContractError` at **instantiation** (not at import).

### CLI usage

```
# semantic-run with Ollama
python -m cli.main semantic-run \
  --adapter ollama --model phi3:mini \
  --task-type tagging --input-text "The Fed held rates."

# ingest-file with Ollama semantic enrichment
python -m cli.main ingest-file /data/article.txt \
  --semantic-adapter ollama --model phi3:mini [--commit]
```

### Manual smoke test (requires live Ollama)

```bash
ollama pull phi3:mini  # one-time pull
python -m cli.main semantic-run \
  --adapter ollama --model phi3:mini \
  --task-type tagging \
  --input-text "The Federal Reserve held interest rates steady on Wednesday."
```

### Future adapter path for llama.cpp

```python
class LlamaCppAdapter(LocalModelAdapter):
    NAME = 'llama-cpp'
    VERSION = '1.0.0'

    def __init__(self, model_path: str):
        self._model_path = model_path  # local GGUF file, no network

    def execute(self, request: LocalModelRequest) -> LocalModelResponse:
        validate_request(request)
        # 1. Build prompt from request.task_type + request.input_text
        # 2. Run llama.cpp subprocess (local only, no network)
        # 3. Parse structured output into SemanticLabel / ExtractedEntity / etc.
        # 4. Return LocalModelResponse
        ...
```

---

## Why Embeddings and Vector Retrieval Are Still Deferred

Embeddings and vector search are a different capability class:

| This milestone | Embeddings (deferred) |
|---|---|
| Text-in → structured-out | Text-in → dense vector-out |
| Deterministic candidates | Approximate nearest-neighbours |
| Operator-reviewed | Implicit retrieval (no review step) |
| Fits governance model | Requires separate retrieval governance |

Vector retrieval changes the memory read path (currently SQL), introduces
similarity thresholds that must be governed, and requires embedding model
versioning that must be tracked in provenance. These are non-trivial
governance additions.

The right time to add embeddings is after: local model execution is
validated, the candidate review workflow is stable, and the embedding
model is itself a governed, versioned artifact with its own provenance
chain.

---

## Package Structure

```
models/
  __init__.py
  capabilities.py    ModelCapability, ModelCapabilitySet, build_full_capability_set
  contracts.py       LocalModelRequest, LocalModelResponse, ModelExecutionPolicy,
                     ModelExecutionResult, task_to_request, response_to_semantic_result
  adapters.py        LocalModelAdapter (ABC), StubModelAdapter, EchoModelAdapter
  execution.py       execute_with_policy, DEFAULT_POLICY, make_policy
  tests/
    test_capabilities.py
    test_contracts.py
    test_adapters.py
    test_execution.py
```

## Invariants

- No function in `models/` writes to any database.
- No function in `models/` makes external network calls.
- No function in `models/` loads model weights or runs real inference.
- `execute_with_policy()` is the single entry point for adapter execution.
- `response_to_semantic_result()` validates before returning.
- `result_to_candidate()` always produces `status='proposed'`.
- `ModelCapabilitySet` must be declared explicitly by every adapter.
- `extraction_method` is always non-empty and always carried through.

"""
Semantic workflow node handler.

Connects the workflow execution layer to the semantic pipeline for
``semantic_extraction`` workflow nodes.

Task payload schema
-------------------
A ``WorkflowNode`` with ``task_type='semantic_extraction'`` carries its
execution parameters in ``task_payload_json``::

    {
        "task_type": "tagging",           # required — semantic task type
        "adapter":   "stub",              # required — adapter name
        "model":     null,                # optional — ollama model (e.g. "phi3:mini")
        "base_url":  "http://localhost:11434",  # optional — ollama base URL
        "source_id": null,                # optional — provenance source_id
        "commit":    false                # optional — if true, promote to memory
    }

Execution contract
------------------
``execute_semantic_node()`` is the single entry point:

1. ``parse_semantic_payload()`` validates ``task_payload_json``.
2. ``resolve_adapter()`` instantiates the adapter (import-safe for OllamaAdapter).
3. ``run_semantic_task()`` executes via the semantic pipeline (no DB writes).
4. ``ledger.record_run()`` persists the run idempotently to the semantic ledger.
5. If ``commit=True``, ``ledger.promote_candidate()`` promotes candidates to
   ``memory_events`` with ``status='unresolved'``. No further status changes occur.
6. Returns ``SemanticNodeResult`` whose ``lineage_metadata`` is suitable for
   embedding in the ``node_completed`` workflow lineage event.

Governance invariants
---------------------
- Model output is **never** canonical memory. Every promotion creates
  ``memory_events.status='unresolved'`` only.
- ``update_status('active')`` is never called here. That is operator-exclusive
  via ``memory-review approve``.
- ``commit=False`` (the default) produces ledger rows only; no memory write occurs.

Recovery and replay semantics
------------------------------
- ``record_run()`` is idempotent: ``INSERT OR IGNORE`` on ``run_id``. A crashed
  or replayed node can call ``execute_semantic_node()`` again safely.
- Candidate rows use ``INSERT OR IGNORE`` on ``candidate_id``. Already-generated
  candidates are skipped, not duplicated.
- Already-promoted candidates have ``status != 'candidate'``; ``promote_candidate()``
  raises ``LedgerError`` for them. The handler catches this per candidate and skips,
  so a partially-promoted run resumes by promoting only the remaining candidates.
- **Workflow replay does not call adapters.** When ``replay_execution()`` processes
  a ``node_completed`` event, the node is placed in ``completed_node_ids`` and
  ``get_ready_node_ids()`` will not re-surface it. The canonical semantic artifact
  is the ``semantic_execution_runs`` ledger row, looked up by ``semantic_run_id``
  from the lineage event metadata. Re-extraction requires a new workflow execution.
"""
import json
from dataclasses import dataclass, field
from typing import Dict, List, Optional


class SemanticHandlerError(ValueError):
    """Raised when handler configuration or execution fails."""


# ---------------------------------------------------------------------------
# Result model
# ---------------------------------------------------------------------------

@dataclass
class SemanticNodeResult:
    """
    Output of one ``execute_semantic_node()`` call.

    ``lineage_metadata`` is ready to embed in a ``WorkflowExecutionLineageEvent``
    as the ``metadata`` dict for the corresponding ``node_completed`` event.
    """
    run_id: str
    candidate_ids: List[str]
    promoted_memory_ids: List[int]
    success: bool
    error: Optional[str]
    lineage_metadata: Dict

    @classmethod
    def failure(cls, error: str) -> 'SemanticNodeResult':
        return cls(
            run_id='',
            candidate_ids=[],
            promoted_memory_ids=[],
            success=False,
            error=error,
            lineage_metadata={'error': error},
        )


# ---------------------------------------------------------------------------
# Payload parsing
# ---------------------------------------------------------------------------

_REQUIRED_PAYLOAD_FIELDS = ('task_type', 'adapter')

_VALID_SEMANTIC_TASK_TYPES = (
    'tagging',
    'polarity_classification',
    'entity_extraction',
    'claim_extraction',
    'relation_extraction',
    'summary_extraction',
    'clustering_hint',
    'memory_candidate_classification',
    'event_extraction',
)


def parse_semantic_payload(task_payload_json: str) -> dict:
    """
    Parse and validate the ``task_payload_json`` of a semantic_extraction node.

    Returns a normalized payload dict. Raises ``SemanticHandlerError`` on any
    parsing or validation failure so the caller can mark the node as failed
    before touching any adapter or ledger.
    """
    try:
        payload = json.loads(task_payload_json or '{}')
    except (json.JSONDecodeError, ValueError) as exc:
        raise SemanticHandlerError(f"task_payload_json is not valid JSON: {exc}") from exc

    if not isinstance(payload, dict):
        raise SemanticHandlerError(
            f"task_payload_json must be a JSON object, got {type(payload).__name__}"
        )

    for field_name in _REQUIRED_PAYLOAD_FIELDS:
        if not payload.get(field_name):
            raise SemanticHandlerError(
                f"task_payload_json missing required field: {field_name!r}"
            )

    task_type = payload['task_type']
    if task_type not in _VALID_SEMANTIC_TASK_TYPES:
        raise SemanticHandlerError(
            f"Invalid semantic task_type {task_type!r}. "
            f"Must be one of: {sorted(_VALID_SEMANTIC_TASK_TYPES)}"
        )

    adapter = payload['adapter']
    if not isinstance(adapter, str) or not adapter.strip():
        raise SemanticHandlerError("task_payload_json 'adapter' must be a non-empty string")

    return {
        'task_type': task_type,
        'adapter': adapter.strip(),
        'model': payload.get('model') or None,
        'base_url': payload.get('base_url') or 'http://localhost:11434',
        'source_id': payload.get('source_id') or None,
        'commit': bool(payload.get('commit', False)),
        'input_text': payload.get('input_text') or '',
    }


# ---------------------------------------------------------------------------
# Adapter resolution
# ---------------------------------------------------------------------------

def resolve_adapter(payload: dict):
    """
    Instantiate the adapter named in ``payload['adapter']``.

    Import-safe: OllamaAdapter is imported only when needed, and only if
    ``requests`` is available (failure deferred to instantiation). Raises
    ``SemanticHandlerError`` on any resolution failure.

    Returns a ``LocalModelAdapter`` instance.
    """
    adapter_name = payload['adapter']

    if adapter_name == 'ollama':
        model = payload.get('model')
        if not model:
            raise SemanticHandlerError(
                "adapter='ollama' requires 'model' in task_payload_json "
                "(e.g. \"model\": \"phi3:mini\")"
            )
        try:
            from models.ollama_adapter import OllamaAdapter
            from models.contracts import ModelContractError
            return OllamaAdapter(
                model=model,
                base_url=payload.get('base_url', 'http://localhost:11434'),
            )
        except Exception as exc:
            raise SemanticHandlerError(
                f"Could not instantiate OllamaAdapter: {exc}"
            ) from exc

    # Built-in adapters (stub, echo)
    try:
        from models.registry import make_default_registry
        registry = make_default_registry()
        return registry.get(adapter_name)
    except Exception as exc:
        raise SemanticHandlerError(
            f"Could not resolve adapter {adapter_name!r}: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# Core handler
# ---------------------------------------------------------------------------

def execute_semantic_node(
    task_payload_json: str,
    db_path: str,
    actor: str = 'semantic-workflow',
) -> SemanticNodeResult:
    """
    Execute one ``semantic_extraction`` workflow node end-to-end.

    Steps
    -----
    1. ``parse_semantic_payload()`` — validate payload before touching any system.
    2. ``resolve_adapter()`` — instantiate adapter (import-safe).
    3. ``run_semantic_task()`` — execute via semantic pipeline (no DB writes).
    4. ``ledger.record_run()`` — persist run to semantic ledger (idempotent).
    5. If ``commit=True``, promote each candidate via ``promote_candidate()``:
       - Already-promoted candidates (``status != 'candidate'``) are skipped.
       - Promotion writes ``memory_events.status='unresolved'`` only.
       - ``update_status('active')`` is never called.
    6. Return ``SemanticNodeResult`` including ``lineage_metadata`` for embedding
       in the ``node_completed`` workflow lineage event.

    Recovery invariants
    -------------------
    - ``record_run()`` is idempotent on ``run_id``. Rerunning after a crash
      skips the ledger insert and returns the existing row.
    - Candidate inserts are idempotent on ``candidate_id``. Already-generated
      candidates are skipped, not duplicated.
    - Already-promoted candidates raise ``LedgerError``; the handler catches
      this per candidate and skips, so a partial resume promotes only remaining.

    Replay contract
    ---------------
    This function is **never called during workflow replay**. Replay reconstructs
    state from ``WorkflowExecutionLineageEvent`` objects — when it encounters
    ``node_completed``, the node is marked done without re-executing. Callers
    must not invoke ``execute_semantic_node()`` on an already-completed node.
    """
    # Step 1: parse payload
    try:
        payload = parse_semantic_payload(task_payload_json)
    except SemanticHandlerError as exc:
        return SemanticNodeResult.failure(str(exc))

    # Step 2: resolve adapter
    try:
        adapter = resolve_adapter(payload)
    except SemanticHandlerError as exc:
        return SemanticNodeResult.failure(str(exc))

    # Step 3: run semantic pipeline (no DB writes)
    from semantic.pipeline import run_semantic_task
    from semantic.validators import SemanticValidationError

    try:
        pipeline_result = run_semantic_task(
            task_type=payload['task_type'],
            input_text=_get_input_text(payload),
            adapter=adapter,
            source_id=payload.get('source_id'),
            created_by=actor,
        )
    except SemanticValidationError as exc:
        return SemanticNodeResult.failure(f"Semantic validation error: {exc}")
    except Exception as exc:
        return SemanticNodeResult.failure(f"Semantic pipeline error: {exc}")

    # Step 4: persist to semantic ledger (idempotent)
    from semantic.ledger import (
        derive_candidate_id,
        init_ledger,
        record_run as _record_run,
    )

    init_ledger(db_path)

    # Capture raw_output from Ollama adapter metadata when available
    raw_out = None
    er = pipeline_result.execution_result
    if er.response and er.response.metadata:
        raw_out = er.response.metadata.get('raw_output')

    ledger_run = _record_run(db_path, pipeline_result, raw_output=raw_out)

    # Collect candidate_ids from ledger (same derivation as record_run)
    run_id = ledger_run.run_id
    candidate_ids = [
        derive_candidate_id(run_id, idx)
        for idx in range(len(pipeline_result.candidates))
    ]

    # Step 5: optionally promote candidates to unresolved memory
    promoted_memory_ids: List[int] = []
    if payload['commit'] and candidate_ids:
        from memory import service as _mem_service
        from semantic.ledger import LedgerError, promote_candidate as _promote_candidate

        _mem_service.init_db(db_path)

        for cid in candidate_ids:
            try:
                mid = _promote_candidate(db_path, cid, approved_by=actor)
                promoted_memory_ids.append(mid)
            except LedgerError:
                # Already promoted or already rejected — skip deterministically.
                # A candidate that was promoted in a previous (partial) run
                # retains its promoted_memory_id; we do not re-promote it.
                pass

    # Step 6: build lineage metadata and return
    lineage_metadata: Dict = {
        'semantic_run_id': run_id,
        'candidate_ids': candidate_ids,
        'promoted_memory_ids': promoted_memory_ids,
        'adapter_name': er.adapter_name,
        'adapter_version': er.adapter_version,
        'task_type': payload['task_type'],
        'committed': payload['commit'],
    }
    if payload.get('model'):
        lineage_metadata['model'] = payload['model']

    error = pipeline_result.error if not pipeline_result.success else None

    return SemanticNodeResult(
        run_id=run_id,
        candidate_ids=candidate_ids,
        promoted_memory_ids=promoted_memory_ids,
        success=pipeline_result.success,
        error=error,
        lineage_metadata=lineage_metadata,
    )


def _get_input_text(payload: dict) -> str:
    """
    Resolve input text from payload.

    ``input_text`` can be embedded in the payload for deterministic replay.
    If absent, returns an empty string — the pipeline will raise
    ``SemanticValidationError`` which is caught by the caller.
    """
    return payload.get('input_text') or ''

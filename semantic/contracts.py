"""
Semantic extraction interface contracts.

This module provides:
  1. make_task()           — factory to create validated SemanticTask instances
  2. result_to_candidate() — converts SemanticExtractionResult to a
                             CandidateMemoryEvent (proposed status, no write)

Integration boundary
--------------------
result_to_candidate() is the ONLY bridge between the semantic layer and the
ingestion/memory layers. It produces a CandidateMemoryEvent with:
  - status='proposed'  (operator review required before any commit)
  - committed_id=None  (not written to memory_events)

No function in this module writes to any database. The caller (CLI or
operator) decides whether to commit the resulting candidate via
ingestion.candidates.commit_candidates().

Future local model adapter path
--------------------------------
A local model adapter would:
  1. Receive a SemanticTask from make_task()
  2. Run inference (outside this module)
  3. Construct a SemanticExtractionResult with extraction_method='local_model:<id>'
  4. Call validate_result(result, task) to verify the output
  5. Optionally call result_to_candidate() to produce a reviewable candidate

The adapter is the only code that changes when switching models. The
contracts (SemanticTask, SemanticExtractionResult) remain stable.
"""
from typing import List, Optional

from .models import (
    EXTRACTION_METHOD_RULE_BASED,
    SemanticExtractionResult,
    SemanticProvenance,
    SemanticSpan,
    SemanticTask,
    _now,
    derive_task_id,
)
from .validators import SemanticValidationError, validate_result, validate_task


# ---------------------------------------------------------------------------
# Task factory
# ---------------------------------------------------------------------------

def make_task(
    task_type: str,
    input_text: str,
    source_id: Optional[str] = None,
    source_span: Optional[SemanticSpan] = None,
    metadata: Optional[dict] = None,
) -> SemanticTask:
    """
    Create and validate a SemanticTask.

    task_id is derived deterministically from (task_type, input_text, source_id).
    Raises SemanticValidationError if the task is structurally invalid.
    """
    task = SemanticTask(
        task_id=derive_task_id(task_type, input_text, source_id or ''),
        task_type=task_type,
        input_text=input_text,
        source_id=source_id,
        source_span=source_span,
        metadata=dict(metadata) if metadata else {},
        created_at=_now(),
    )
    validate_task(task)
    return task


# ---------------------------------------------------------------------------
# Result factory (used by rule-based extractors and future model adapters)
# ---------------------------------------------------------------------------

def make_result(
    task: SemanticTask,
    overall_confidence: int,
    extraction_method: str = EXTRACTION_METHOD_RULE_BASED,
    provenance: Optional[SemanticProvenance] = None,
    **kwargs,
) -> SemanticExtractionResult:
    """
    Construct a SemanticExtractionResult tied to a task.

    Validates the result against the task before returning.
    Raises SemanticValidationError if validation fails.
    """
    if provenance is None:
        provenance = SemanticProvenance(
            extraction_method=extraction_method,
            source_id=task.source_id,
            source_span=task.source_span,
        )

    result = SemanticExtractionResult(
        task_id=task.task_id,
        task_type=task.task_type,
        extraction_method=extraction_method,
        provenance=provenance,
        overall_confidence=overall_confidence,
        extracted_at=_now(),
        **kwargs,
    )
    validate_result(result, task)
    return result


# ---------------------------------------------------------------------------
# Integration bridge: result → candidate (no memory write)
# ---------------------------------------------------------------------------

def result_to_candidate(
    result: SemanticExtractionResult,
    task: SemanticTask,
    event_type: str,
    title: str,
    created_by: str = 'semantic-extractor',
    extra_tags: Optional[List[str]] = None,
) -> 'CandidateMemoryEvent':
    """
    Convert a SemanticExtractionResult into a CandidateMemoryEvent.

    The candidate has status='proposed' and committed_id=None.
    It is NOT written to memory_events — the operator must explicitly
    commit via ingestion.candidates.commit_candidates().

    event_type must be an EXTRACTABLE_EVENT_TYPE (validated by
    CandidateMemoryEvent.__post_init__).

    Raises:
        SemanticValidationError — if result does not validate against task.
        ingestion.models.ValidationError — if event_type is not extractable
            or other CandidateMemoryEvent constraints are violated.
    """
    validate_result(result, task)

    from ingestion.models import CandidateMemoryEvent
    from ingestion.models import SourceSpan as IngestionSourceSpan

    # Build the ingestion SourceSpan from semantic provenance.
    # If provenance carries a span, use it; otherwise span the full input.
    pspan = result.provenance.source_span
    if pspan is not None:
        span_start = pspan.start
        span_end = pspan.end
    else:
        span_start = 0
        span_end = len(task.input_text)

    span_text = task.input_text[span_start:span_end]
    ingestion_span = IngestionSourceSpan(
        start=span_start,
        end=span_end,
        text=span_text,
    )

    # Derive tags from semantic labels (non-empty labels become tags)
    tags = [lb.label for lb in result.labels if lb.label.strip()]
    if extra_tags:
        for t in extra_tags:
            if t not in tags:
                tags.append(t)

    # Build summary from result (prefer explicit summary, else claim text)
    summary = result.summary
    if not summary and result.claims:
        summary = result.claims[0].text
    if not summary:
        summary = f"Semantic extraction via {result.extraction_method}"

    # Build evidence string
    evidence_parts = []
    if result.entities:
        evidence_parts.append(
            "Entities: " + ", ".join(
                f"{e.text} [{e.entity_type}]" for e in result.entities
            )
        )
    if result.relations:
        evidence_parts.append(
            "Relations: " + "; ".join(
                f"{r.subject} {r.predicate} {r.object_}" for r in result.relations
            )
        )
    evidence = " | ".join(evidence_parts) if evidence_parts else None

    return CandidateMemoryEvent(
        event_type=event_type,
        title=title,
        summary=summary,
        evidence=evidence,
        source=task.source_id or task.input_text[:80],
        confidence=result.overall_confidence,
        status='proposed',
        tags=tags,
        created_by=created_by,
        source_span=ingestion_span,
        extraction_method=result.extraction_method,
        committed_id=None,
    )

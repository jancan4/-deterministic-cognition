"""
Semantic extraction validators.

All validation is deterministic and side-effect-free. No model calls, no
network access, no database writes.

Raises SemanticValidationError (a ValueError subclass) on any violation.
All public validate_* functions are callable in any order.
"""
from typing import Optional

from .models import (
    CONFIDENCE_MAX,
    CONFIDENCE_MIN,
    EXTRACTION_POLARITY_VALUES,
    SEMANTIC_TASK_TYPES,
    ExtractedClaim,
    ExtractedEntity,
    ExtractedRelation,
    SemanticExtractionResult,
    SemanticLabel,
    SemanticProvenance,
    SemanticSpan,
    SemanticTask,
)


class SemanticValidationError(ValueError):
    pass


# ---------------------------------------------------------------------------
# Primitive validators
# ---------------------------------------------------------------------------

def validate_confidence(confidence: int, field_name: str = 'confidence') -> None:
    """Confidence must be an integer in [1, 5]."""
    if isinstance(confidence, bool) or not isinstance(confidence, int):
        raise SemanticValidationError(
            f"{field_name} must be an integer, got {type(confidence).__name__}"
        )
    if not CONFIDENCE_MIN <= confidence <= CONFIDENCE_MAX:
        raise SemanticValidationError(
            f"{field_name} must be {CONFIDENCE_MIN}–{CONFIDENCE_MAX}, got {confidence}"
        )


def validate_span(span: SemanticSpan, text_len: int, field_name: str = 'span') -> None:
    """
    Span must satisfy: 0 <= start < end <= text_len.

    A zero-length span (start == end) is not meaningful as an extraction
    result and is rejected.
    """
    if not isinstance(span, SemanticSpan):
        raise SemanticValidationError(
            f"{field_name} must be a SemanticSpan, got {type(span).__name__}"
        )
    if span.start < 0:
        raise SemanticValidationError(
            f"{field_name}.start must be >= 0, got {span.start}"
        )
    if span.end > text_len:
        raise SemanticValidationError(
            f"{field_name}.end={span.end} exceeds text length {text_len}"
        )
    if span.start >= span.end:
        raise SemanticValidationError(
            f"{field_name} must satisfy start < end, got start={span.start} end={span.end}"
        )


def validate_nonempty_string(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise SemanticValidationError(
            f"{field_name} must be a non-empty string, got {value!r}"
        )


# ---------------------------------------------------------------------------
# Provenance validator
# ---------------------------------------------------------------------------

def validate_provenance(
    provenance: SemanticProvenance,
    source_bound: bool = False,
    input_text_len: Optional[int] = None,
) -> None:
    """
    Validate a SemanticProvenance object.

    source_bound=True: source_id must be present (task has a registered source).
    input_text_len: when provided, source_span is range-checked against it.
    """
    if not isinstance(provenance, SemanticProvenance):
        raise SemanticValidationError(
            f"provenance must be a SemanticProvenance, got {type(provenance).__name__}"
        )
    validate_nonempty_string(provenance.extraction_method, 'provenance.extraction_method')

    if source_bound and not provenance.source_id:
        raise SemanticValidationError(
            "provenance.source_id is required when the task is source-bound "
            "(task.source_id is set)"
        )

    if provenance.source_span is not None and input_text_len is not None:
        validate_span(provenance.source_span, input_text_len, 'provenance.source_span')


# ---------------------------------------------------------------------------
# Label / Entity / Claim / Relation validators
# ---------------------------------------------------------------------------

def validate_label(label: SemanticLabel) -> None:
    validate_nonempty_string(label.label, 'label.label')
    validate_confidence(label.confidence, 'label.confidence')


def validate_entity(entity: ExtractedEntity, input_text_len: Optional[int] = None) -> None:
    validate_nonempty_string(entity.text, 'entity.text')
    validate_nonempty_string(entity.entity_type, 'entity.entity_type')
    validate_confidence(entity.confidence, 'entity.confidence')
    if entity.span is not None and input_text_len is not None:
        validate_span(entity.span, input_text_len, 'entity.span')


def validate_claim(claim: ExtractedClaim, input_text_len: Optional[int] = None) -> None:
    validate_nonempty_string(claim.text, 'claim.text')
    if claim.polarity not in EXTRACTION_POLARITY_VALUES:
        raise SemanticValidationError(
            f"claim.polarity must be one of {EXTRACTION_POLARITY_VALUES}, "
            f"got {claim.polarity!r}"
        )
    validate_confidence(claim.confidence, 'claim.confidence')
    if claim.span is not None and input_text_len is not None:
        validate_span(claim.span, input_text_len, 'claim.span')


def validate_relation(relation: ExtractedRelation, input_text_len: Optional[int] = None) -> None:
    validate_nonempty_string(relation.subject, 'relation.subject')
    validate_nonempty_string(relation.predicate, 'relation.predicate')
    validate_nonempty_string(relation.object_, 'relation.object_')
    validate_confidence(relation.confidence, 'relation.confidence')
    if relation.span is not None and input_text_len is not None:
        validate_span(relation.span, input_text_len, 'relation.span')


# ---------------------------------------------------------------------------
# Task validator
# ---------------------------------------------------------------------------

def validate_task(task: SemanticTask) -> None:
    """
    Validate a SemanticTask.

    Raises SemanticValidationError if:
      - task_type is not an approved SEMANTIC_TASK_TYPE
      - input_text is empty
      - source_span is out of range (when present)
    """
    if not isinstance(task, SemanticTask):
        raise SemanticValidationError(
            f"Expected SemanticTask, got {type(task).__name__}"
        )
    if task.task_type not in SEMANTIC_TASK_TYPES:
        raise SemanticValidationError(
            f"Invalid task_type {task.task_type!r}. "
            f"Approved types: {SEMANTIC_TASK_TYPES}"
        )
    if not task.input_text or not task.input_text.strip():
        raise SemanticValidationError("task.input_text must not be empty")
    if task.source_span is not None:
        validate_span(task.source_span, len(task.input_text), 'task.source_span')


# ---------------------------------------------------------------------------
# Result validator
# ---------------------------------------------------------------------------

def validate_result(
    result: SemanticExtractionResult,
    task: Optional[SemanticTask] = None,
) -> None:
    """
    Validate a SemanticExtractionResult.

    When task is provided:
      - result.task_id must match task.task_id
      - result.task_type must match task.task_type
      - provenance source_bound check is applied
      - span ranges are checked against len(task.input_text)

    Raises SemanticValidationError on any violation.
    """
    if not isinstance(result, SemanticExtractionResult):
        raise SemanticValidationError(
            f"Expected SemanticExtractionResult, got {type(result).__name__}"
        )
    validate_nonempty_string(result.extraction_method, 'result.extraction_method')
    validate_confidence(result.overall_confidence, 'result.overall_confidence')

    if result.task_type not in SEMANTIC_TASK_TYPES:
        raise SemanticValidationError(
            f"result.task_type {result.task_type!r} is not an approved task type"
        )

    source_bound = False
    input_text_len = None

    if task is not None:
        if result.task_id != task.task_id:
            raise SemanticValidationError(
                f"result.task_id {result.task_id!r} does not match "
                f"task.task_id {task.task_id!r}"
            )
        if result.task_type != task.task_type:
            raise SemanticValidationError(
                f"result.task_type {result.task_type!r} does not match "
                f"task.task_type {task.task_type!r}"
            )
        source_bound = task.is_source_bound
        input_text_len = len(task.input_text)

    validate_provenance(result.provenance, source_bound=source_bound,
                        input_text_len=input_text_len)

    for lb in result.labels:
        validate_label(lb)
    for entity in result.entities:
        validate_entity(entity, input_text_len)
    for claim in result.claims:
        validate_claim(claim, input_text_len)
    for relation in result.relations:
        validate_relation(relation, input_text_len)

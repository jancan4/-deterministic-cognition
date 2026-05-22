"""
Local model adapter contracts: request/response envelopes, execution policy,
and conversion helpers between the semantic layer and the model layer.

Design principles:
  - Every structure serializes to deterministic JSON.
  - request_id is content-addressed: same (model, version, task, input) → same id.
  - No model weights, inference logic, or network calls here.
  - Conversion helpers are the governed bridge: semantic.SemanticTask →
    LocalModelRequest and LocalModelResponse → semantic.SemanticExtractionResult.

Dependency direction:
  models → semantic   (models imports from semantic; semantic never imports from models)
"""
import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional

from semantic.models import (
    CONFIDENCE_MAX,
    CONFIDENCE_MIN,
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
from semantic.validators import SemanticValidationError, validate_result


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_MAX_RETRIES = 0
MAX_TIMEOUT_SECONDS = 300.0


class ModelContractError(ValueError):
    pass


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def derive_request_id(
    model_name: str,
    model_version: str,
    task_type: str,
    input_text: str,
) -> str:
    """
    Deterministic request_id:
      sha256(model_name + NUL + model_version + NUL + task_type + NUL + input_text)[:16]
    """
    raw = f"{model_name}\x00{model_version}\x00{task_type}\x00{input_text}".encode('utf-8')
    return hashlib.sha256(raw).hexdigest()[:16]


# ---------------------------------------------------------------------------
# LocalModelRequest
# ---------------------------------------------------------------------------

@dataclass
class LocalModelRequest:
    """
    A deterministic, self-describing envelope sent to a model adapter.

    request_id is content-addressed: identical inputs → identical id.
    Callers should use task_to_request() to construct requests from
    SemanticTask instances.
    """
    request_id: str
    model_name: str
    model_version: str
    task_type: str
    input_text: str
    extraction_method: str
    source_id: Optional[str] = None
    source_span: Optional[SemanticSpan] = None
    metadata: dict = field(default_factory=dict)
    requested_at: str = field(default_factory=_now)

    def to_dict(self) -> dict:
        return {
            'request_id': self.request_id,
            'model_name': self.model_name,
            'model_version': self.model_version,
            'task_type': self.task_type,
            'input_text': self.input_text,
            'extraction_method': self.extraction_method,
            'source_id': self.source_id,
            'source_span': self.source_span.to_dict() if self.source_span else None,
            'metadata': self.metadata,
            'requested_at': self.requested_at,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, ensure_ascii=True)


# ---------------------------------------------------------------------------
# LocalModelResponse
# ---------------------------------------------------------------------------

@dataclass
class LocalModelResponse:
    """
    The structured output returned by a model adapter for one request.

    Uses semantic types directly (SemanticLabel, ExtractedEntity, etc.) so
    the response is immediately usable without further parsing.

    responded_at is set by the adapter at response time.
    """
    request_id: str
    model_name: str
    model_version: str
    task_type: str
    extraction_method: str
    overall_confidence: int
    responded_at: str
    labels: List[SemanticLabel] = field(default_factory=list)
    entities: List[ExtractedEntity] = field(default_factory=list)
    claims: List[ExtractedClaim] = field(default_factory=list)
    relations: List[ExtractedRelation] = field(default_factory=list)
    summary: Optional[str] = None
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            'request_id': self.request_id,
            'model_name': self.model_name,
            'model_version': self.model_version,
            'task_type': self.task_type,
            'extraction_method': self.extraction_method,
            'overall_confidence': self.overall_confidence,
            'responded_at': self.responded_at,
            'labels': [lb.to_dict() for lb in self.labels],
            'entities': [e.to_dict() for e in self.entities],
            'claims': [c.to_dict() for c in self.claims],
            'relations': [r.to_dict() for r in self.relations],
            'summary': self.summary,
            'metadata': self.metadata,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, ensure_ascii=True)


# ---------------------------------------------------------------------------
# ModelExecutionPolicy
# ---------------------------------------------------------------------------

@dataclass
class ModelExecutionPolicy:
    """
    Governs how an adapter execution is run.

    timeout_seconds: maximum wall-clock seconds allowed for adapter.execute().
    max_retries: how many times to retry on transient failure (0 = no retry).
    retry_delay_seconds: wait between retries (metadata; enforcement is adapter-side).
    deterministic_mode: request that the adapter produce identical output for
        identical input (e.g. temperature=0 for neural models).
    """
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    max_retries: int = DEFAULT_MAX_RETRIES
    retry_delay_seconds: float = 1.0
    deterministic_mode: bool = True

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0 or self.timeout_seconds > MAX_TIMEOUT_SECONDS:
            raise ModelContractError(
                f"timeout_seconds must be in (0, {MAX_TIMEOUT_SECONDS}], "
                f"got {self.timeout_seconds}"
            )
        if not isinstance(self.max_retries, int) or isinstance(self.max_retries, bool):
            raise ModelContractError("max_retries must be a non-negative integer")
        if self.max_retries < 0:
            raise ModelContractError(
                f"max_retries must be >= 0, got {self.max_retries}"
            )
        if self.retry_delay_seconds < 0:
            raise ModelContractError(
                f"retry_delay_seconds must be >= 0, got {self.retry_delay_seconds}"
            )

    def to_dict(self) -> dict:
        return {
            'timeout_seconds': self.timeout_seconds,
            'max_retries': self.max_retries,
            'retry_delay_seconds': self.retry_delay_seconds,
            'deterministic_mode': self.deterministic_mode,
        }


# ---------------------------------------------------------------------------
# ModelExecutionResult
# ---------------------------------------------------------------------------

@dataclass
class ModelExecutionResult:
    """
    The complete output of one adapter execution, including timing metadata
    and the converted SemanticExtractionResult.

    Fields:
      response          — raw adapter output (None on failure)
      semantic_result   — validated SemanticExtractionResult (None on failure)
      duration_ms       — wall-clock execution time
      timeout_applied   — True if execution was interrupted by timeout
      retry_count       — how many retries were consumed
      success           — True if no error occurred
      error             — error message string (None on success)
    """
    request_id: str
    adapter_name: str
    adapter_version: str
    started_at: str
    completed_at: str
    duration_ms: float
    timeout_applied: bool
    retry_count: int
    success: bool
    response: Optional[LocalModelResponse] = None
    semantic_result: Optional[SemanticExtractionResult] = None
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            'request_id': self.request_id,
            'adapter_name': self.adapter_name,
            'adapter_version': self.adapter_version,
            'started_at': self.started_at,
            'completed_at': self.completed_at,
            'duration_ms': self.duration_ms,
            'timeout_applied': self.timeout_applied,
            'retry_count': self.retry_count,
            'success': self.success,
            'response': self.response.to_dict() if self.response else None,
            'semantic_result': self.semantic_result.to_dict() if self.semantic_result else None,
            'error': self.error,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, ensure_ascii=True)


# ---------------------------------------------------------------------------
# Request validation
# ---------------------------------------------------------------------------

def validate_request(request: LocalModelRequest) -> None:
    """
    Validate a LocalModelRequest.

    Raises ModelContractError on any violation.
    """
    if not isinstance(request, LocalModelRequest):
        raise ModelContractError(
            f"Expected LocalModelRequest, got {type(request).__name__}"
        )
    if not request.model_name or not request.model_name.strip():
        raise ModelContractError("request.model_name must not be empty")
    if not request.model_version or not request.model_version.strip():
        raise ModelContractError("request.model_version must not be empty")
    if request.task_type not in SEMANTIC_TASK_TYPES:
        raise ModelContractError(
            f"request.task_type {request.task_type!r} is not an approved task type. "
            f"Approved: {SEMANTIC_TASK_TYPES}"
        )
    if not request.input_text or not request.input_text.strip():
        raise ModelContractError("request.input_text must not be empty")
    if not request.extraction_method or not request.extraction_method.strip():
        raise ModelContractError("request.extraction_method must not be empty")
    if not request.request_id or not request.request_id.strip():
        raise ModelContractError("request.request_id must not be empty")


def validate_response(
    response: LocalModelResponse,
    request: Optional[LocalModelRequest] = None,
) -> None:
    """
    Validate a LocalModelResponse.

    When request is provided, checks that request_id, task_type, and
    model_name/version are consistent.
    Raises ModelContractError on any violation.
    """
    if not isinstance(response, LocalModelResponse):
        raise ModelContractError(
            f"Expected LocalModelResponse, got {type(response).__name__}"
        )
    if response.task_type not in SEMANTIC_TASK_TYPES:
        raise ModelContractError(
            f"response.task_type {response.task_type!r} is not approved"
        )
    if not response.extraction_method or not response.extraction_method.strip():
        raise ModelContractError("response.extraction_method must not be empty")
    if (
        isinstance(response.overall_confidence, bool)
        or not isinstance(response.overall_confidence, int)
        or not CONFIDENCE_MIN <= response.overall_confidence <= CONFIDENCE_MAX
    ):
        raise ModelContractError(
            f"response.overall_confidence must be integer {CONFIDENCE_MIN}–{CONFIDENCE_MAX}, "
            f"got {response.overall_confidence!r}"
        )

    if request is not None:
        if response.request_id != request.request_id:
            raise ModelContractError(
                f"response.request_id {response.request_id!r} does not match "
                f"request.request_id {request.request_id!r}"
            )
        if response.task_type != request.task_type:
            raise ModelContractError(
                f"response.task_type {response.task_type!r} does not match "
                f"request.task_type {request.task_type!r}"
            )
        if response.model_name != request.model_name:
            raise ModelContractError(
                f"response.model_name {response.model_name!r} does not match "
                f"request.model_name {request.model_name!r}"
            )
        if response.model_version != request.model_version:
            raise ModelContractError(
                f"response.model_version {response.model_version!r} does not match "
                f"request.model_version {request.model_version!r}"
            )


# ---------------------------------------------------------------------------
# Conversion helpers
# ---------------------------------------------------------------------------

def task_to_request(
    task: SemanticTask,
    model_name: str,
    model_version: str,
    extraction_method: Optional[str] = None,
) -> LocalModelRequest:
    """
    Convert a SemanticTask into a LocalModelRequest for a specific model.

    extraction_method defaults to 'local_model:<model_name>:<model_version>'.
    request_id is derived deterministically from (model_name, model_version,
    task_type, input_text).
    """
    if not model_name or not model_name.strip():
        raise ModelContractError("model_name must not be empty")
    if not model_version or not model_version.strip():
        raise ModelContractError("model_version must not be empty")

    method = extraction_method or f"local_model:{model_name}:{model_version}"
    request = LocalModelRequest(
        request_id=derive_request_id(model_name, model_version, task.task_type, task.input_text),
        model_name=model_name,
        model_version=model_version,
        task_type=task.task_type,
        input_text=task.input_text,
        extraction_method=method,
        source_id=task.source_id,
        source_span=task.source_span,
        metadata=dict(task.metadata),
        requested_at=_now(),
    )
    validate_request(request)
    return request


def response_to_semantic_result(
    response: LocalModelResponse,
    task: SemanticTask,
) -> SemanticExtractionResult:
    """
    Convert a LocalModelResponse into a SemanticExtractionResult.

    Builds provenance from the response's extraction_method and the task's
    source_id/source_span. Validates the result before returning.

    Raises:
        ModelContractError  — response is invalid.
        SemanticValidationError — result does not validate against task.
    """
    provenance = SemanticProvenance(
        extraction_method=response.extraction_method,
        source_id=task.source_id,
        source_span=task.source_span,
        model_id=f"{response.model_name}:{response.model_version}",
    )

    result = SemanticExtractionResult(
        task_id=task.task_id,
        task_type=task.task_type,
        extraction_method=response.extraction_method,
        provenance=provenance,
        overall_confidence=response.overall_confidence,
        extracted_at=response.responded_at,
        labels=list(response.labels),
        entities=list(response.entities),
        claims=list(response.claims),
        relations=list(response.relations),
        summary=response.summary,
        metadata=dict(response.metadata),
    )
    validate_result(result, task)
    return result

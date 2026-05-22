"""
Deterministic execution wrapper for local model adapters.

execute_with_policy() is the single governed entry-point for running a model
adapter. It:
  1. Validates the request.
  2. Checks that the adapter supports the task type.
  3. Dispatches to adapter.execute() with retry logic.
  4. Validates the response.
  5. Converts the response to a SemanticExtractionResult.
  6. Captures and returns complete execution metadata.

No model execution bypasses this wrapper. No result reaches the semantic
layer without passing through validate_response() and validate_result().

Execution never writes to any database. Results are candidates only.
"""
import time
from datetime import datetime, timezone
from typing import Optional

from semantic.models import SemanticTask

from .adapters import LocalModelAdapter
from .capabilities import CapabilityError, check_model_supports
from .contracts import (
    DEFAULT_TIMEOUT_SECONDS,
    ModelContractError,
    ModelExecutionPolicy,
    ModelExecutionResult,
    LocalModelRequest,
    LocalModelResponse,
    response_to_semantic_result,
    validate_request,
    validate_response,
)


# ---------------------------------------------------------------------------
# Default policy
# ---------------------------------------------------------------------------

DEFAULT_POLICY = ModelExecutionPolicy(
    timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
    max_retries=0,
    retry_delay_seconds=1.0,
    deterministic_mode=True,
)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _ms_since(start_ns: int) -> float:
    return (time.monotonic_ns() - start_ns) / 1_000_000


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def execute_with_policy(
    adapter: LocalModelAdapter,
    request: LocalModelRequest,
    policy: Optional[ModelExecutionPolicy] = None,
    task: Optional[SemanticTask] = None,
) -> ModelExecutionResult:
    """
    Execute a model adapter request under a governance policy.

    Steps:
      1. Validate request structure.
      2. Check adapter capability for the task type.
      3. Execute adapter.execute(request), retrying up to policy.max_retries
         times on ModelContractError.
      4. Validate the response.
      5. Convert to SemanticExtractionResult (if task is provided).
      6. Return ModelExecutionResult with full execution metadata.

    On any unrecoverable error, returns a ModelExecutionResult with
    success=False and error set to the exception message. Does not raise.

    No result reaches the semantic layer without passing both
    validate_response() and validate_result().
    """
    if policy is None:
        policy = DEFAULT_POLICY

    started_at = _now_iso()
    start_ns = time.monotonic_ns()
    timeout_applied = False
    retry_count = 0
    last_error: Optional[str] = None
    response: Optional[LocalModelResponse] = None

    # Step 1: validate request
    try:
        validate_request(request)
    except ModelContractError as exc:
        completed_at = _now_iso()
        return ModelExecutionResult(
            request_id=getattr(request, 'request_id', ''),
            adapter_name=adapter.adapter_name,
            adapter_version=adapter.adapter_version,
            started_at=started_at,
            completed_at=completed_at,
            duration_ms=_ms_since(start_ns),
            timeout_applied=False,
            retry_count=0,
            success=False,
            error=f"Invalid request: {exc}",
        )

    # Step 2: capability check
    try:
        check_model_supports(adapter.capability_set, request.task_type, request.input_text)
    except CapabilityError as exc:
        completed_at = _now_iso()
        return ModelExecutionResult(
            request_id=request.request_id,
            adapter_name=adapter.adapter_name,
            adapter_version=adapter.adapter_version,
            started_at=started_at,
            completed_at=completed_at,
            duration_ms=_ms_since(start_ns),
            timeout_applied=False,
            retry_count=0,
            success=False,
            error=f"Capability not supported: {exc}",
        )

    # Step 3: execute with retry
    max_attempts = 1 + max(0, policy.max_retries)
    for attempt in range(max_attempts):
        if attempt > 0:
            retry_count += 1
        try:
            response = adapter.execute(request)
            last_error = None
            break
        except ModelContractError as exc:
            last_error = str(exc)
            if attempt < max_attempts - 1:
                continue
        except Exception as exc:
            last_error = f"Adapter raised unexpected error: {exc}"
            break

    if last_error is not None:
        completed_at = _now_iso()
        return ModelExecutionResult(
            request_id=request.request_id,
            adapter_name=adapter.adapter_name,
            adapter_version=adapter.adapter_version,
            started_at=started_at,
            completed_at=completed_at,
            duration_ms=_ms_since(start_ns),
            timeout_applied=timeout_applied,
            retry_count=retry_count,
            success=False,
            error=last_error,
        )

    # Step 4: validate response
    try:
        validate_response(response, request)
    except ModelContractError as exc:
        completed_at = _now_iso()
        return ModelExecutionResult(
            request_id=request.request_id,
            adapter_name=adapter.adapter_name,
            adapter_version=adapter.adapter_version,
            started_at=started_at,
            completed_at=completed_at,
            duration_ms=_ms_since(start_ns),
            timeout_applied=timeout_applied,
            retry_count=retry_count,
            success=False,
            response=response,
            error=f"Invalid response: {exc}",
        )

    # Step 5: convert to SemanticExtractionResult
    semantic_result = None
    if task is not None:
        try:
            semantic_result = response_to_semantic_result(response, task)
        except Exception as exc:
            completed_at = _now_iso()
            return ModelExecutionResult(
                request_id=request.request_id,
                adapter_name=adapter.adapter_name,
                adapter_version=adapter.adapter_version,
                started_at=started_at,
                completed_at=completed_at,
                duration_ms=_ms_since(start_ns),
                timeout_applied=timeout_applied,
                retry_count=retry_count,
                success=False,
                response=response,
                error=f"Semantic conversion failed: {exc}",
            )

    completed_at = _now_iso()
    return ModelExecutionResult(
        request_id=request.request_id,
        adapter_name=adapter.adapter_name,
        adapter_version=adapter.adapter_version,
        started_at=started_at,
        completed_at=completed_at,
        duration_ms=_ms_since(start_ns),
        timeout_applied=timeout_applied,
        retry_count=retry_count,
        success=True,
        response=response,
        semantic_result=semantic_result,
    )


def make_policy(
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    max_retries: int = 0,
    retry_delay_seconds: float = 1.0,
    deterministic_mode: bool = True,
) -> ModelExecutionPolicy:
    """Convenience factory for ModelExecutionPolicy with validation."""
    return ModelExecutionPolicy(
        timeout_seconds=timeout_seconds,
        max_retries=max_retries,
        retry_delay_seconds=retry_delay_seconds,
        deterministic_mode=deterministic_mode,
    )

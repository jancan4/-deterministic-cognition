"""
Deterministic task handler registry.

Separates handler registration from task orchestration. The registry is an
explicit object passed into the runner — no global mutable state, no implicit
side channels. Every dispatch decision is traceable to a named, registered
callable.

Design invariants:
- No random IDs anywhere in the dispatch path.
- No network calls. Handlers must be local, synchronous callables.
- Handler exceptions are captured in HandlerResult, never propagated to the
  runner. The caller decides what transition to make based on success/failure.
- Handlers cannot directly alter task state in the orchestration DB without
  going through transition_task, which enforces the state machine. Any attempt
  by a handler to make an invalid transition raises TransitionError, which is
  captured by execute_handler and results in a failed dispatch.
- result_json and metadata_json use sort_keys=True for deterministic output
  across Python versions.
"""
import json
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from orchestration.models import Task


class HandlerRegistrationError(ValueError):
    pass


class HandlerNotFoundError(KeyError):
    pass


@dataclass
class HandlerResult:
    """
    Structured outcome of a single handler execution.

    result_json and metadata_json are pre-serialized JSON strings.
    error is None on success and a plain-text message on failure.
    """
    task_id: int
    task_type: str
    success: bool
    result_json: str
    error: Optional[str]
    metadata_json: str

    def to_dict(self) -> dict:
        return {
            'task_id': self.task_id,
            'task_type': self.task_type,
            'success': self.success,
            'result_json': self.result_json,
            'error': self.error,
            'metadata_json': self.metadata_json,
        }


class TaskHandlerRegistry:
    """
    Registry mapping task_type strings to handler callables.

    Handlers are stored in insertion order (Python 3.7+ dict guarantee).
    list_handlers() returns sorted task_type names for deterministic audit output
    regardless of registration order.

    The registry is an explicit argument to run_iterations and execute_task —
    there is no module-level global registry. Callers that pass registry=None
    receive deterministic missing_handler failures for every task, not silent
    noop execution.
    """

    def __init__(self) -> None:
        self._handlers: Dict[str, Callable] = {}

    def register(
        self,
        task_type: str,
        handler: Callable,
        replace: bool = False,
    ) -> None:
        """
        Register handler for task_type.

        Raises HandlerRegistrationError if task_type is empty, handler is not
        callable, or task_type is already registered and replace=False.
        """
        if not task_type or not task_type.strip():
            raise HandlerRegistrationError("task_type must not be empty")
        if not callable(handler):
            raise HandlerRegistrationError(
                f"handler for '{task_type}' must be callable, "
                f"got {type(handler).__name__}"
            )
        if task_type in self._handlers and not replace:
            raise HandlerRegistrationError(
                f"Handler for '{task_type}' is already registered. "
                f"Use replace=True to override."
            )
        self._handlers[task_type] = handler

    def get(self, task_type: str) -> Callable:
        """Return the handler for task_type. Raises HandlerNotFoundError if absent."""
        if task_type not in self._handlers:
            raise HandlerNotFoundError(
                f"No handler registered for task_type '{task_type}'"
            )
        return self._handlers[task_type]

    def has(self, task_type: str) -> bool:
        """Return True if a handler is registered for task_type."""
        return task_type in self._handlers

    def list_handlers(self) -> List[str]:
        """Return sorted list of registered task_type names."""
        return sorted(self._handlers)

    def unregister(self, task_type: str) -> None:
        """
        Remove handler for task_type.

        Raises HandlerNotFoundError if task_type has no registered handler.
        """
        if task_type not in self._handlers:
            raise HandlerNotFoundError(
                f"Cannot unregister '{task_type}': no handler registered"
            )
        del self._handlers[task_type]


def execute_handler(
    registry: TaskHandlerRegistry,
    task: Task,
    metadata: Optional[Dict[str, Any]] = None,
) -> HandlerResult:
    """
    Execute the registered handler for task.task_type.

    Never raises. All outcomes — missing handler, successful return,
    any exception — are captured and returned as a HandlerResult. The
    caller is responsible for interpreting success/failure and making the
    appropriate orchestration transition.

    result_json is the JSON-serialized return value of the handler (or '{}'
    on failure). metadata_json is the JSON-serialized metadata dict.
    Both use sort_keys=True for deterministic output.
    """
    meta_json = json.dumps(metadata or {}, sort_keys=True)

    if not registry.has(task.task_type):
        return HandlerResult(
            task_id=task.id,
            task_type=task.task_type,
            success=False,
            result_json='{}',
            error=f'missing_handler:{task.task_type}',
            metadata_json=meta_json,
        )

    handler = registry.get(task.task_type)
    try:
        raw = handler(task)
        result_json = json.dumps({} if raw is None else raw, sort_keys=True)
        return HandlerResult(
            task_id=task.id,
            task_type=task.task_type,
            success=True,
            result_json=result_json,
            error=None,
            metadata_json=meta_json,
        )
    except Exception as exc:
        error_msg = str(exc) or repr(exc)
        return HandlerResult(
            task_id=task.id,
            task_type=task.task_type,
            success=False,
            result_json='{}',
            error=error_msg,
            metadata_json=meta_json,
        )

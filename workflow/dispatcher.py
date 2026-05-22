"""
Semantic workflow task dispatcher.

Connects the runtime execution layer to ``semantic_extraction`` workflow nodes
by registering a handler with the ``TaskHandlerRegistry``.

Handler lifecycle
-----------------
1. Read ``task_payload_json`` from ``task.metadata`` (embedded at submission
   time by ``submit_ready_nodes()``).
2. Call ``execute_semantic_node()`` — validates payload, resolves adapter,
   executes the semantic pipeline, persists to the semantic ledger, and
   optionally promotes candidates to memory.
3. On success: optionally append a ``node_completed`` lineage event to the
   workflow execution log (requires ``workflow_db_path`` and execution context
   in task metadata).
4. On failure: raise ``SemanticHandlerError``. ``execute_handler()`` in the
   runtime layer catches this and produces ``HandlerResult(success=False)``,
   causing ``execute_task()`` to transition the task ``running → failed``.

Governance contract (inherited from ``execute_semantic_node``)
--------------------------------------------------------------
- Promotion creates ``memory_events`` with ``status='unresolved'`` only.
- ``update_status('active')`` is never called here.
- ``commit=False`` (the default in any payload) produces ledger rows only;
  no ``memory_events`` row is created.
- No automatic approval, no hidden mutation.

Replay contract
---------------
This module is **live-execution only**. Replay reconstructs state from
``WorkflowExecutionLineageEvent`` rows (``event_type='node_completed'`` with
``semantic_run_id`` in ``metadata``) and the semantic ledger. Replay must
never call adapters or invoke the dispatcher handler.

Idempotency
-----------
``execute_semantic_node()`` is idempotent on ``run_id`` (``INSERT OR IGNORE``).
A crashed or retried dispatch re-uses the existing ledger row. Already-promoted
candidates are skipped. The orchestration state machine prevents re-dispatch of
tasks in terminal or non-ready states — calling ``execute_task()`` on a
completed task raises ``ValidationError`` before the handler is reached.
"""
from datetime import datetime, timezone
from typing import Optional

from orchestration.models import Task
from runtime.handlers import TaskHandlerRegistry
from workflow.semantic_handler import SemanticHandlerError, execute_semantic_node
from workflow.state import EVENT_NODE_COMPLETED, WorkflowExecutionLineageEvent
from workflow.storage import append_execution_events, init_db as _wf_init_db


_DEFAULT_ACTOR = 'semantic-dispatcher'


def _now() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def make_semantic_handler(
    db_path: str,
    workflow_db_path: Optional[str] = None,
    actor: str = _DEFAULT_ACTOR,
):
    """
    Build a ``semantic_extraction`` handler callable bound to ``db_path`` and ``actor``.

    The returned callable has signature ``handler(task: Task) -> dict`` and is
    suitable for registration with ``TaskHandlerRegistry``.

    ``db_path``
        Path to the memory/semantic ledger database. Passed to
        ``execute_semantic_node()`` for all ledger and memory writes.
    ``workflow_db_path``
        Optional path to the workflow lineage database. When set and the task
        metadata carries ``workflow_execution_id`` + ``workflow_node_id``, a
        ``node_completed`` event is appended after successful execution.
        When absent, semantic ledger writes still occur; lineage is skipped.
    ``actor``
        Actor identifier recorded in ledger and lineage rows.

    On failure the handler raises ``SemanticHandlerError``. ``execute_handler()``
    catches this and returns ``HandlerResult(success=False)``, which causes
    ``execute_task()`` to transition the orchestration task to ``failed``.
    """
    def handler(task: Task) -> dict:
        task_payload_json = task.metadata.get('task_payload_json', '{}')

        sem_result = execute_semantic_node(
            task_payload_json=task_payload_json,
            db_path=db_path,
            actor=actor,
        )

        if not sem_result.success:
            raise SemanticHandlerError(
                sem_result.error or 'semantic execution failed'
            )

        # Optionally append workflow lineage. Non-fatal: the canonical semantic
        # artifact is in the ledger; lineage is supplemental provenance.
        execution_id = task.metadata.get('workflow_execution_id')
        node_id = task.metadata.get('workflow_node_id')

        if workflow_db_path and execution_id and node_id:
            try:
                _wf_init_db(workflow_db_path)
                evt = WorkflowExecutionLineageEvent(
                    execution_id=execution_id,
                    event_type=EVENT_NODE_COMPLETED,
                    old_state=None,
                    new_state=None,
                    node_id=node_id,
                    stage_index=0,
                    reason=f'Dispatcher: semantic run {sem_result.run_id}',
                    created_at=_now(),
                    metadata=dict(sem_result.lineage_metadata),
                )
                append_execution_events(workflow_db_path, [evt])
            except Exception:
                pass

        return {
            'semantic_run_id': sem_result.run_id,
            'candidate_ids': sem_result.candidate_ids,
            'promoted_memory_ids': sem_result.promoted_memory_ids,
            'lineage_metadata': sem_result.lineage_metadata,
        }

    return handler


def build_semantic_registry(
    db_path: str,
    workflow_db_path: Optional[str] = None,
    actor: str = _DEFAULT_ACTOR,
) -> TaskHandlerRegistry:
    """
    Build and return a ``TaskHandlerRegistry`` with ``semantic_extraction`` registered.

    Suitable for passing directly to ``run_iterations()`` or ``execute_task()``.
    """
    registry = TaskHandlerRegistry()
    registry.register(
        'semantic_extraction',
        make_semantic_handler(db_path, workflow_db_path=workflow_db_path, actor=actor),
    )
    return registry

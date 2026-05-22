"""Tests for the semantic workflow task dispatcher."""
import json

import pytest

from memory import service as mem_service
from orchestration.service import create_task, init_db as orch_init_db, transition_task
from runtime.handlers import execute_handler
from runtime.service import execute_task
from semantic.ledger import init_ledger, list_candidates
from workflow.coordination import submit_ready_nodes
from workflow.dispatcher import build_semantic_registry, make_semantic_handler
from workflow.executor import initialize_execution, start_execution
from workflow.models import RetryPolicy, WorkflowNode
from workflow.service import define_workflow, plan_workflow
from workflow.state import EVENT_NODE_COMPLETED
from workflow.storage import init_db as wf_init_db, load_execution_events

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MINIMAL_PAYLOAD = json.dumps({
    'task_type': 'tagging',
    'adapter': 'stub',
    'input_text': 'Central banks raised interest rates.',
})

_COMMIT_PAYLOAD = json.dumps({
    'task_type': 'tagging',
    'adapter': 'stub',
    'input_text': 'Rate hikes continue across G7.',
    'commit': True,
})

_BAD_PAYLOAD = json.dumps({'adapter': 'stub'})   # missing required task_type


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _orch_db(tmp_path):
    db = str(tmp_path / 'orch.db')
    orch_init_db(db)
    return db


def _mem_db(tmp_path):
    db = str(tmp_path / 'memory.db')
    init_ledger(db)
    mem_service.init_db(db)
    return db


def _wf_db(tmp_path):
    db = str(tmp_path / 'workflow.db')
    wf_init_db(db)
    return db


def _semantic_node(node_id, payload=None):
    return WorkflowNode(
        node_id=node_id,
        task_type='semantic_extraction',
        task_payload_json=payload or _MINIMAL_PAYLOAD,
        dependency_ids=[],
        retry_policy=RetryPolicy(max_attempts=1),
    )


def _running_execution(plan):
    execution, _ = initialize_execution(plan)
    execution, _ = start_execution(execution)
    return execution


def _make_wf_and_plan(*nodes, wf_id='wf'):
    wf = define_workflow(wf_id, 'Test', list(nodes))
    vr, plan, _ = plan_workflow(wf)
    assert vr.valid, vr.errors
    return wf, plan


def _make_ready_task(tmp_path, payload=None, orch_db=None, metadata_extra=None):
    """
    Create an orchestration task in state='ready' with the semantic payload
    embedded in its metadata (as submit_ready_nodes does after the fix).
    """
    db = orch_db or _orch_db(tmp_path)
    meta = {
        'workflow_execution_id': 'test-exec-1',
        'workflow_node_id': 'test-node-1',
        'task_payload_json': payload or _MINIMAL_PAYLOAD,
    }
    if metadata_extra:
        meta.update(metadata_extra)
    task = create_task(
        db,
        title='test-semantic-node',
        task_type='semantic_extraction',
        actor='test',
        priority=3,
        metadata=meta,
    )
    task = transition_task(db, task.id, 'ready', reason='ready', actor='test')
    return db, task


# ---------------------------------------------------------------------------
# T1: submit_ready_nodes embeds task_payload_json for semantic_extraction nodes
# ---------------------------------------------------------------------------

def test_submit_ready_nodes_embeds_payload_for_semantic_node(tmp_path):
    orch_db = _orch_db(tmp_path)
    n = _semantic_node('extract', payload=_MINIMAL_PAYLOAD)
    wf, plan = _make_wf_and_plan(n)
    execution = _running_execution(plan)

    tasks, _ = submit_ready_nodes(orch_db, execution, plan, wf, 'coordinator')

    assert len(tasks) == 1
    assert 'task_payload_json' in tasks[0].metadata
    stored = json.loads(tasks[0].metadata['task_payload_json'])
    assert stored['task_type'] == 'tagging'
    assert stored['adapter'] == 'stub'


def test_submit_ready_nodes_no_payload_embedded_when_default_empty_object(tmp_path):
    orch_db = _orch_db(tmp_path)
    # Default task_payload_json is '{}'
    n = WorkflowNode(
        node_id='non-semantic',
        task_type='research',
        task_payload_json='{}',
        dependency_ids=[],
        retry_policy=RetryPolicy(max_attempts=1),
    )
    wf, plan = _make_wf_and_plan(n)
    execution = _running_execution(plan)

    tasks, _ = submit_ready_nodes(orch_db, execution, plan, wf, 'coordinator')

    assert len(tasks) == 1
    assert 'task_payload_json' not in tasks[0].metadata


def test_submit_ready_nodes_payload_roundtrips_correctly(tmp_path):
    """The payload embedded at submission time is the exact value from the WorkflowNode."""
    orch_db = _orch_db(tmp_path)
    payload = json.dumps({'task_type': 'entity_extraction', 'adapter': 'echo',
                          'input_text': 'test'})
    n = _semantic_node('sem', payload=payload)
    wf, plan = _make_wf_and_plan(n)
    execution = _running_execution(plan)

    tasks, _ = submit_ready_nodes(orch_db, execution, plan, wf, 'coordinator')

    assert tasks[0].metadata['task_payload_json'] == payload


# ---------------------------------------------------------------------------
# T2: handler happy path returns success HandlerResult without exception
# ---------------------------------------------------------------------------

def test_handler_happy_path_returns_success(tmp_path):
    mem_db = _mem_db(tmp_path)
    registry = build_semantic_registry(mem_db)
    orch_db, task = _make_ready_task(tmp_path)
    running_task = transition_task(orch_db, task.id, 'running', reason='test', actor='test')

    result = execute_handler(registry, running_task)

    assert result.success is True
    assert result.error is None
    data = json.loads(result.result_json)
    assert 'semantic_run_id' in data
    assert data['semantic_run_id'] != ''


def test_handler_result_carries_lineage_metadata(tmp_path):
    mem_db = _mem_db(tmp_path)
    registry = build_semantic_registry(mem_db)
    orch_db, task = _make_ready_task(tmp_path)
    running_task = transition_task(orch_db, task.id, 'running', reason='test', actor='test')

    result = execute_handler(registry, running_task)

    data = json.loads(result.result_json)
    meta = data['lineage_metadata']
    assert meta['task_type'] == 'tagging'
    assert meta['adapter_name'] is not None
    assert 'candidate_ids' in meta
    assert meta['committed'] is False


# ---------------------------------------------------------------------------
# T3: bad payload returns failed HandlerResult without exception escape
# ---------------------------------------------------------------------------

def test_handler_bad_payload_no_exception_escape(tmp_path):
    mem_db = _mem_db(tmp_path)
    registry = build_semantic_registry(mem_db)
    orch_db, task = _make_ready_task(tmp_path, payload=_BAD_PAYLOAD)
    running_task = transition_task(orch_db, task.id, 'running', reason='test', actor='test')

    result = execute_handler(registry, running_task)

    assert result.success is False
    assert result.error is not None
    # Error references the missing required field
    assert 'task_type' in result.error or 'missing' in result.error.lower()


def test_handler_empty_payload_fails_gracefully(tmp_path):
    mem_db = _mem_db(tmp_path)
    registry = build_semantic_registry(mem_db)
    orch_db, task = _make_ready_task(tmp_path, payload='{}')
    running_task = transition_task(orch_db, task.id, 'running', reason='test', actor='test')

    result = execute_handler(registry, running_task)

    assert result.success is False
    assert result.error is not None


# ---------------------------------------------------------------------------
# T4: execute_task transitions ready → running → completed
# ---------------------------------------------------------------------------

def test_execute_task_transitions_to_completed(tmp_path):
    mem_db = _mem_db(tmp_path)
    registry = build_semantic_registry(mem_db)
    orch_db, task = _make_ready_task(tmp_path)

    final_task = execute_task(orch_db, task.id, 'test-actor', registry=registry)

    assert final_task.state == 'completed'


# ---------------------------------------------------------------------------
# T5: execute_task bad payload transitions ready → running → failed
# ---------------------------------------------------------------------------

def test_execute_task_bad_payload_transitions_to_failed(tmp_path):
    mem_db = _mem_db(tmp_path)
    registry = build_semantic_registry(mem_db)
    orch_db, task = _make_ready_task(tmp_path, payload=_BAD_PAYLOAD)

    final_task = execute_task(orch_db, task.id, 'test-actor', registry=registry)

    assert final_task.state == 'failed'


# ---------------------------------------------------------------------------
# T6: duplicate execute_task after completion does not re-run handler
# ---------------------------------------------------------------------------

def test_duplicate_execute_task_after_completion_raises(tmp_path):
    mem_db = _mem_db(tmp_path)
    registry = build_semantic_registry(mem_db)
    orch_db, task = _make_ready_task(tmp_path)

    final_task = execute_task(orch_db, task.id, 'test-actor', registry=registry)
    assert final_task.state == 'completed'

    # State machine rejects ready → running from completed state.
    # This is the idempotency guard — the handler is never reached a second time.
    with pytest.raises(Exception):
        execute_task(orch_db, task.id, 'test-actor', registry=registry)


# ---------------------------------------------------------------------------
# T7: commit=True promotes unresolved memory only
# ---------------------------------------------------------------------------

def test_commit_true_promotes_unresolved_memory(tmp_path):
    mem_db = _mem_db(tmp_path)
    registry = build_semantic_registry(mem_db)
    orch_db, task = _make_ready_task(tmp_path, payload=_COMMIT_PAYLOAD)

    final_task = execute_task(orch_db, task.id, 'test-actor', registry=registry)
    assert final_task.state == 'completed'

    events = mem_service.list_memory_events(mem_db, status='unresolved', limit=200)
    semantic_events = [e for e in events if e.evidence and e.evidence.startswith('semantic:')]
    assert len(semantic_events) > 0
    for evt in semantic_events:
        assert evt.status == 'unresolved'


def test_commit_true_never_activates_memory(tmp_path):
    """Promoted memory must not reach status='active' via the dispatcher."""
    mem_db = _mem_db(tmp_path)
    registry = build_semantic_registry(mem_db)
    orch_db, task = _make_ready_task(tmp_path, payload=_COMMIT_PAYLOAD)

    execute_task(orch_db, task.id, 'test-actor', registry=registry)

    active_events = mem_service.list_memory_events(mem_db, status='active', limit=200)
    assert len(active_events) == 0


# ---------------------------------------------------------------------------
# T8: commit=False records candidates but no memory rows
# ---------------------------------------------------------------------------

def test_commit_false_no_memory_rows(tmp_path):
    mem_db = _mem_db(tmp_path)
    no_commit_payload = json.dumps({
        'task_type': 'tagging',
        'adapter': 'stub',
        'input_text': 'EUR/USD fell sharply.',
        'commit': False,
    })
    registry = build_semantic_registry(mem_db)
    orch_db, task = _make_ready_task(tmp_path, payload=no_commit_payload)

    final_task = execute_task(orch_db, task.id, 'test-actor', registry=registry)
    assert final_task.state == 'completed'

    candidates = list_candidates(mem_db)
    assert len(candidates) > 0

    events = mem_service.list_memory_events(mem_db, limit=200)
    assert len(events) == 0


def test_default_payload_has_no_commit(tmp_path):
    """_MINIMAL_PAYLOAD has no commit flag so defaults to False — no memory rows."""
    mem_db = _mem_db(tmp_path)
    registry = build_semantic_registry(mem_db)
    orch_db, task = _make_ready_task(tmp_path, payload=_MINIMAL_PAYLOAD)

    execute_task(orch_db, task.id, 'test-actor', registry=registry)

    events = mem_service.list_memory_events(mem_db, limit=200)
    assert len(events) == 0


# ---------------------------------------------------------------------------
# T9: successful handler appends node_completed with semantic_run_id
# ---------------------------------------------------------------------------

def _setup_wf_execution(wf_db, node_id='sem-node', payload=None):
    """
    Create and persist a real workflow execution so that the workflow_executions
    FK constraint is satisfied. Returns (execution, plan).
    """
    from workflow.executor import initialize_execution, start_execution
    from workflow.persistence import persist_execution

    node = WorkflowNode(
        node_id=node_id,
        task_type='semantic_extraction',
        task_payload_json=payload or _MINIMAL_PAYLOAD,
        dependency_ids=[],
        retry_policy=RetryPolicy(max_attempts=1),
    )
    wf, plan = _make_wf_and_plan(node)
    execution, init_evt = initialize_execution(plan)
    execution, start_evts = start_execution(execution)
    persist_execution(wf_db, execution, [init_evt] + start_evts)
    return execution, plan


def test_successful_handler_appends_node_completed_lineage(tmp_path):
    mem_db = _mem_db(tmp_path)
    wf_db = _wf_db(tmp_path)
    orch_db = _orch_db(tmp_path)

    node_id = 'sem-node'
    execution, _ = _setup_wf_execution(wf_db, node_id=node_id)
    execution_id = execution.execution_id

    task = create_task(
        orch_db,
        title=node_id,
        task_type='semantic_extraction',
        actor='test',
        priority=3,
        metadata={
            'workflow_execution_id': execution_id,
            'workflow_node_id': node_id,
            'task_payload_json': _MINIMAL_PAYLOAD,
        },
    )
    task = transition_task(orch_db, task.id, 'ready', reason='ready', actor='test')

    registry = build_semantic_registry(mem_db, workflow_db_path=wf_db)
    execute_task(orch_db, task.id, 'test-actor', registry=registry)

    events = load_execution_events(wf_db, execution_id)
    node_completed = [e for e in events if e.event_type == EVENT_NODE_COMPLETED]
    assert len(node_completed) == 1
    assert node_completed[0].node_id == node_id
    assert 'semantic_run_id' in node_completed[0].metadata
    assert node_completed[0].metadata['semantic_run_id'] != ''


def test_lineage_event_carries_full_semantic_metadata(tmp_path):
    """node_completed metadata must include all required lineage keys."""
    mem_db = _mem_db(tmp_path)
    wf_db = _wf_db(tmp_path)
    orch_db = _orch_db(tmp_path)

    node_id = 'sem-meta-node'
    execution, _ = _setup_wf_execution(wf_db, node_id=node_id)
    execution_id = execution.execution_id

    task = create_task(
        orch_db,
        title=node_id,
        task_type='semantic_extraction',
        actor='test',
        priority=3,
        metadata={
            'workflow_execution_id': execution_id,
            'workflow_node_id': node_id,
            'task_payload_json': _MINIMAL_PAYLOAD,
        },
    )
    task = transition_task(orch_db, task.id, 'ready', reason='ready', actor='test')

    registry = build_semantic_registry(mem_db, workflow_db_path=wf_db)
    execute_task(orch_db, task.id, 'test-actor', registry=registry)

    events = load_execution_events(wf_db, execution_id)
    completed_evts = [e for e in events if e.event_type == EVENT_NODE_COMPLETED]
    assert len(completed_evts) == 1
    meta = completed_evts[0].metadata
    for key in ('semantic_run_id', 'candidate_ids', 'promoted_memory_ids',
                'adapter_name', 'adapter_version', 'task_type', 'committed'):
        assert key in meta, f"Missing lineage metadata key: {key!r}"


# ---------------------------------------------------------------------------
# Additional: handler without workflow_db still succeeds (ledger-only mode)
# ---------------------------------------------------------------------------

def test_handler_no_workflow_db_still_completes(tmp_path):
    mem_db = _mem_db(tmp_path)
    registry = build_semantic_registry(mem_db, workflow_db_path=None)
    orch_db, task = _make_ready_task(tmp_path)

    final_task = execute_task(orch_db, task.id, 'test-actor', registry=registry)

    assert final_task.state == 'completed'


# ---------------------------------------------------------------------------
# Additional: handler with no workflow execution metadata skips lineage safely
# ---------------------------------------------------------------------------

def test_handler_missing_execution_metadata_skips_lineage_without_error(tmp_path):
    mem_db = _mem_db(tmp_path)
    wf_db = _wf_db(tmp_path)
    orch_db = _orch_db(tmp_path)

    # Task has no workflow_execution_id or workflow_node_id
    task = create_task(
        orch_db,
        title='bare-semantic',
        task_type='semantic_extraction',
        actor='test',
        priority=3,
        metadata={'task_payload_json': _MINIMAL_PAYLOAD},
    )
    task = transition_task(orch_db, task.id, 'ready', reason='ready', actor='test')

    registry = build_semantic_registry(mem_db, workflow_db_path=wf_db)
    final_task = execute_task(orch_db, task.id, 'test-actor', registry=registry)

    assert final_task.state == 'completed'
    # Nothing was written to the workflow lineage DB — no crash

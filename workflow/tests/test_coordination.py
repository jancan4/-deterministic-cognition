"""Tests for workflow-to-orchestration coordination layer."""
import pytest

from orchestration.service import init_db as orch_init_db, list_tasks, transition_task

from workflow.coordination import (
    build_task_metadata,
    extract_execution_id,
    extract_node_id,
    find_submitted_node_ids,
    handle_task_result,
    step_execution,
    submit_ready_nodes,
)
from workflow.executor import initialize_execution, record_node_completed, start_execution
from workflow.models import RetryPolicy, WorkflowNode
from workflow.service import define_workflow, plan_workflow
from workflow.state import EVENT_NODE_SUBMITTED


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _orch_db(tmp_path):
    db = str(tmp_path / 'orch.db')
    orch_init_db(db)
    return db


def _node(node_id, task_type='research', dep_ids=None, max_attempts=1):
    return WorkflowNode(
        node_id=node_id,
        task_type=task_type,
        dependency_ids=dep_ids or [],
        retry_policy=RetryPolicy(max_attempts=max_attempts),
    )


def _make_wf_and_plan(*nodes, wf_id='wf'):
    wf = define_workflow(wf_id, 'Test', list(nodes))
    vr, plan, _ = plan_workflow(wf)
    assert vr.valid, vr.errors
    return wf, plan


def _running_execution(plan):
    execution, _ = initialize_execution(plan)
    execution, _ = start_execution(execution)
    return execution


# ---------------------------------------------------------------------------
# build_task_metadata / extract_node_id / extract_execution_id
# ---------------------------------------------------------------------------

def test_build_task_metadata_keys():
    meta = build_task_metadata('exec-1', 'node-a')
    assert 'workflow_execution_id' in meta
    assert 'workflow_node_id' in meta


def test_extract_node_id_roundtrip():
    meta = build_task_metadata('exec-1', 'fetch')

    class FakeTask:
        metadata = meta

    assert extract_node_id(FakeTask()) == 'fetch'


def test_extract_execution_id_roundtrip():
    meta = build_task_metadata('exec-42', 'node-x')

    class FakeTask:
        metadata = meta

    assert extract_execution_id(FakeTask()) == 'exec-42'


def test_extract_node_id_missing_returns_none():
    class FakeTask:
        metadata = {}

    assert extract_node_id(FakeTask()) is None


def test_build_task_metadata_is_deterministic():
    a = build_task_metadata('e', 'n')
    b = build_task_metadata('e', 'n')
    assert a == b


# ---------------------------------------------------------------------------
# submit_ready_nodes — task creation
# ---------------------------------------------------------------------------

def test_submit_ready_nodes_creates_tasks(tmp_path):
    orch_db = _orch_db(tmp_path)
    wf, plan = _make_wf_and_plan(_node('fetch'), _node('parse', dep_ids=['fetch']))
    execution = _running_execution(plan)

    tasks, events = submit_ready_nodes(orch_db, execution, plan, wf, 'coordinator')
    assert len(tasks) == 1
    assert tasks[0].title == 'wf:wf:fetch'


def test_submit_ready_nodes_transitions_tasks_to_ready(tmp_path):
    orch_db = _orch_db(tmp_path)
    wf, plan = _make_wf_and_plan(_node('fetch'))
    execution = _running_execution(plan)

    tasks, _ = submit_ready_nodes(orch_db, execution, plan, wf, 'coordinator')
    assert tasks[0].state == 'ready'


def test_submit_ready_nodes_task_metadata_contains_execution_and_node_ids(tmp_path):
    orch_db = _orch_db(tmp_path)
    wf, plan = _make_wf_and_plan(_node('fetch'))
    execution = _running_execution(plan)

    tasks, _ = submit_ready_nodes(orch_db, execution, plan, wf, 'coordinator')
    task = tasks[0]
    assert task.metadata.get('workflow_node_id') == 'fetch'
    assert task.metadata.get('workflow_execution_id') == execution.execution_id


def test_submit_ready_nodes_emits_lineage_events(tmp_path):
    orch_db = _orch_db(tmp_path)
    wf, plan = _make_wf_and_plan(_node('fetch'), _node('parse', dep_ids=['fetch']))
    execution = _running_execution(plan)

    _, events = submit_ready_nodes(orch_db, execution, plan, wf, 'coordinator')
    assert len(events) == 1
    assert events[0].event_type == EVENT_NODE_SUBMITTED
    assert events[0].node_id == 'fetch'


def test_submit_ready_nodes_correct_task_type(tmp_path):
    orch_db = _orch_db(tmp_path)
    wf, plan = _make_wf_and_plan(_node('work', task_type='analysis'))
    execution = _running_execution(plan)

    tasks, _ = submit_ready_nodes(orch_db, execution, plan, wf, 'coordinator')
    assert tasks[0].task_type == 'analysis'


def test_submit_ready_nodes_multiple_roots_submitted(tmp_path):
    orch_db = _orch_db(tmp_path)
    wf, plan = _make_wf_and_plan(
        _node('a'),
        _node('b'),
        _node('c'),
        _node('sink', dep_ids=['a', 'b', 'c']),
    )
    execution = _running_execution(plan)

    tasks, events = submit_ready_nodes(orch_db, execution, plan, wf, 'coordinator')
    assert len(tasks) == 3
    submitted_node_ids = sorted(t.metadata['workflow_node_id'] for t in tasks)
    assert submitted_node_ids == ['a', 'b', 'c']


# ---------------------------------------------------------------------------
# find_submitted_node_ids — idempotency guard
# ---------------------------------------------------------------------------

def test_find_submitted_node_ids_empty_initially(tmp_path):
    orch_db = _orch_db(tmp_path)
    wf, plan = _make_wf_and_plan(_node('a'))
    execution = _running_execution(plan)

    submitted = find_submitted_node_ids(orch_db, execution.execution_id)
    assert submitted == set()


def test_find_submitted_node_ids_after_submission(tmp_path):
    orch_db = _orch_db(tmp_path)
    wf, plan = _make_wf_and_plan(_node('fetch'), _node('parse', dep_ids=['fetch']))
    execution = _running_execution(plan)

    submit_ready_nodes(orch_db, execution, plan, wf, 'coordinator')
    submitted = find_submitted_node_ids(orch_db, execution.execution_id)
    assert 'fetch' in submitted


def test_submit_ready_nodes_is_idempotent(tmp_path):
    """Calling submit twice must not double-submit nodes."""
    orch_db = _orch_db(tmp_path)
    wf, plan = _make_wf_and_plan(_node('fetch'))
    execution = _running_execution(plan)

    tasks1, _ = submit_ready_nodes(orch_db, execution, plan, wf, 'coordinator')
    tasks2, _ = submit_ready_nodes(orch_db, execution, plan, wf, 'coordinator')
    assert len(tasks1) == 1
    assert len(tasks2) == 0  # already submitted


def test_find_submitted_excludes_completed_tasks(tmp_path):
    """Nodes whose tasks have completed should not appear in submitted set."""
    orch_db = _orch_db(tmp_path)
    wf, plan = _make_wf_and_plan(_node('fetch'))
    execution = _running_execution(plan)

    tasks, _ = submit_ready_nodes(orch_db, execution, plan, wf, 'coordinator')
    # Simulate runtime: running → completed
    transition_task(orch_db, tasks[0].id, 'running', reason='runner picked up', actor='runner')
    transition_task(orch_db, tasks[0].id, 'completed', reason='done', actor='runner')

    submitted = find_submitted_node_ids(orch_db, execution.execution_id)
    assert 'fetch' not in submitted  # completed tasks are not "active"


# ---------------------------------------------------------------------------
# handle_task_result
# ---------------------------------------------------------------------------

def test_handle_task_result_success_completes_node(tmp_path):
    wf, plan = _make_wf_and_plan(_node('a'))
    execution = _running_execution(plan)

    execution, events = handle_task_result(execution, plan, wf, 'a', success=True)
    assert 'a' in execution.completed_node_ids


def test_handle_task_result_failure_records_failure(tmp_path):
    wf, plan = _make_wf_and_plan(_node('a', max_attempts=1))
    execution = _running_execution(plan)

    execution, events = handle_task_result(execution, plan, wf, 'a', success=False, reason='timeout')
    assert 'a' in execution.failed_node_ids


def test_handle_task_result_success_transitions_to_completed(tmp_path):
    wf, plan = _make_wf_and_plan(_node('a'))
    execution = _running_execution(plan)

    execution, _ = handle_task_result(execution, plan, wf, 'a', success=True)
    assert execution.state == 'completed'


def test_handle_task_result_failure_with_retry(tmp_path):
    wf, plan = _make_wf_and_plan(_node('a', max_attempts=3))
    execution = _running_execution(plan)

    execution, events = handle_task_result(execution, plan, wf, 'a', success=False)
    assert 'a' not in execution.failed_node_ids
    assert execution.state == 'executing'


def test_handle_task_result_passes_reason(tmp_path):
    from workflow.state import EVENT_NODE_COMPLETED
    wf, plan = _make_wf_and_plan(_node('a'))
    execution = _running_execution(plan)

    _, events = handle_task_result(execution, plan, wf, 'a', success=True, reason='handler ok')
    node_events = [e for e in events if e.event_type == EVENT_NODE_COMPLETED]
    assert any('handler ok' in e.reason for e in node_events)


# ---------------------------------------------------------------------------
# step_execution
# ---------------------------------------------------------------------------

def test_step_execution_returns_execution_and_tasks(tmp_path):
    orch_db = _orch_db(tmp_path)
    wf, plan = _make_wf_and_plan(_node('fetch'))
    execution = _running_execution(plan)

    execution, tasks, events = step_execution(orch_db, execution, plan, wf, 'coordinator')
    assert len(tasks) == 1
    assert execution.state == 'executing'  # unchanged by step


def test_step_execution_does_not_change_execution_state(tmp_path):
    orch_db = _orch_db(tmp_path)
    wf, plan = _make_wf_and_plan(_node('a'))
    execution = _running_execution(plan)
    state_before = execution.state

    execution, _, _ = step_execution(orch_db, execution, plan, wf, 'coordinator')
    assert execution.state == state_before


# ---------------------------------------------------------------------------
# Full integration: initialize → submit → complete → workflow done
# ---------------------------------------------------------------------------

def test_full_linear_workflow_coordination(tmp_path):
    """Three-node linear chain: fetch → parse → report."""
    orch_db = _orch_db(tmp_path)
    wf, plan = _make_wf_and_plan(
        _node('fetch', task_type='research'),
        _node('parse', task_type='analysis', dep_ids=['fetch']),
        _node('report', task_type='report', dep_ids=['parse']),
    )
    execution = _running_execution(plan)

    # Stage 0: submit 'fetch'
    execution, tasks, _ = step_execution(orch_db, execution, plan, wf, 'coord')
    assert len(tasks) == 1 and tasks[0].metadata['workflow_node_id'] == 'fetch'

    # 'fetch' completes
    execution, _ = handle_task_result(execution, plan, wf, 'fetch', success=True)
    assert execution.active_stage_index == 1

    # Stage 1: submit 'parse'
    execution, tasks, _ = step_execution(orch_db, execution, plan, wf, 'coord')
    assert len(tasks) == 1 and tasks[0].metadata['workflow_node_id'] == 'parse'

    execution, _ = handle_task_result(execution, plan, wf, 'parse', success=True)

    # Stage 2: submit 'report'
    execution, tasks, _ = step_execution(orch_db, execution, plan, wf, 'coord')
    assert tasks[0].metadata['workflow_node_id'] == 'report'

    execution, _ = handle_task_result(execution, plan, wf, 'report', success=True)
    assert execution.state == 'completed'


def test_full_diamond_workflow_coordination(tmp_path):
    orch_db = _orch_db(tmp_path)
    wf, plan = _make_wf_and_plan(
        _node('root', task_type='research'),
        _node('left', task_type='analysis', dep_ids=['root']),
        _node('right', task_type='validation', dep_ids=['root']),
        _node('sink', task_type='report', dep_ids=['left', 'right']),
    )
    execution = _running_execution(plan)

    # Submit root
    execution, tasks, _ = step_execution(orch_db, execution, plan, wf, 'coord')
    execution, _ = handle_task_result(execution, plan, wf, 'root', success=True)

    # Both left and right become ready
    execution, tasks, _ = step_execution(orch_db, execution, plan, wf, 'coord')
    assert len(tasks) == 2

    execution, _ = handle_task_result(execution, plan, wf, 'left', success=True)
    execution, _ = handle_task_result(execution, plan, wf, 'right', success=True)

    # Sink submits and completes
    execution, tasks, _ = step_execution(orch_db, execution, plan, wf, 'coord')
    execution, _ = handle_task_result(execution, plan, wf, 'sink', success=True)
    assert execution.state == 'completed'


def test_node_failure_blocks_downstream(tmp_path):
    orch_db = _orch_db(tmp_path)
    wf, plan = _make_wf_and_plan(
        _node('fetch', task_type='research', max_attempts=1),
        _node('parse', task_type='analysis', dep_ids=['fetch']),
    )
    execution = _running_execution(plan)

    # Submit and fail 'fetch'
    execution, _, _ = step_execution(orch_db, execution, plan, wf, 'coord')
    execution, _ = handle_task_result(execution, plan, wf, 'fetch', success=False, reason='error')

    assert 'fetch' in execution.failed_node_ids
    assert execution.state == 'blocked'

    # 'parse' is not submitted in next step (blocked)
    execution, tasks, _ = step_execution(orch_db, execution, plan, wf, 'coord')
    assert len(tasks) == 0


def test_retry_allows_resubmission(tmp_path):
    orch_db = _orch_db(tmp_path)
    wf, plan = _make_wf_and_plan(_node('fetch', task_type='research', max_attempts=2))
    execution = _running_execution(plan)

    # First attempt — submit
    execution, tasks, _ = step_execution(orch_db, execution, plan, wf, 'coord')
    assert len(tasks) == 1

    # Simulate task completing (so it's no longer "active" in orchestration)
    transition_task(orch_db, tasks[0].id, 'running', reason='picked up', actor='runner')
    transition_task(orch_db, tasks[0].id, 'failed', reason='error', actor='runner')

    # Record failure with retry available
    execution, _ = handle_task_result(execution, plan, wf, 'fetch', success=False, reason='err')
    assert 'fetch' not in execution.failed_node_ids

    # Second attempt — should be re-submitted
    execution, tasks, _ = step_execution(orch_db, execution, plan, wf, 'coord')
    assert len(tasks) == 1  # re-submitted


def test_submission_order_deterministic_across_runs(tmp_path):
    """Repeated step_execution on same state must produce same node ordering."""
    orch_db1 = _orch_db(tmp_path)
    orch_db2 = str(tmp_path / 'orch2.db')
    orch_init_db(orch_db2)
    wf, plan = _make_wf_and_plan(
        _node('root', task_type='research'),
        _node('a', task_type='analysis', dep_ids=['root']),
        _node('b', task_type='validation', dep_ids=['root']),
        _node('c', task_type='governance', dep_ids=['root']),
    )

    exec1 = _running_execution(plan)
    exec1, _ = record_node_completed(exec1, plan, 'root')
    _, tasks1, _ = step_execution(orch_db1, exec1, plan, wf, 'coord')

    exec2 = _running_execution(plan)
    exec2, _ = record_node_completed(exec2, plan, 'root')
    _, tasks2, _ = step_execution(orch_db2, exec2, plan, wf, 'coord')

    node_ids_1 = [t.metadata['workflow_node_id'] for t in tasks1]
    node_ids_2 = [t.metadata['workflow_node_id'] for t in tasks2]
    assert node_ids_1 == node_ids_2

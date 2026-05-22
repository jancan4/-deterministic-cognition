"""Tests for deterministic workflow execution engine."""
import pytest

from workflow.executor import (
    compute_stage_execution,
    detect_outcome,
    get_blocked_node_ids,
    get_ready_node_ids,
    initialize_execution,
    record_node_completed,
    record_node_failed,
    start_execution,
)
from workflow.models import RetryPolicy, WorkflowNode
from workflow.service import define_workflow, plan_workflow
from workflow.state import (
    EVENT_NODE_COMPLETED,
    EVENT_NODE_FAILED,
    EVENT_NODE_RETRY,
    EVENT_STAGE_ADVANCED,
    EVENT_STATE_TRANSITION,
    WorkflowExecutionTransitionError,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _node(node_id, task_type='research', dep_ids=None, max_attempts=1):
    return WorkflowNode(
        node_id=node_id,
        task_type=task_type,
        dependency_ids=dep_ids or [],
        retry_policy=RetryPolicy(max_attempts=max_attempts),
    )


def _make_plan(*nodes, wf_id='wf'):
    wf = define_workflow(wf_id, 'Test', list(nodes))
    vr, plan, _ = plan_workflow(wf)
    assert vr.valid, f"Workflow invalid: {vr.errors}"
    return wf, plan


def _init(plan):
    execution, _ = initialize_execution(plan)
    execution, _ = start_execution(execution)
    return execution


# ---------------------------------------------------------------------------
# initialize_execution
# ---------------------------------------------------------------------------

def test_initialize_execution_state_is_initialized():
    _, plan = _make_plan(_node('a'))
    execution, evt = initialize_execution(plan)
    assert execution.state == 'initialized'


def test_initialize_execution_lineage_event_type():
    _, plan = _make_plan(_node('a'))
    _, evt = initialize_execution(plan)
    assert evt.event_type == EVENT_STATE_TRANSITION
    assert evt.new_state == 'initialized'
    assert evt.old_state is None


def test_initialize_execution_version_is_1():
    _, plan = _make_plan(_node('a'))
    execution, _ = initialize_execution(plan)
    assert execution.version == 1


def test_initialize_execution_sets_workflow_id():
    _, plan = _make_plan(_node('a'), wf_id='my-wf')
    execution, _ = initialize_execution(plan)
    assert execution.workflow_id == 'my-wf'


def test_initialize_execution_empty_node_tracking():
    _, plan = _make_plan(_node('a'))
    execution, _ = initialize_execution(plan)
    assert execution.completed_node_ids == []
    assert execution.failed_node_ids == []
    assert execution.node_attempts == {}


def test_initialize_execution_id_is_64_char_hex():
    _, plan = _make_plan(_node('a'))
    execution, _ = initialize_execution(plan)
    assert len(execution.execution_id) == 64
    int(execution.execution_id, 16)


# ---------------------------------------------------------------------------
# start_execution
# ---------------------------------------------------------------------------

def test_start_execution_state_is_executing():
    _, plan = _make_plan(_node('a'))
    execution, _ = initialize_execution(plan)
    execution, _ = start_execution(execution)
    assert execution.state == 'executing'


def test_start_execution_returns_two_events():
    _, plan = _make_plan(_node('a'))
    execution, _ = initialize_execution(plan)
    _, events = start_execution(execution)
    assert len(events) == 2
    assert events[0].new_state == 'ready'
    assert events[1].new_state == 'executing'


def test_start_execution_increments_version():
    _, plan = _make_plan(_node('a'))
    execution, _ = initialize_execution(plan)
    assert execution.version == 1
    execution, _ = start_execution(execution)
    assert execution.version == 3  # initialized→ready (+1) + ready→executing (+1)


# ---------------------------------------------------------------------------
# get_ready_node_ids
# ---------------------------------------------------------------------------

def test_ready_nodes_at_start_are_roots():
    _, plan = _make_plan(_node('root'), _node('child', dep_ids=['root']))
    execution = _init(plan)
    assert get_ready_node_ids(execution, plan) == ['root']


def test_ready_nodes_after_root_completes():
    _, plan = _make_plan(_node('root'), _node('child', dep_ids=['root']))
    execution = _init(plan)
    execution, _ = record_node_completed(execution, plan, 'root')
    assert get_ready_node_ids(execution, plan) == ['child']


def test_ready_nodes_empty_when_all_complete():
    _, plan = _make_plan(_node('a'), _node('b', dep_ids=['a']))
    execution = _init(plan)
    execution, _ = record_node_completed(execution, plan, 'a')
    execution, _ = record_node_completed(execution, plan, 'b')
    assert get_ready_node_ids(execution, plan) == []


def test_ready_nodes_diamond_both_children_ready_after_root():
    _, plan = _make_plan(
        _node('root'),
        _node('b', dep_ids=['root']),
        _node('c', dep_ids=['root']),
        _node('sink', dep_ids=['b', 'c']),
    )
    execution = _init(plan)
    execution, _ = record_node_completed(execution, plan, 'root')
    ready = get_ready_node_ids(execution, plan)
    assert sorted(ready) == ['b', 'c']


def test_ready_nodes_excludes_failed():
    wf, plan = _make_plan(
        _node('a', max_attempts=1),
        _node('b', dep_ids=['a']),
    )
    execution = _init(plan)
    execution, _ = record_node_failed(execution, plan, wf, 'a')
    ready = get_ready_node_ids(execution, plan)
    assert 'a' not in ready


def test_ready_nodes_deterministic_order_matches_plan():
    _, plan = _make_plan(
        _node('root'),
        _node('a_child', dep_ids=['root']),
        _node('z_child', dep_ids=['root']),
    )
    execution = _init(plan)
    execution, _ = record_node_completed(execution, plan, 'root')
    ready = get_ready_node_ids(execution, plan)
    # Stage 1 is ordered by (priority=0, node_id lex) → a_child before z_child
    assert ready == ['a_child', 'z_child']


# ---------------------------------------------------------------------------
# get_blocked_node_ids
# ---------------------------------------------------------------------------

def test_blocked_nodes_empty_without_failures():
    _, plan = _make_plan(_node('a'), _node('b', dep_ids=['a']))
    execution = _init(plan)
    assert get_blocked_node_ids(execution, plan) == []


def test_blocked_nodes_include_downstream_of_failed():
    wf, plan = _make_plan(
        _node('root', max_attempts=1),
        _node('child', dep_ids=['root']),
        _node('grandchild', dep_ids=['child']),
    )
    execution = _init(plan)
    execution, _ = record_node_failed(execution, plan, wf, 'root')
    blocked = get_blocked_node_ids(execution, plan)
    assert 'child' in blocked
    assert 'grandchild' in blocked


def test_blocked_nodes_exclude_independent_branches():
    wf, plan = _make_plan(
        _node('root', max_attempts=1),
        _node('child', dep_ids=['root']),
        _node('independent'),
        _node('sink', dep_ids=['child', 'independent']),
    )
    execution = _init(plan)
    execution, _ = record_node_failed(execution, plan, wf, 'root')
    blocked = get_blocked_node_ids(execution, plan)
    assert 'independent' not in blocked


# ---------------------------------------------------------------------------
# compute_stage_execution
# ---------------------------------------------------------------------------

def test_compute_stage_execution_initial():
    _, plan = _make_plan(_node('a'), _node('b', dep_ids=['a']))
    execution = _init(plan)
    se = compute_stage_execution(execution, plan, 0)
    assert se.node_ids == ['a']
    assert se.pending_node_ids == ['a']
    assert se.completed_node_ids == []
    assert se.is_complete is False
    assert se.has_failures is False


def test_compute_stage_execution_after_completion():
    _, plan = _make_plan(_node('a'), _node('b', dep_ids=['a']))
    execution = _init(plan)
    execution, _ = record_node_completed(execution, plan, 'a')
    se = compute_stage_execution(execution, plan, 0)
    assert se.is_complete is True
    assert se.completed_node_ids == ['a']
    assert se.pending_node_ids == []


def test_compute_stage_execution_with_failure():
    wf, plan = _make_plan(
        _node('root'),
        _node('a', dep_ids=['root'], max_attempts=1),
        _node('b', dep_ids=['root']),
    )
    execution = _init(plan)
    execution, _ = record_node_completed(execution, plan, 'root')
    execution, _ = record_node_failed(execution, plan, wf, 'a')
    se = compute_stage_execution(execution, plan, 1)
    assert se.has_failures is True
    assert 'a' in se.failed_node_ids


# ---------------------------------------------------------------------------
# record_node_completed
# ---------------------------------------------------------------------------

def test_record_node_completed_updates_completed_node_ids():
    _, plan = _make_plan(_node('a'))
    execution = _init(plan)
    execution, _ = record_node_completed(execution, plan, 'a')
    assert 'a' in execution.completed_node_ids


def test_record_node_completed_emits_lineage_event():
    _, plan = _make_plan(_node('a'))
    execution = _init(plan)
    _, events = record_node_completed(execution, plan, 'a')
    node_events = [e for e in events if e.event_type == EVENT_NODE_COMPLETED]
    assert len(node_events) == 1
    assert node_events[0].node_id == 'a'


def test_record_node_completed_advances_stage():
    _, plan = _make_plan(_node('a'), _node('b', dep_ids=['a']))
    execution = _init(plan)
    assert execution.active_stage_index == 0
    execution, events = record_node_completed(execution, plan, 'a')
    assert execution.active_stage_index == 1
    stage_events = [e for e in events if e.event_type == EVENT_STAGE_ADVANCED]
    assert len(stage_events) == 1


def test_record_node_completed_transitions_to_completed():
    _, plan = _make_plan(_node('a'))
    execution = _init(plan)
    execution, events = record_node_completed(execution, plan, 'a')
    assert execution.state == 'completed'
    transition_events = [
        e for e in events
        if e.event_type == EVENT_STATE_TRANSITION and e.new_state == 'completed'
    ]
    assert len(transition_events) == 1


def test_record_node_completed_increments_version():
    _, plan = _make_plan(_node('a'))
    execution = _init(plan)
    v_before = execution.version
    execution, _ = record_node_completed(execution, plan, 'a')
    assert execution.version > v_before


def test_record_node_completed_sorted_completed_ids():
    _, plan = _make_plan(
        _node('root'),
        _node('b', dep_ids=['root']),
        _node('a', dep_ids=['root']),
        _node('sink', dep_ids=['b', 'a']),
    )
    execution = _init(plan)
    execution, _ = record_node_completed(execution, plan, 'root')
    execution, _ = record_node_completed(execution, plan, 'b')
    execution, _ = record_node_completed(execution, plan, 'a')
    assert execution.completed_node_ids == sorted(execution.completed_node_ids)


def test_record_two_stage_workflow_completion():
    _, plan = _make_plan(_node('a'), _node('b', dep_ids=['a']))
    execution = _init(plan)
    execution, _ = record_node_completed(execution, plan, 'a')
    assert execution.state == 'executing'
    execution, _ = record_node_completed(execution, plan, 'b')
    assert execution.state == 'completed'


# ---------------------------------------------------------------------------
# record_node_failed — retry policy
# ---------------------------------------------------------------------------

def test_record_node_failed_no_retry_adds_to_failed():
    wf, plan = _make_plan(_node('a', max_attempts=1))
    execution = _init(plan)
    execution, events = record_node_failed(execution, plan, wf, 'a')
    assert 'a' in execution.failed_node_ids
    fail_events = [e for e in events if e.event_type == EVENT_NODE_FAILED]
    assert len(fail_events) == 1


def test_record_node_failed_no_retry_transitions_to_blocked():
    wf, plan = _make_plan(_node('a', max_attempts=1))
    execution = _init(plan)
    execution, _ = record_node_failed(execution, plan, wf, 'a')
    assert execution.state == 'blocked'


def test_record_node_failed_retry_available_not_in_failed():
    wf, plan = _make_plan(_node('a', max_attempts=3))
    execution = _init(plan)
    execution, events = record_node_failed(execution, plan, wf, 'a', 'first attempt')
    assert 'a' not in execution.failed_node_ids
    assert execution.node_attempts.get('a') == 1
    retry_events = [e for e in events if e.event_type == EVENT_NODE_RETRY]
    assert len(retry_events) == 1


def test_record_node_failed_retry_event_contains_attempt_count():
    wf, plan = _make_plan(_node('a', max_attempts=3))
    execution = _init(plan)
    _, events = record_node_failed(execution, plan, wf, 'a', 'first failure')
    retry_event = next(e for e in events if e.event_type == EVENT_NODE_RETRY)
    assert '1/3' in retry_event.reason or '1/' in retry_event.reason


def test_record_node_failed_retry_exhaustion_adds_to_failed():
    wf, plan = _make_plan(_node('a', max_attempts=2))
    execution = _init(plan)
    execution, _ = record_node_failed(execution, plan, wf, 'a', 'first')
    execution, events = record_node_failed(execution, plan, wf, 'a', 'second')
    assert 'a' in execution.failed_node_ids
    fail_events = [e for e in events if e.event_type == EVENT_NODE_FAILED]
    assert len(fail_events) == 1


def test_record_node_failed_retry_exhaustion_increments_attempts():
    wf, plan = _make_plan(_node('a', max_attempts=2))
    execution = _init(plan)
    execution, _ = record_node_failed(execution, plan, wf, 'a')
    assert execution.node_attempts['a'] == 1
    execution, _ = record_node_failed(execution, plan, wf, 'a')
    assert execution.node_attempts['a'] == 2


def test_record_node_failed_independent_branches_stay_executing():
    """When a node fails but independent branches remain, stay 'executing'."""
    wf, plan = _make_plan(
        _node('root'),
        _node('fail_me', dep_ids=['root'], max_attempts=1),
        _node('independent', dep_ids=['root']),
        _node('sink', dep_ids=['fail_me', 'independent']),
    )
    execution = _init(plan)
    execution, _ = record_node_completed(execution, plan, 'root')
    execution, _ = record_node_failed(execution, plan, wf, 'fail_me')
    # 'independent' can still complete
    assert execution.state == 'executing'


def test_record_node_failed_no_retries_reason_in_lineage():
    wf, plan = _make_plan(_node('a', max_attempts=1))
    execution = _init(plan)
    _, events = record_node_failed(execution, plan, wf, 'a', 'handler error')
    fail_evt = next(e for e in events if e.event_type == EVENT_NODE_FAILED)
    assert 'handler error' in fail_evt.reason


# ---------------------------------------------------------------------------
# detect_outcome
# ---------------------------------------------------------------------------

def test_detect_outcome_executing_at_start():
    _, plan = _make_plan(_node('a'))
    execution = _init(plan)
    assert detect_outcome(execution, plan) == 'executing'


def test_detect_outcome_completed_when_all_done():
    _, plan = _make_plan(_node('a'))
    execution = _init(plan)
    execution, _ = record_node_completed(execution, plan, 'a')
    assert detect_outcome(execution, plan) == 'completed'


def test_detect_outcome_blocked_when_no_progress_possible():
    wf, plan = _make_plan(
        _node('root', max_attempts=1),
        _node('child', dep_ids=['root']),
    )
    execution = _init(plan)
    execution, _ = record_node_failed(execution, plan, wf, 'root')
    assert detect_outcome(execution, plan) == 'blocked'


def test_detect_outcome_executing_with_independent_branch_after_failure():
    wf, plan = _make_plan(
        _node('root'),
        _node('fail_branch', dep_ids=['root'], max_attempts=1),
        _node('ok_branch', dep_ids=['root']),
    )
    execution = _init(plan)
    execution, _ = record_node_completed(execution, plan, 'root')
    execution, _ = record_node_failed(execution, plan, wf, 'fail_branch')
    assert detect_outcome(execution, plan) == 'executing'


def test_detect_outcome_blocked_after_independent_branch_also_completes():
    wf, plan = _make_plan(
        _node('root'),
        _node('fail_branch', dep_ids=['root'], max_attempts=1),
        _node('ok_branch', dep_ids=['root']),
        _node('sink', dep_ids=['fail_branch', 'ok_branch']),
    )
    execution = _init(plan)
    execution, _ = record_node_completed(execution, plan, 'root')
    execution, _ = record_node_failed(execution, plan, wf, 'fail_branch')
    execution, _ = record_node_completed(execution, plan, 'ok_branch')
    # 'ok_branch' completed but 'sink' can't run (depends on failed 'fail_branch')
    assert detect_outcome(execution, plan) in ('blocked', 'executing')
    assert execution.state == 'blocked'


# ---------------------------------------------------------------------------
# Determinism across repeated calls
# ---------------------------------------------------------------------------

def test_get_ready_node_ids_deterministic_repeated_calls():
    _, plan = _make_plan(
        _node('root'),
        _node('a', dep_ids=['root']),
        _node('b', dep_ids=['root']),
    )
    execution = _init(plan)
    execution, _ = record_node_completed(execution, plan, 'root')
    r1 = get_ready_node_ids(execution, plan)
    r2 = get_ready_node_ids(execution, plan)
    assert r1 == r2


def test_stage_advancement_only_on_success_not_failure():
    wf, plan = _make_plan(
        _node('root', max_attempts=1),
        _node('child', dep_ids=['root']),
    )
    execution = _init(plan)
    assert execution.active_stage_index == 0
    execution, _ = record_node_failed(execution, plan, wf, 'root')
    # Stage should NOT advance when a failure occurs
    assert execution.active_stage_index == 0

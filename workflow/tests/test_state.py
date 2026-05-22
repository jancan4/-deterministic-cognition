"""Tests for workflow execution state models and state machine."""
import pytest

from workflow.state import (
    EVENT_NODE_COMPLETED,
    EVENT_STATE_TRANSITION,
    TERMINAL_WORKFLOW_EXECUTION_STATES,
    VALID_WORKFLOW_EXECUTION_STATES,
    VALID_WORKFLOW_EXECUTION_TRANSITIONS,
    WorkflowExecution,
    WorkflowExecutionLineageEvent,
    WorkflowExecutionTransitionError,
    WorkflowStageExecution,
    make_execution_id,
    validate_execution_transition,
)


# ---------------------------------------------------------------------------
# State machine constants
# ---------------------------------------------------------------------------

def test_all_states_have_transition_entries():
    for state in VALID_WORKFLOW_EXECUTION_STATES:
        assert state in VALID_WORKFLOW_EXECUTION_TRANSITIONS, \
            f"State '{state}' missing from VALID_WORKFLOW_EXECUTION_TRANSITIONS"


def test_terminal_states_have_no_outgoing_transitions():
    for state in TERMINAL_WORKFLOW_EXECUTION_STATES:
        assert VALID_WORKFLOW_EXECUTION_TRANSITIONS[state] == frozenset(), \
            f"Terminal state '{state}' must have no transitions"


def test_all_transition_targets_are_valid_states():
    for state, targets in VALID_WORKFLOW_EXECUTION_TRANSITIONS.items():
        for t in targets:
            assert t in VALID_WORKFLOW_EXECUTION_STATES, \
                f"Transition target '{t}' from '{state}' is not a valid state"


# ---------------------------------------------------------------------------
# validate_execution_transition
# ---------------------------------------------------------------------------

def test_valid_transition_initialized_to_ready():
    validate_execution_transition('initialized', 'ready')  # must not raise


def test_valid_transition_ready_to_executing():
    validate_execution_transition('ready', 'executing')


def test_valid_transition_executing_to_completed():
    validate_execution_transition('executing', 'completed')


def test_valid_transition_executing_to_failed():
    validate_execution_transition('executing', 'failed')


def test_valid_transition_executing_to_blocked():
    validate_execution_transition('executing', 'blocked')


def test_valid_transition_blocked_to_failed():
    validate_execution_transition('blocked', 'failed')


def test_valid_transition_paused_to_executing():
    validate_execution_transition('paused', 'executing')


def test_invalid_transition_completed_to_executing():
    with pytest.raises(WorkflowExecutionTransitionError, match='not permitted'):
        validate_execution_transition('completed', 'executing')


def test_invalid_transition_cancelled_to_any():
    with pytest.raises(WorkflowExecutionTransitionError):
        validate_execution_transition('cancelled', 'executing')


def test_self_transition_raises():
    for state in VALID_WORKFLOW_EXECUTION_STATES:
        with pytest.raises(WorkflowExecutionTransitionError, match='Self-transition'):
            validate_execution_transition(state, state)


def test_unknown_source_state_raises():
    with pytest.raises(WorkflowExecutionTransitionError, match='Unknown source'):
        validate_execution_transition('nonexistent', 'executing')


def test_unknown_target_state_raises():
    with pytest.raises(WorkflowExecutionTransitionError, match='Unknown target'):
        validate_execution_transition('initialized', 'nonexistent')


def test_initialized_cannot_transition_to_executing_directly():
    with pytest.raises(WorkflowExecutionTransitionError):
        validate_execution_transition('initialized', 'executing')


# ---------------------------------------------------------------------------
# make_execution_id
# ---------------------------------------------------------------------------

def test_execution_id_is_64_char_hex():
    eid = make_execution_id('plan-abc', '2026-01-01T00:00:00Z')
    assert len(eid) == 64
    int(eid, 16)  # valid hex


def test_execution_id_is_deterministic():
    a = make_execution_id('plan-abc', '2026-01-01T00:00:00Z')
    b = make_execution_id('plan-abc', '2026-01-01T00:00:00Z')
    assert a == b


def test_execution_id_changes_with_different_plan_id():
    a = make_execution_id('plan-1', '2026-01-01T00:00:00Z')
    b = make_execution_id('plan-2', '2026-01-01T00:00:00Z')
    assert a != b


def test_execution_id_changes_with_different_timestamp():
    a = make_execution_id('plan-1', '2026-01-01T00:00:00Z')
    b = make_execution_id('plan-1', '2026-01-01T00:00:01Z')
    assert a != b


# ---------------------------------------------------------------------------
# WorkflowExecution
# ---------------------------------------------------------------------------

def _make_execution(**kwargs) -> WorkflowExecution:
    defaults = dict(
        execution_id='eid',
        workflow_id='wf',
        plan_id='pid',
        state='executing',
        active_stage_index=0,
        completed_node_ids=[],
        failed_node_ids=[],
        node_attempts={},
        created_at='2026-01-01T00:00:00Z',
        updated_at='2026-01-01T00:00:00Z',
        version=1,
    )
    defaults.update(kwargs)
    return WorkflowExecution(**defaults)


def test_workflow_execution_to_dict_keys():
    ex = _make_execution()
    d = ex.to_dict()
    assert set(d.keys()) == {
        'execution_id', 'workflow_id', 'plan_id', 'state',
        'active_stage_index', 'completed_node_ids', 'failed_node_ids',
        'node_attempts', 'created_at', 'updated_at', 'version',
    }


def test_workflow_execution_to_dict_node_attempts_sorted():
    ex = _make_execution(node_attempts={'z': 2, 'a': 1})
    d = ex.to_dict()
    keys = list(d['node_attempts'].keys())
    assert keys == ['a', 'z']


def test_workflow_execution_roundtrip():
    ex = _make_execution(
        completed_node_ids=['a', 'b'],
        failed_node_ids=['c'],
        node_attempts={'c': 1},
        version=5,
    )
    assert WorkflowExecution.from_dict(ex.to_dict()) == ex


def test_workflow_execution_from_dict_defaults_empty_lists():
    ex = WorkflowExecution.from_dict({
        'execution_id': 'e', 'workflow_id': 'w', 'plan_id': 'p',
        'state': 'initialized', 'active_stage_index': 0,
        'created_at': '2026-01-01T00:00:00Z',
        'updated_at': '2026-01-01T00:00:00Z',
        'version': 1,
    })
    assert ex.completed_node_ids == []
    assert ex.failed_node_ids == []
    assert ex.node_attempts == {}


# ---------------------------------------------------------------------------
# WorkflowExecutionLineageEvent
# ---------------------------------------------------------------------------

def test_lineage_event_to_dict_state_transition():
    evt = WorkflowExecutionLineageEvent(
        execution_id='eid',
        event_type=EVENT_STATE_TRANSITION,
        old_state='executing',
        new_state='completed',
        node_id=None,
        stage_index=2,
        reason='done',
        created_at='2026-01-01T00:00:00Z',
    )
    d = evt.to_dict()
    assert d['event_type'] == EVENT_STATE_TRANSITION
    assert d['old_state'] == 'executing'
    assert d['new_state'] == 'completed'
    assert d['node_id'] is None


def test_lineage_event_to_dict_node_completed():
    evt = WorkflowExecutionLineageEvent(
        execution_id='eid',
        event_type=EVENT_NODE_COMPLETED,
        old_state=None,
        new_state=None,
        node_id='fetch',
        stage_index=0,
        reason='handler succeeded',
        created_at='2026-01-01T00:00:00Z',
    )
    d = evt.to_dict()
    assert d['node_id'] == 'fetch'
    assert d['old_state'] is None


# ---------------------------------------------------------------------------
# WorkflowStageExecution
# ---------------------------------------------------------------------------

def test_workflow_stage_execution_is_complete_true():
    se = WorkflowStageExecution(
        stage_index=0,
        node_ids=['a', 'b'],
        completed_node_ids=['a', 'b'],
        failed_node_ids=[],
        pending_node_ids=[],
        is_complete=True,
        has_failures=False,
    )
    assert se.is_complete is True
    assert se.has_failures is False


def test_workflow_stage_execution_has_failures_true():
    se = WorkflowStageExecution(
        stage_index=1,
        node_ids=['a', 'b'],
        completed_node_ids=['a'],
        failed_node_ids=['b'],
        pending_node_ids=[],
        is_complete=False,
        has_failures=True,
    )
    assert se.has_failures is True


def test_workflow_stage_execution_to_dict():
    se = WorkflowStageExecution(
        stage_index=0,
        node_ids=['a'],
        completed_node_ids=['a'],
        failed_node_ids=[],
        pending_node_ids=[],
        is_complete=True,
        has_failures=False,
    )
    d = se.to_dict()
    assert d['is_complete'] is True
    assert d['stage_index'] == 0

"""Tests for workflow lineage replay engine."""
import pytest

from workflow.replay import (
    ReplayResult,
    _validate_delta_events,
    replay_execution,
    replay_from_snapshot,
    validate_lineage,
)
from workflow.state import (
    EVENT_NODE_COMPLETED,
    EVENT_NODE_FAILED,
    EVENT_NODE_RETRY,
    EVENT_NODE_SUBMITTED,
    EVENT_STAGE_ADVANCED,
    EVENT_STATE_TRANSITION,
    WorkflowExecution,
    WorkflowExecutionLineageEvent,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _evt(event_type, **kwargs) -> WorkflowExecutionLineageEvent:
    defaults = dict(
        execution_id='eid-1',
        event_type=event_type,
        old_state=None,
        new_state=None,
        node_id=None,
        stage_index=0,
        reason='test',
        created_at='2026-01-01T00:00:00Z',
    )
    defaults.update(kwargs)
    return WorkflowExecutionLineageEvent(**defaults)


def _transition(old_state, new_state, **kwargs):
    return _evt(EVENT_STATE_TRANSITION, old_state=old_state, new_state=new_state, **kwargs)


def _node_completed(node_id, **kwargs):
    return _evt(EVENT_NODE_COMPLETED, node_id=node_id, **kwargs)


def _node_failed(node_id, **kwargs):
    return _evt(EVENT_NODE_FAILED, node_id=node_id, **kwargs)


def _node_retry(node_id, **kwargs):
    return _evt(EVENT_NODE_RETRY, node_id=node_id, **kwargs)


def _stage_advanced(stage_index, **kwargs):
    return _evt(EVENT_STAGE_ADVANCED, stage_index=stage_index, **kwargs)


def _node_submitted(node_id, **kwargs):
    return _evt(EVENT_NODE_SUBMITTED, node_id=node_id, **kwargs)


def _minimal_sequence():
    """initialized → ready → executing"""
    return [
        _transition(None, 'initialized'),
        _transition('initialized', 'ready'),
        _transition('ready', 'executing'),
    ]


def _snapshot(**kwargs) -> WorkflowExecution:
    defaults = dict(
        execution_id='eid-1',
        workflow_id='wf',
        plan_id='plan-1',
        state='executing',
        active_stage_index=0,
        completed_node_ids=[],
        failed_node_ids=[],
        node_attempts={},
        created_at='2026-01-01T00:00:00Z',
        updated_at='2026-01-01T00:00:00Z',
        version=3,
    )
    defaults.update(kwargs)
    return WorkflowExecution(**defaults)


# ---------------------------------------------------------------------------
# validate_lineage
# ---------------------------------------------------------------------------

def test_validate_empty_lineage_returns_error():
    errors = validate_lineage([])
    assert errors
    assert any('empty' in e.lower() for e in errors)


def test_validate_valid_minimal_sequence():
    errors = validate_lineage(_minimal_sequence())
    assert errors == []


def test_validate_first_event_must_be_initialized():
    events = [_transition(None, 'ready')]
    errors = validate_lineage(events)
    assert errors
    assert any('initialized' in e for e in errors)


def test_validate_all_events_same_execution_id():
    events = _minimal_sequence()
    events.append(_evt(EVENT_NODE_COMPLETED, execution_id='other-eid', node_id='fetch'))
    errors = validate_lineage(events)
    assert any("other-eid" in e for e in errors)


def test_validate_invalid_state_transition():
    events = [
        _transition(None, 'initialized'),
        _transition('initialized', 'executing'),  # skips ready
    ]
    errors = validate_lineage(events)
    assert errors


def test_validate_duplicate_node_completed():
    events = _minimal_sequence() + [
        _node_completed('fetch'),
        _node_completed('fetch'),
    ]
    errors = validate_lineage(events)
    assert any('duplicate' in e.lower() for e in errors)


def test_validate_node_failed_after_completed():
    events = _minimal_sequence() + [
        _node_completed('fetch'),
        _node_failed('fetch'),
    ]
    errors = validate_lineage(events)
    assert any('fetch' in e for e in errors)


def test_validate_event_after_completed_state():
    events = _minimal_sequence() + [
        _node_completed('a'),
        _transition('executing', 'completed'),
        _node_completed('b'),  # after terminal
    ]
    errors = validate_lineage(events)
    assert any('terminal' in e.lower() for e in errors)


def test_validate_event_after_cancelled_state():
    events = [
        _transition(None, 'initialized'),
        _transition('initialized', 'ready'),
        _transition('ready', 'cancelled'),
        _node_completed('a'),
    ]
    errors = validate_lineage(events)
    assert any('terminal' in e.lower() for e in errors)


def test_validate_node_submitted_does_not_cause_errors():
    events = _minimal_sequence() + [
        _node_submitted('fetch'),
    ]
    assert validate_lineage(events) == []


def test_validate_stage_advanced_is_valid():
    events = _minimal_sequence() + [
        _node_completed('fetch'),
        _stage_advanced(0),  # stage 0 advanced → now at stage 1
    ]
    assert validate_lineage(events) == []


# ---------------------------------------------------------------------------
# replay_execution — state reconstruction
# ---------------------------------------------------------------------------

def test_replay_empty_events_returns_invalid():
    result = replay_execution([])
    assert result.is_valid is False
    assert result.execution is None
    assert result.events_applied == 0


def test_replay_minimal_sequence_produces_executing_state():
    events = _minimal_sequence()
    result = replay_execution(events)
    assert result.is_valid
    assert result.execution.state == 'executing'


def test_replay_events_applied_count():
    events = _minimal_sequence()
    result = replay_execution(events)
    assert result.events_applied == 3


def test_replay_node_completed_adds_to_completed_list():
    events = _minimal_sequence() + [_node_completed('fetch')]
    result = replay_execution(events)
    assert 'fetch' in result.execution.completed_node_ids


def test_replay_multiple_nodes_completed_sorted():
    events = _minimal_sequence() + [
        _node_completed('z'),
        _node_completed('a'),
        _node_completed('m'),
    ]
    result = replay_execution(events)
    assert result.execution.completed_node_ids == ['a', 'm', 'z']


def test_replay_node_failed_adds_to_failed_list():
    events = _minimal_sequence() + [_node_failed('fetch')]
    result = replay_execution(events)
    assert 'fetch' in result.execution.failed_node_ids


def test_replay_node_retry_increments_attempts():
    events = _minimal_sequence() + [
        _node_retry('fetch'),
        _node_retry('fetch'),
    ]
    result = replay_execution(events)
    assert result.execution.node_attempts.get('fetch') == 2


def test_replay_node_failed_increments_attempts():
    events = _minimal_sequence() + [
        _node_retry('fetch'),
        _node_failed('fetch'),
    ]
    result = replay_execution(events)
    assert result.execution.node_attempts.get('fetch') == 2


def test_replay_stage_advanced_updates_stage_index():
    events = _minimal_sequence() + [
        _node_completed('fetch'),
        _stage_advanced(0),
    ]
    result = replay_execution(events)
    assert result.execution.active_stage_index == 1


def test_replay_multiple_stage_advances():
    events = _minimal_sequence() + [
        _node_completed('a'),
        _stage_advanced(0),
        _node_completed('b'),
        _stage_advanced(1),
    ]
    result = replay_execution(events)
    assert result.execution.active_stage_index == 2


def test_replay_node_submitted_does_not_change_state():
    events = _minimal_sequence() + [_node_submitted('fetch')]
    result = replay_execution(events)
    assert result.execution.state == 'executing'
    assert result.execution.completed_node_ids == []


def test_replay_completed_workflow():
    events = _minimal_sequence() + [
        _node_completed('a'),
        _node_completed('b'),
        _transition('executing', 'completed'),
    ]
    result = replay_execution(events)
    assert result.is_valid
    assert result.execution.state == 'completed'
    assert 'a' in result.execution.completed_node_ids
    assert 'b' in result.execution.completed_node_ids


def test_replay_blocked_workflow():
    events = _minimal_sequence() + [
        _node_failed('fetch'),
        _transition('executing', 'blocked'),
    ]
    result = replay_execution(events)
    assert result.is_valid
    assert result.execution.state == 'blocked'


def test_replay_invalid_sequence_returns_is_valid_false():
    events = [
        _transition(None, 'initialized'),
        _transition('initialized', 'executing'),  # invalid transition
    ]
    result = replay_execution(events)
    assert result.is_valid is False
    assert result.validation_errors


def test_replay_version_increments_per_state_change():
    events = _minimal_sequence()  # 3 state transitions
    result = replay_execution(events)
    assert result.execution.version == 3


def test_replay_version_includes_node_events():
    events = _minimal_sequence() + [
        _node_completed('a'),
        _node_failed('b'),
        _node_retry('c'),
        _stage_advanced(0),
    ]
    result = replay_execution(events)
    assert result.execution.version == 3 + 4  # 3 transitions + 4 node events


def test_replay_is_deterministic():
    events = _minimal_sequence() + [
        _node_completed('x'),
        _stage_advanced(0),
    ]
    r1 = replay_execution(events)
    r2 = replay_execution(events)
    assert r1.execution == r2.execution


# ---------------------------------------------------------------------------
# replay_from_snapshot
# ---------------------------------------------------------------------------

def test_replay_from_snapshot_no_delta():
    snap = _snapshot()
    result = replay_from_snapshot(snap, [])
    assert result.is_valid
    assert result.execution == snap
    assert result.events_applied == 0


def test_replay_from_snapshot_applies_delta():
    snap = _snapshot(state='executing', version=3)
    delta = [
        _node_completed('fetch'),
        _stage_advanced(0),
        _transition('executing', 'completed'),
    ]
    result = replay_from_snapshot(snap, delta)
    assert result.execution.state == 'completed'
    assert 'fetch' in result.execution.completed_node_ids
    assert result.execution.active_stage_index == 1
    assert result.events_applied == 3


def test_replay_from_snapshot_version_increments():
    snap = _snapshot(version=10)
    delta = [_node_completed('a'), _transition('executing', 'completed')]
    result = replay_from_snapshot(snap, delta)
    assert result.execution.version == 12


def test_replay_from_snapshot_preserves_workflow_id_and_plan_id():
    snap = _snapshot(workflow_id='my-wf', plan_id='my-plan')
    result = replay_from_snapshot(snap, [])
    assert result.execution.workflow_id == 'my-wf'
    assert result.execution.plan_id == 'my-plan'


def test_replay_from_snapshot_retry_increments_attempts():
    snap = _snapshot(node_attempts={'fetch': 1})
    delta = [_node_retry('fetch')]
    result = replay_from_snapshot(snap, delta)
    assert result.execution.node_attempts['fetch'] == 2


def test_replay_from_snapshot_node_submitted_no_change():
    snap = _snapshot()
    delta = [_node_submitted('fetch')]
    result = replay_from_snapshot(snap, delta)
    assert result.execution == snap
    assert result.events_applied == 1


# ---------------------------------------------------------------------------
# W-4: validate_lineage checks old_state against current_state
# ---------------------------------------------------------------------------

def test_validate_old_state_matches_current_state():
    """Correct old_state on a transition passes validation."""
    events = [
        _transition(None, 'initialized'),
        _transition('initialized', 'ready'),   # old_state matches
        _transition('ready', 'executing'),      # old_state matches
    ]
    assert validate_lineage(events) == []


def test_validate_old_state_mismatch_raises_error():
    """old_state that disagrees with tracked current_state is an error."""
    events = [
        _transition(None, 'initialized'),
        _transition('initialized', 'ready'),
        _transition('initialized', 'executing'),  # old_state='initialized' but current='ready'
    ]
    errors = validate_lineage(events)
    assert errors
    assert any("old_state" in e for e in errors)


def test_validate_old_state_none_is_always_accepted():
    """old_state=None means 'not recorded' and skips the check."""
    events = [
        _transition(None, 'initialized'),
        _evt(EVENT_STATE_TRANSITION, old_state=None, new_state='ready'),
        _evt(EVENT_STATE_TRANSITION, old_state=None, new_state='executing'),
    ]
    assert validate_lineage(events) == []


def test_validate_old_state_mismatch_on_first_transition_not_checked():
    """The first state_transition (bootstrapping) has no old_state constraint."""
    events = [_transition('anything', 'initialized')]
    # The bootstrap path only checks new_state == 'initialized', not old_state.
    # old_state is ignored on the very first event.
    errors = validate_lineage(events)
    assert not errors


# ---------------------------------------------------------------------------
# C-2: replay_execution recovers workflow_id and plan_id from init event metadata
# ---------------------------------------------------------------------------

def test_replay_reconstructs_workflow_id_from_metadata():
    """Pure replay (no DB) recovers workflow_id from the init event's metadata."""
    events = [
        _evt(EVENT_STATE_TRANSITION, new_state='initialized',
             metadata={'workflow_id': 'wf-xyz', 'plan_id': 'plan-abc'}),
        _transition('initialized', 'ready'),
        _transition('ready', 'executing'),
    ]
    result = replay_execution(events)
    assert result.is_valid
    assert result.execution.workflow_id == 'wf-xyz'


def test_replay_reconstructs_plan_id_from_metadata():
    events = [
        _evt(EVENT_STATE_TRANSITION, new_state='initialized',
             metadata={'workflow_id': 'wf-xyz', 'plan_id': 'plan-abc'}),
        _transition('initialized', 'ready'),
        _transition('ready', 'executing'),
    ]
    result = replay_execution(events)
    assert result.execution.plan_id == 'plan-abc'


def test_replay_identity_empty_when_no_metadata():
    """Events without metadata produce empty identity strings (not errors)."""
    events = _minimal_sequence()  # no metadata
    result = replay_execution(events)
    assert result.is_valid
    assert result.execution.workflow_id == ''
    assert result.execution.plan_id == ''


def test_replay_identity_independent_of_event_order_of_non_init_events():
    """Only the init event's metadata is used for identity — subsequent events don't override."""
    events = [
        _evt(EVENT_STATE_TRANSITION, new_state='initialized',
             metadata={'workflow_id': 'correct-wf', 'plan_id': 'correct-plan'}),
        _evt(EVENT_STATE_TRANSITION, old_state='initialized', new_state='ready',
             metadata={'workflow_id': 'wrong-wf'}),
        _transition('ready', 'executing'),
    ]
    result = replay_execution(events)
    assert result.execution.workflow_id == 'correct-wf'


# ---------------------------------------------------------------------------
# W-2: replay_from_snapshot validates delta events
# ---------------------------------------------------------------------------

def test_replay_from_snapshot_rejects_wrong_execution_id():
    snap = _snapshot(execution_id='eid-1')
    delta = [_evt(EVENT_NODE_COMPLETED, execution_id='eid-WRONG', node_id='fetch')]
    result = replay_from_snapshot(snap, delta)
    assert result.is_valid is False
    assert any('eid-WRONG' in e for e in result.validation_errors)


def test_replay_from_snapshot_rejects_events_when_snapshot_is_terminal():
    snap = _snapshot(state='completed')
    delta = [_node_completed('fetch')]
    result = replay_from_snapshot(snap, delta)
    assert result.is_valid is False
    assert any('terminal' in e.lower() for e in result.validation_errors)


def test_replay_from_snapshot_rejects_cancelled_snapshot_with_delta():
    snap = _snapshot(state='cancelled')
    delta = [_node_submitted('fetch')]
    result = replay_from_snapshot(snap, delta)
    assert result.is_valid is False


def test_replay_from_snapshot_rejects_duplicate_node_completion_vs_snapshot():
    snap = _snapshot(completed_node_ids=['fetch'])
    delta = [_node_completed('fetch')]  # already in snapshot
    result = replay_from_snapshot(snap, delta)
    assert result.is_valid is False
    assert any('fetch' in e for e in result.validation_errors)


def test_replay_from_snapshot_rejects_duplicate_node_completion_within_delta():
    snap = _snapshot()
    delta = [_node_completed('fetch'), _node_completed('fetch')]
    result = replay_from_snapshot(snap, delta)
    assert result.is_valid is False
    assert any('duplicate' in e.lower() for e in result.validation_errors)


def test_replay_from_snapshot_rejects_node_failed_after_completed_in_snapshot():
    snap = _snapshot(completed_node_ids=['fetch'])
    delta = [_node_failed('fetch')]
    result = replay_from_snapshot(snap, delta)
    assert result.is_valid is False


def test_replay_from_snapshot_rejects_node_failed_after_completed_in_delta():
    snap = _snapshot()
    delta = [_node_completed('fetch'), _node_failed('fetch')]
    result = replay_from_snapshot(snap, delta)
    assert result.is_valid is False


def test_replay_from_snapshot_rejects_events_after_terminal_within_delta():
    snap = _snapshot()
    delta = [
        _transition('executing', 'completed'),
        _node_submitted('fetch'),  # after terminal
    ]
    result = replay_from_snapshot(snap, delta)
    assert result.is_valid is False
    assert any('terminal' in e.lower() for e in result.validation_errors)


def test_replay_from_snapshot_invalid_returns_snapshot_unchanged():
    """When delta is invalid, execution returned is the unmodified snapshot."""
    snap = _snapshot(completed_node_ids=['already-done'])
    delta = [_node_completed('already-done')]  # duplicate
    result = replay_from_snapshot(snap, delta)
    assert result.is_valid is False
    assert result.execution == snap
    assert result.events_applied == 0


def test_replay_from_snapshot_rejects_invalid_state_transition_in_delta():
    snap = _snapshot(state='executing')
    delta = [_transition('executing', 'initialized')]  # not a valid transition
    result = replay_from_snapshot(snap, delta)
    assert result.is_valid is False


def test_replay_from_snapshot_valid_delta_still_applies_correctly():
    """Valid deltas continue to apply and produce is_valid=True."""
    snap = _snapshot(state='executing', version=3)
    delta = [
        _node_completed('fetch'),
        _transition('executing', 'completed'),
    ]
    result = replay_from_snapshot(snap, delta)
    assert result.is_valid is True
    assert result.execution.state == 'completed'
    assert 'fetch' in result.execution.completed_node_ids

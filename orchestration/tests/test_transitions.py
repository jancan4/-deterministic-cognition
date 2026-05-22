"""
Hermetic unit tests for orchestration/transitions.py.

All tests are pure — no database, no filesystem.
"""

import pytest
from orchestration.models import TERMINAL_STATES, VALID_STATES, VALID_TRANSITIONS
from orchestration.transitions import (
    TransitionError,
    can_transition,
    current_state_from_lineage,
    get_valid_transitions,
    is_terminal,
    replay_state_history,
    validate_transition,
)


# ---------------------------------------------------------------------------
# Valid transitions
# ---------------------------------------------------------------------------

class TestValidTransitions:
    @pytest.mark.parametrize('old,new', [
        ('pending',   'ready'),
        ('pending',   'blocked'),
        ('pending',   'cancelled'),
        ('ready',     'running'),
        ('ready',     'blocked'),
        ('ready',     'cancelled'),
        ('running',   'completed'),
        ('running',   'failed'),
        ('running',   'blocked'),
        ('blocked',   'ready'),
        ('blocked',   'cancelled'),
        ('failed',    'ready'),
        ('failed',    'cancelled'),
        ('completed', 'superseded'),
    ])
    def test_valid_transition_passes(self, old, new):
        validate_transition(old, new)  # must not raise

    @pytest.mark.parametrize('old,new', [
        ('pending',   'ready'),
        ('ready',     'running'),
        ('running',   'completed'),
        ('failed',    'ready'),
        ('blocked',   'ready'),
    ])
    def test_can_transition_true(self, old, new):
        assert can_transition(old, new) is True


# ---------------------------------------------------------------------------
# Invalid transitions
# ---------------------------------------------------------------------------

class TestInvalidTransitions:
    @pytest.mark.parametrize('old,new', [
        ('pending',   'running'),
        ('pending',   'completed'),
        ('pending',   'failed'),
        ('pending',   'superseded'),
        ('ready',     'pending'),
        ('ready',     'completed'),
        ('ready',     'failed'),
        ('running',   'pending'),
        ('running',   'ready'),
        ('running',   'cancelled'),
        ('running',   'superseded'),
        ('blocked',   'running'),
        ('blocked',   'completed'),
        ('blocked',   'failed'),
        ('failed',    'running'),
        ('failed',    'completed'),
        ('failed',    'blocked'),
        ('completed', 'ready'),
        ('completed', 'running'),
        ('completed', 'failed'),
        ('completed', 'cancelled'),
        ('cancelled', 'ready'),
        ('cancelled', 'running'),
        ('cancelled', 'pending'),
        ('superseded','ready'),
        ('superseded','running'),
    ])
    def test_invalid_transition_raises(self, old, new):
        with pytest.raises(TransitionError):
            validate_transition(old, new)

    @pytest.mark.parametrize('old,new', [
        ('pending',   'running'),
        ('completed', 'ready'),
        ('cancelled', 'pending'),
    ])
    def test_can_transition_false(self, old, new):
        assert can_transition(old, new) is False

    def test_unknown_source_state_raises(self):
        with pytest.raises(TransitionError, match='Unknown source state'):
            validate_transition('nonexistent', 'ready')

    def test_unknown_target_state_raises(self):
        with pytest.raises(TransitionError, match='Unknown target state'):
            validate_transition('pending', 'nonexistent')

    def test_error_message_contains_states(self):
        with pytest.raises(TransitionError) as exc:
            validate_transition('pending', 'completed')
        msg = str(exc.value)
        assert 'pending' in msg
        assert 'completed' in msg


# ---------------------------------------------------------------------------
# Terminal states
# ---------------------------------------------------------------------------

class TestTerminalStates:
    @pytest.mark.parametrize('state', ['cancelled', 'superseded'])
    def test_terminal_states_identified(self, state):
        assert is_terminal(state) is True

    @pytest.mark.parametrize('state', ['pending', 'ready', 'running', 'blocked', 'failed', 'completed'])
    def test_non_terminal_states_identified(self, state):
        assert is_terminal(state) is False

    @pytest.mark.parametrize('state', ['cancelled', 'superseded'])
    def test_terminal_states_have_no_outgoing_transitions(self, state):
        assert get_valid_transitions(state) == frozenset()

    @pytest.mark.parametrize('state', ['cancelled', 'superseded'])
    def test_transition_from_terminal_raises(self, state):
        for target in VALID_STATES:
            if target != state:
                with pytest.raises(TransitionError):
                    validate_transition(state, target)


# ---------------------------------------------------------------------------
# get_valid_transitions
# ---------------------------------------------------------------------------

class TestGetValidTransitions:
    def test_pending_transitions(self):
        assert get_valid_transitions('pending') == frozenset({'ready', 'blocked', 'cancelled'})

    def test_ready_transitions(self):
        assert get_valid_transitions('ready') == frozenset({'running', 'blocked', 'cancelled'})

    def test_running_transitions(self):
        assert get_valid_transitions('running') == frozenset({'completed', 'failed', 'blocked'})

    def test_blocked_transitions(self):
        assert get_valid_transitions('blocked') == frozenset({'ready', 'cancelled'})

    def test_failed_transitions(self):
        assert get_valid_transitions('failed') == frozenset({'ready', 'cancelled'})

    def test_completed_transitions(self):
        assert get_valid_transitions('completed') == frozenset({'superseded'})

    def test_unknown_state_returns_empty(self):
        assert get_valid_transitions('nonexistent') == frozenset()

    def test_all_states_have_entries_in_map(self):
        for state in VALID_STATES:
            assert state in VALID_TRANSITIONS


# ---------------------------------------------------------------------------
# Lineage replay
# ---------------------------------------------------------------------------

class TestReplayStateHistory:
    def _make_event(self, old, new):
        from orchestration.models import TaskLineageEvent
        return TaskLineageEvent(
            id=1, task_id=1,
            old_state=old, new_state=new,
            reason='test', actor='tester',
            dependency_snapshot=[], metadata={},
            created_at='2026-05-21T00:00:00Z',
        )

    def test_creation_event(self):
        ev = self._make_event(None, 'pending')
        history = replay_state_history([ev])
        assert history == [(None, 'pending')]

    def test_full_happy_path(self):
        events = [
            self._make_event(None,      'pending'),
            self._make_event('pending', 'ready'),
            self._make_event('ready',   'running'),
            self._make_event('running', 'completed'),
        ]
        history = replay_state_history(events)
        assert history == [
            (None,      'pending'),
            ('pending', 'ready'),
            ('ready',   'running'),
            ('running', 'completed'),
        ]

    def test_retry_path(self):
        events = [
            self._make_event(None,      'pending'),
            self._make_event('pending', 'ready'),
            self._make_event('ready',   'running'),
            self._make_event('running', 'failed'),
            self._make_event('failed',  'ready'),
            self._make_event('ready',   'running'),
            self._make_event('running', 'completed'),
        ]
        history = replay_state_history(events)
        assert len(history) == 7
        assert history[3] == ('running', 'failed')
        assert history[4] == ('failed',  'ready')

    def test_empty_lineage_returns_empty(self):
        assert replay_state_history([]) == []

    def test_current_state_from_lineage(self):
        events = [
            self._make_event(None,      'pending'),
            self._make_event('pending', 'ready'),
            self._make_event('ready',   'running'),
        ]
        assert current_state_from_lineage(events) == 'running'

    def test_current_state_empty_lineage(self):
        assert current_state_from_lineage([]) is None

    def test_replay_is_deterministic(self):
        ev = self._make_event('pending', 'ready')
        r1 = replay_state_history([ev])
        r2 = replay_state_history([ev])
        assert r1 == r2


# ---------------------------------------------------------------------------
# State machine completeness
# ---------------------------------------------------------------------------

class TestStateMachineCompleteness:
    def test_all_valid_states_in_transition_map(self):
        for state in VALID_STATES:
            assert state in VALID_TRANSITIONS, f"'{state}' missing from VALID_TRANSITIONS"

    def test_all_transition_targets_are_valid_states(self):
        for src, targets in VALID_TRANSITIONS.items():
            for tgt in targets:
                assert tgt in VALID_STATES, f"Target '{tgt}' not in VALID_STATES"

    def test_terminal_states_in_terminal_frozenset(self):
        for state in VALID_STATES:
            if not VALID_TRANSITIONS[state]:
                assert state in TERMINAL_STATES

    def test_no_self_transitions(self):
        for state in VALID_STATES:
            assert state not in VALID_TRANSITIONS[state], \
                f"Self-transition detected: '{state}' → '{state}'"

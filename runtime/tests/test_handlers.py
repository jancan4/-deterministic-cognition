"""
Hermetic handler registry and dispatch tests.

No network, no shared state, no side-effects between test functions.
Integration tests that need an orchestration DB create one via tmp_path.
"""
import json

import pytest

from orchestration.service import (
    create_task,
    get_lineage,
    init_db as orch_init_db,
    transition_task,
)
from runtime.handlers import (
    HandlerNotFoundError,
    HandlerRegistrationError,
    HandlerResult,
    TaskHandlerRegistry,
    execute_handler,
)
from runtime.models import RuntimeConfig
from runtime.runner import run_iterations
from runtime.service import execute_task
from runtime.state_store import init_db as rt_init_db, register_runtime


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _orch_db(tmp_path):
    db = str(tmp_path / 'orch.db')
    orch_init_db(db)
    return db


def _rt_db(tmp_path):
    db = str(tmp_path / 'rt.db')
    rt_init_db(db)
    return db


def _ready_task(orch_db, task_type='research'):
    t = create_task(orch_db, f'{task_type}-task', task_type, 'actor')
    return transition_task(orch_db, t.id, 'ready', reason='unblocked', actor='actor')


def _noop_handler(task):
    return {'handled': True, 'task_type': task.task_type}


def _failing_handler(task):
    raise RuntimeError(f"deliberate failure for {task.task_type}")


def _cfg(max_iterations=2):
    return RuntimeConfig(actor='test-actor', max_iterations=max_iterations,
                         poll_interval_s=0.0, checkpoint_every=1)


# ---------------------------------------------------------------------------
# TaskHandlerRegistry — registration
# ---------------------------------------------------------------------------

def test_register_handler_callable():
    registry = TaskHandlerRegistry()
    registry.register('research', _noop_handler)
    assert registry.has('research')


def test_register_lambda_accepted():
    registry = TaskHandlerRegistry()
    registry.register('analysis', lambda task: {})
    assert registry.has('analysis')


def test_register_callable_class_accepted():
    class MyHandler:
        def __call__(self, task):
            return {}

    registry = TaskHandlerRegistry()
    registry.register('validation', MyHandler())
    assert registry.has('validation')


def test_register_empty_task_type_rejected():
    registry = TaskHandlerRegistry()
    with pytest.raises(HandlerRegistrationError, match='task_type'):
        registry.register('', _noop_handler)


def test_register_whitespace_task_type_rejected():
    registry = TaskHandlerRegistry()
    with pytest.raises(HandlerRegistrationError, match='task_type'):
        registry.register('   ', _noop_handler)


def test_register_noncallable_string_rejected():
    registry = TaskHandlerRegistry()
    with pytest.raises(HandlerRegistrationError, match='callable'):
        registry.register('research', 'not-a-function')


def test_register_noncallable_int_rejected():
    registry = TaskHandlerRegistry()
    with pytest.raises(HandlerRegistrationError, match='callable'):
        registry.register('research', 42)


def test_register_noncallable_none_rejected():
    registry = TaskHandlerRegistry()
    with pytest.raises(HandlerRegistrationError, match='callable'):
        registry.register('research', None)


def test_register_duplicate_rejected_by_default():
    registry = TaskHandlerRegistry()
    registry.register('research', _noop_handler)
    with pytest.raises(HandlerRegistrationError, match="already registered"):
        registry.register('research', _noop_handler)


def test_register_replace_false_explicit_rejects_duplicate():
    registry = TaskHandlerRegistry()
    registry.register('research', _noop_handler)
    with pytest.raises(HandlerRegistrationError):
        registry.register('research', _failing_handler, replace=False)


def test_register_replace_true_overrides():
    registry = TaskHandlerRegistry()
    registry.register('research', _noop_handler)
    registry.register('research', _failing_handler, replace=True)
    assert registry.get('research') is _failing_handler


def test_register_different_types_independent():
    registry = TaskHandlerRegistry()
    registry.register('research', _noop_handler)
    registry.register('analysis', _failing_handler)
    assert registry.has('research')
    assert registry.has('analysis')


# ---------------------------------------------------------------------------
# TaskHandlerRegistry — query
# ---------------------------------------------------------------------------

def test_has_returns_true_for_registered():
    registry = TaskHandlerRegistry()
    registry.register('research', _noop_handler)
    assert registry.has('research') is True


def test_has_returns_false_for_unregistered():
    registry = TaskHandlerRegistry()
    assert registry.has('research') is False


def test_get_returns_registered_callable():
    registry = TaskHandlerRegistry()
    registry.register('research', _noop_handler)
    assert registry.get('research') is _noop_handler


def test_get_not_found_raises():
    registry = TaskHandlerRegistry()
    with pytest.raises(HandlerNotFoundError):
        registry.get('unknown')


def test_list_handlers_empty():
    registry = TaskHandlerRegistry()
    assert registry.list_handlers() == []


def test_list_handlers_returns_sorted():
    registry = TaskHandlerRegistry()
    # Register in non-alphabetical order.
    registry.register('validation', _noop_handler)
    registry.register('analysis', _noop_handler)
    registry.register('research', _noop_handler)
    assert registry.list_handlers() == ['analysis', 'research', 'validation']


def test_list_handlers_deterministic_regardless_of_registration_order():
    r1 = TaskHandlerRegistry()
    r2 = TaskHandlerRegistry()
    for tt in ('z', 'a', 'm'):
        r1.register(tt, _noop_handler)
    for tt in ('a', 'z', 'm'):
        r2.register(tt, _noop_handler)
    assert r1.list_handlers() == r2.list_handlers()


# ---------------------------------------------------------------------------
# TaskHandlerRegistry — unregister
# ---------------------------------------------------------------------------

def test_unregister_removes_handler():
    registry = TaskHandlerRegistry()
    registry.register('research', _noop_handler)
    registry.unregister('research')
    assert not registry.has('research')


def test_unregister_missing_raises():
    registry = TaskHandlerRegistry()
    with pytest.raises(HandlerNotFoundError, match="Cannot unregister"):
        registry.unregister('nonexistent')


def test_unregister_then_reregister_works():
    registry = TaskHandlerRegistry()
    registry.register('research', _noop_handler)
    registry.unregister('research')
    registry.register('research', _failing_handler)
    assert registry.get('research') is _failing_handler


# ---------------------------------------------------------------------------
# execute_handler — success path
# ---------------------------------------------------------------------------

def test_execute_handler_success(tmp_path):
    orch_db = _orch_db(tmp_path)
    task = _ready_task(orch_db, 'research')
    running = transition_task(orch_db, task.id, 'running', reason='r', actor='a')

    registry = TaskHandlerRegistry()
    registry.register('research', _noop_handler)
    result = execute_handler(registry, running)

    assert result.success is True
    assert result.task_id == running.id
    assert result.task_type == 'research'
    assert result.error is None


def test_execute_handler_result_json_serialized(tmp_path):
    orch_db = _orch_db(tmp_path)
    task = _ready_task(orch_db, 'research')
    running = transition_task(orch_db, task.id, 'running', reason='r', actor='a')

    registry = TaskHandlerRegistry()
    registry.register('research', lambda t: {'score': 42, 'label': 'ok'})
    result = execute_handler(registry, running)

    parsed = json.loads(result.result_json)
    assert parsed == {'label': 'ok', 'score': 42}


def test_execute_handler_result_json_sort_keys(tmp_path):
    orch_db = _orch_db(tmp_path)
    task = _ready_task(orch_db, 'research')
    running = transition_task(orch_db, task.id, 'running', reason='r', actor='a')

    registry = TaskHandlerRegistry()
    registry.register('research', lambda t: {'z': 3, 'a': 1, 'm': 2})
    result = execute_handler(registry, running)

    assert result.result_json == '{"a": 1, "m": 2, "z": 3}'


def test_execute_handler_none_result_becomes_empty_dict(tmp_path):
    orch_db = _orch_db(tmp_path)
    task = _ready_task(orch_db, 'research')
    running = transition_task(orch_db, task.id, 'running', reason='r', actor='a')

    registry = TaskHandlerRegistry()
    registry.register('research', lambda t: None)
    result = execute_handler(registry, running)

    assert result.result_json == '{}'
    assert result.success is True


def test_execute_handler_metadata_serialized(tmp_path):
    orch_db = _orch_db(tmp_path)
    task = _ready_task(orch_db, 'research')
    running = transition_task(orch_db, task.id, 'running', reason='r', actor='a')

    registry = TaskHandlerRegistry()
    registry.register('research', _noop_handler)
    result = execute_handler(registry, running, metadata={'run': 1, 'batch': 'a'})

    parsed = json.loads(result.metadata_json)
    assert parsed == {'batch': 'a', 'run': 1}


def test_execute_handler_deterministic_result_json(tmp_path):
    orch_db = _orch_db(tmp_path)
    task = _ready_task(orch_db, 'research')
    running = transition_task(orch_db, task.id, 'running', reason='r', actor='a')

    registry = TaskHandlerRegistry()
    registry.register('research', lambda t: {'b': 2, 'a': 1})

    r1 = execute_handler(registry, running)
    r2 = execute_handler(registry, running)
    assert r1.result_json == r2.result_json


# ---------------------------------------------------------------------------
# execute_handler — failure paths
# ---------------------------------------------------------------------------

def test_execute_handler_exception_captured(tmp_path):
    orch_db = _orch_db(tmp_path)
    task = _ready_task(orch_db, 'research')
    running = transition_task(orch_db, task.id, 'running', reason='r', actor='a')

    registry = TaskHandlerRegistry()
    registry.register('research', _failing_handler)
    result = execute_handler(registry, running)

    assert result.success is False
    assert result.error is not None
    assert 'deliberate failure' in result.error


def test_execute_handler_exception_result_json_empty(tmp_path):
    orch_db = _orch_db(tmp_path)
    task = _ready_task(orch_db, 'research')
    running = transition_task(orch_db, task.id, 'running', reason='r', actor='a')

    registry = TaskHandlerRegistry()
    registry.register('research', _failing_handler)
    result = execute_handler(registry, running)

    assert result.result_json == '{}'


def test_execute_handler_missing_handler_result(tmp_path):
    orch_db = _orch_db(tmp_path)
    task = _ready_task(orch_db, 'research')
    running = transition_task(orch_db, task.id, 'running', reason='r', actor='a')

    registry = TaskHandlerRegistry()  # empty — no handlers registered
    result = execute_handler(registry, running)

    assert result.success is False
    assert result.error == 'missing_handler:research'
    assert result.result_json == '{}'


def test_execute_handler_does_not_raise_on_exception(tmp_path):
    orch_db = _orch_db(tmp_path)
    task = _ready_task(orch_db, 'research')
    running = transition_task(orch_db, task.id, 'running', reason='r', actor='a')

    registry = TaskHandlerRegistry()
    registry.register('research', lambda t: 1 / 0)  # ZeroDivisionError

    # Must return a HandlerResult, not raise.
    result = execute_handler(registry, running)
    assert isinstance(result, HandlerResult)
    assert result.success is False


# ---------------------------------------------------------------------------
# execute_task integration (orchestration DB)
# ---------------------------------------------------------------------------

def test_execute_task_with_registry_transitions_completed(tmp_path):
    orch_db = _orch_db(tmp_path)
    registry = TaskHandlerRegistry()
    registry.register('research', _noop_handler)

    task = _ready_task(orch_db, 'research')
    result = execute_task(orch_db, task.id, 'runner', registry=registry)

    assert result.state == 'completed'


def test_execute_task_without_registry_transitions_failed(tmp_path):
    orch_db = _orch_db(tmp_path)
    task = _ready_task(orch_db, 'research')
    result = execute_task(orch_db, task.id, 'runner', registry=None)

    assert result.state == 'failed'


def test_execute_task_without_registry_reason_is_missing_handler(tmp_path):
    orch_db = _orch_db(tmp_path)
    task = _ready_task(orch_db, 'research')
    execute_task(orch_db, task.id, 'runner', registry=None)

    lineage = get_lineage(orch_db, task.id)
    failed_event = lineage[-1]
    assert failed_event.new_state == 'failed'
    assert 'missing_handler:research' in failed_event.reason


def test_execute_task_unregistered_type_transitions_failed(tmp_path):
    orch_db = _orch_db(tmp_path)
    registry = TaskHandlerRegistry()
    registry.register('analysis', _noop_handler)  # 'research' not registered

    task = _ready_task(orch_db, 'research')
    result = execute_task(orch_db, task.id, 'runner', registry=registry)

    assert result.state == 'failed'


def test_execute_task_unregistered_type_reason_in_lineage(tmp_path):
    orch_db = _orch_db(tmp_path)
    registry = TaskHandlerRegistry()

    task = _ready_task(orch_db, 'research')
    execute_task(orch_db, task.id, 'runner', registry=registry)

    lineage = get_lineage(orch_db, task.id)
    assert 'missing_handler:research' in lineage[-1].reason


def test_execute_task_handler_exception_transitions_failed(tmp_path):
    orch_db = _orch_db(tmp_path)
    registry = TaskHandlerRegistry()
    registry.register('research', _failing_handler)

    task = _ready_task(orch_db, 'research')
    result = execute_task(orch_db, task.id, 'runner', registry=registry)

    assert result.state == 'failed'


def test_execute_task_handler_exception_reason_in_lineage(tmp_path):
    orch_db = _orch_db(tmp_path)
    registry = TaskHandlerRegistry()
    registry.register('research', _failing_handler)

    task = _ready_task(orch_db, 'research')
    execute_task(orch_db, task.id, 'runner', registry=registry)

    lineage = get_lineage(orch_db, task.id)
    failed_event = lineage[-1]
    assert failed_event.new_state == 'failed'
    assert 'deliberate failure' in failed_event.reason


def test_execute_task_lineage_shows_running_then_completed(tmp_path):
    orch_db = _orch_db(tmp_path)
    registry = TaskHandlerRegistry()
    registry.register('research', _noop_handler)

    task = _ready_task(orch_db, 'research')
    execute_task(orch_db, task.id, 'runner', registry=registry)

    lineage = get_lineage(orch_db, task.id)
    states = [e.new_state for e in lineage]
    assert 'running' in states
    assert 'completed' in states
    assert states.index('running') < states.index('completed')


def test_execute_task_lineage_shows_running_then_failed(tmp_path):
    orch_db = _orch_db(tmp_path)
    task = _ready_task(orch_db, 'research')
    execute_task(orch_db, task.id, 'runner', registry=None)

    lineage = get_lineage(orch_db, task.id)
    states = [e.new_state for e in lineage]
    assert 'running' in states
    assert 'failed' in states
    assert states.index('running') < states.index('failed')


def test_handler_cannot_bypass_invalid_transition(tmp_path):
    """
    A handler that attempts an invalid orchestration transition raises
    TransitionError inside execute_handler, which captures it.
    The task ends in failed, not in a corrupted state.
    """
    from orchestration.service import transition_task as orch_transition
    from orchestration.transitions import TransitionError

    orch_db = _orch_db(tmp_path)

    def bad_handler(task):
        # running → cancelled is invalid per the task state machine.
        orch_transition(orch_db, task.id, 'cancelled', reason='bypass', actor='bad')
        return {}

    registry = TaskHandlerRegistry()
    registry.register('research', bad_handler)

    task = _ready_task(orch_db, 'research')
    result = execute_task(orch_db, task.id, 'runner', registry=registry)

    # TransitionError was captured; task ends in failed (not cancelled).
    assert result.state == 'failed'
    lineage = get_lineage(orch_db, task.id)
    final_states = [e.new_state for e in lineage]
    assert 'cancelled' not in final_states


# ---------------------------------------------------------------------------
# Runner integration — handler dispatch end-to-end
# ---------------------------------------------------------------------------

def test_runner_with_registry_completes_tasks(tmp_path):
    from orchestration.service import list_tasks
    rt_db = _rt_db(tmp_path)
    orch_db = _orch_db(tmp_path)
    registry = TaskHandlerRegistry()
    registry.register('research', _noop_handler)

    _ready_task(orch_db, 'research')
    rt = register_runtime(rt_db, 'r', orch_db, _cfg())
    run_iterations(rt_db, rt.id, orch_db, _cfg(max_iterations=1), registry=registry)

    assert len(list_tasks(orch_db, state='completed')) == 1


def test_runner_without_registry_fails_tasks(tmp_path):
    from orchestration.service import list_tasks
    rt_db = _rt_db(tmp_path)
    orch_db = _orch_db(tmp_path)

    _ready_task(orch_db, 'research')
    rt = register_runtime(rt_db, 'r', orch_db, _cfg())
    run_iterations(rt_db, rt.id, orch_db, _cfg(max_iterations=1), registry=None)

    assert len(list_tasks(orch_db, state='failed')) == 1
    assert len(list_tasks(orch_db, state='completed')) == 0


def test_runner_without_registry_still_counts_dispatched(tmp_path):
    rt_db = _rt_db(tmp_path)
    orch_db = _orch_db(tmp_path)

    _ready_task(orch_db, 'research')
    _ready_task(orch_db, 'research')
    rt = register_runtime(rt_db, 'r', orch_db, _cfg())
    result = run_iterations(rt_db, rt.id, orch_db, _cfg(max_iterations=1), registry=None)

    assert result.tasks_executed == 2


def test_runner_mixed_task_types_partial_dispatch(tmp_path):
    """Registry has 'research' but not 'analysis'. analysis tasks fail, research completes."""
    from orchestration.service import list_tasks
    rt_db = _rt_db(tmp_path)
    orch_db = _orch_db(tmp_path)

    registry = TaskHandlerRegistry()
    registry.register('research', _noop_handler)

    _ready_task(orch_db, 'research')
    _ready_task(orch_db, 'analysis')

    rt = register_runtime(rt_db, 'r', orch_db, _cfg())
    run_iterations(rt_db, rt.id, orch_db, _cfg(max_iterations=1), registry=registry)

    assert len(list_tasks(orch_db, state='completed')) == 1
    assert len(list_tasks(orch_db, state='failed')) == 1


def test_runner_handler_exception_fails_task_continues_loop(tmp_path):
    """A handler exception on one task does not abort the iteration loop."""
    from orchestration.service import list_tasks
    rt_db = _rt_db(tmp_path)
    orch_db = _orch_db(tmp_path)

    registry = TaskHandlerRegistry()
    registry.register('research', _failing_handler)

    _ready_task(orch_db, 'research')
    _ready_task(orch_db, 'research')

    rt = register_runtime(rt_db, 'r', orch_db, _cfg())
    result = run_iterations(rt_db, rt.id, orch_db, _cfg(max_iterations=1), registry=registry)

    # Both tasks dispatched even though both failed.
    assert result.tasks_executed == 2
    assert len(list_tasks(orch_db, state='failed')) == 2

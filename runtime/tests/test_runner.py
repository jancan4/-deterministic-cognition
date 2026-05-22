"""
Hermetic runner tests.

Each test creates its own tmp_path databases (state_db and orchestration_db).
No network, no global state, no shared fixtures between test functions.
"""
import pytest

from orchestration.service import create_task, init_db as orch_init_db, transition_task
from runtime.models import RuntimeConfig, TransitionError
from runtime.runner import (
    pause_runtime,
    recover_runtime,
    resume_runtime,
    run_iterations,
    stop_runtime,
)
from runtime.state_store import (
    get_all_checkpoints,
    get_latest_checkpoint,
    get_runtime,
    get_runtime_lineage,
    init_db as rt_init_db,
    register_runtime,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _rt_db(tmp_path):
    db = str(tmp_path / 'runtime.db')
    rt_init_db(db)
    return db


def _orch_db(tmp_path):
    db = str(tmp_path / 'orch.db')
    orch_init_db(db)
    return db


def _cfg(**kwargs):
    defaults = dict(actor='test-actor', max_iterations=3, poll_interval_s=0.0, checkpoint_every=1)
    defaults.update(kwargs)
    return RuntimeConfig(**defaults)


def _runtime(rt_db, orch_db, cfg=None):
    return register_runtime(rt_db, 'test-runtime', orch_db, cfg or _cfg())


def _add_ready_task(orch_db, title='task', n=1):
    tasks = []
    for i in range(n):
        t = create_task(orch_db, f'{title}-{i}', 'research', 'actor')
        transition_task(orch_db, t.id, 'ready', reason='unblocked', actor='actor')
        tasks.append(t)
    return tasks


# ---------------------------------------------------------------------------
# Basic run — no tasks
# ---------------------------------------------------------------------------

def test_run_zero_max_iterations(tmp_path):
    rt_db = _rt_db(tmp_path)
    orch_db = _orch_db(tmp_path)
    rt = _runtime(rt_db, orch_db, _cfg(max_iterations=0))
    result = run_iterations(rt_db, rt.id, orch_db, _cfg(max_iterations=0))
    assert result.iterations_completed == 0
    assert result.tasks_executed == 0
    assert result.final_state == 'paused'
    assert 'max_iterations' in result.stopped_reason


def test_run_no_tasks_completes_iterations(tmp_path):
    rt_db = _rt_db(tmp_path)
    orch_db = _orch_db(tmp_path)
    rt = _runtime(rt_db, orch_db)
    result = run_iterations(rt_db, rt.id, orch_db, _cfg(max_iterations=3))
    assert result.iterations_completed == 3
    assert result.tasks_executed == 0
    assert result.final_state == 'paused'


def test_run_final_state_is_paused_after_max(tmp_path):
    rt_db = _rt_db(tmp_path)
    orch_db = _orch_db(tmp_path)
    rt = _runtime(rt_db, orch_db)
    result = run_iterations(rt_db, rt.id, orch_db, _cfg(max_iterations=1))
    runtime = get_runtime(rt_db, rt.id)
    assert runtime.state == 'paused'


# ---------------------------------------------------------------------------
# Run with tasks
# ---------------------------------------------------------------------------

def test_run_executes_ready_tasks(tmp_path):
    rt_db = _rt_db(tmp_path)
    orch_db = _orch_db(tmp_path)
    _add_ready_task(orch_db, n=2)
    rt = _runtime(rt_db, orch_db)
    result = run_iterations(rt_db, rt.id, orch_db, _cfg(max_iterations=1))
    assert result.tasks_executed == 2


def test_run_tasks_are_completed_after_execution(tmp_path):
    from orchestration.service import list_tasks
    rt_db = _rt_db(tmp_path)
    orch_db = _orch_db(tmp_path)
    _add_ready_task(orch_db, n=1)
    rt = _runtime(rt_db, orch_db)
    run_iterations(rt_db, rt.id, orch_db, _cfg(max_iterations=1))
    completed = list_tasks(orch_db, state='completed')
    assert len(completed) == 1


def test_run_multiple_iterations_accumulates_tasks_executed(tmp_path):
    rt_db = _rt_db(tmp_path)
    orch_db = _orch_db(tmp_path)
    # 3 tasks will be executed on iteration 1; iterations 2-3 have no ready tasks.
    _add_ready_task(orch_db, n=3)
    rt = _runtime(rt_db, orch_db)
    result = run_iterations(rt_db, rt.id, orch_db, _cfg(max_iterations=3))
    assert result.tasks_executed == 3


# ---------------------------------------------------------------------------
# should_stop signal
# ---------------------------------------------------------------------------

def test_run_stops_on_should_stop(tmp_path):
    rt_db = _rt_db(tmp_path)
    orch_db = _orch_db(tmp_path)
    rt = _runtime(rt_db, orch_db, _cfg(max_iterations=100))
    call_count = {'n': 0}

    def stop_after_two():
        call_count['n'] += 1
        return call_count['n'] > 2

    result = run_iterations(rt_db, rt.id, orch_db, _cfg(max_iterations=100), should_stop=stop_after_two)
    assert result.iterations_completed == 2
    assert result.final_state == 'paused'
    assert 'should_stop' in result.stopped_reason


def test_run_stops_immediately_if_should_stop_true_from_start(tmp_path):
    rt_db = _rt_db(tmp_path)
    orch_db = _orch_db(tmp_path)
    rt = _runtime(rt_db, orch_db)
    result = run_iterations(rt_db, rt.id, orch_db, _cfg(max_iterations=10),
                            should_stop=lambda: True)
    assert result.iterations_completed == 0


# ---------------------------------------------------------------------------
# Checkpoints
# ---------------------------------------------------------------------------

def test_run_saves_checkpoint_each_iteration(tmp_path):
    rt_db = _rt_db(tmp_path)
    orch_db = _orch_db(tmp_path)
    rt = _runtime(rt_db, orch_db)
    _add_ready_task(orch_db, n=1)
    run_iterations(rt_db, rt.id, orch_db, _cfg(max_iterations=3, checkpoint_every=1))
    cps = get_all_checkpoints(rt_db, rt.id)
    # Checkpoint saved when tasks exist (executing→checkpointing path).
    # No-task iterations go idle→polling→idle without checkpointing.
    assert len(cps) >= 1


def test_run_checkpoint_every_skips_non_matching_iterations(tmp_path):
    rt_db = _rt_db(tmp_path)
    orch_db = _orch_db(tmp_path)
    rt = _runtime(rt_db, orch_db)
    _add_ready_task(orch_db, n=1)
    # checkpoint_every=2: only checkpoint on even iterations.
    run_iterations(rt_db, rt.id, orch_db, _cfg(max_iterations=4, checkpoint_every=2))
    cps = get_all_checkpoints(rt_db, rt.id)
    for cp in cps:
        assert cp.iteration % 2 == 0


def test_latest_checkpoint_reflects_last_run(tmp_path):
    rt_db = _rt_db(tmp_path)
    orch_db = _orch_db(tmp_path)
    rt = _runtime(rt_db, orch_db)
    _add_ready_task(orch_db, n=1)
    result = run_iterations(rt_db, rt.id, orch_db, _cfg(max_iterations=2))
    cp = get_latest_checkpoint(rt_db, rt.id)
    assert cp is not None
    assert cp.state['tasks_executed'] >= 1


# ---------------------------------------------------------------------------
# Lineage correctness
# ---------------------------------------------------------------------------

def test_run_lineage_includes_all_transitions(tmp_path):
    rt_db = _rt_db(tmp_path)
    orch_db = _orch_db(tmp_path)
    _add_ready_task(orch_db, n=1)
    rt = _runtime(rt_db, orch_db)
    run_iterations(rt_db, rt.id, orch_db, _cfg(max_iterations=1))
    lineage = get_runtime_lineage(rt_db, rt.id)
    states = [e.new_state for e in lineage]
    # Must have passed through polling and executing.
    assert 'polling' in states
    assert 'executing' in states


def test_run_lineage_starts_with_initialized(tmp_path):
    rt_db = _rt_db(tmp_path)
    orch_db = _orch_db(tmp_path)
    rt = _runtime(rt_db, orch_db)
    run_iterations(rt_db, rt.id, orch_db, _cfg(max_iterations=1))
    lineage = get_runtime_lineage(rt_db, rt.id)
    assert lineage[0].new_state == 'initialized'


def test_run_lineage_ends_with_paused(tmp_path):
    rt_db = _rt_db(tmp_path)
    orch_db = _orch_db(tmp_path)
    rt = _runtime(rt_db, orch_db)
    run_iterations(rt_db, rt.id, orch_db, _cfg(max_iterations=1))
    lineage = get_runtime_lineage(rt_db, rt.id)
    assert lineage[-1].new_state == 'paused'


# ---------------------------------------------------------------------------
# pause_runtime
# ---------------------------------------------------------------------------

def test_pause_runtime_from_idle(tmp_path):
    from runtime.state_store import transition_runtime
    rt_db = _rt_db(tmp_path)
    orch_db = _orch_db(tmp_path)
    rt = _runtime(rt_db, orch_db)
    transition_runtime(rt_db, rt.id, 'idle', reason='starting', iteration=0)
    paused = pause_runtime(rt_db, rt.id, reason='manual pause')
    assert paused.state == 'paused'


def test_pause_runtime_invalid_from_stopped(tmp_path):
    from runtime.state_store import transition_runtime
    rt_db = _rt_db(tmp_path)
    orch_db = _orch_db(tmp_path)
    rt = _runtime(rt_db, orch_db)
    transition_runtime(rt_db, rt.id, 'stopped', reason='done', iteration=0)
    with pytest.raises(TransitionError):
        pause_runtime(rt_db, rt.id, reason='too late')


# ---------------------------------------------------------------------------
# stop_runtime
# ---------------------------------------------------------------------------

def test_stop_runtime_from_paused(tmp_path):
    from runtime.state_store import transition_runtime
    rt_db = _rt_db(tmp_path)
    orch_db = _orch_db(tmp_path)
    rt = _runtime(rt_db, orch_db)
    transition_runtime(rt_db, rt.id, 'idle', reason='r', iteration=0)
    transition_runtime(rt_db, rt.id, 'paused', reason='r', iteration=0)
    stopped = stop_runtime(rt_db, rt.id, reason='shutting down')
    assert stopped.state == 'stopped'


def test_stop_runtime_is_terminal(tmp_path):
    from runtime.state_store import transition_runtime
    rt_db = _rt_db(tmp_path)
    orch_db = _orch_db(tmp_path)
    rt = _runtime(rt_db, orch_db)
    transition_runtime(rt_db, rt.id, 'idle', reason='r', iteration=0)
    transition_runtime(rt_db, rt.id, 'paused', reason='r', iteration=0)
    stop_runtime(rt_db, rt.id, reason='done')
    with pytest.raises(TransitionError):
        stop_runtime(rt_db, rt.id, reason='already stopped')


# ---------------------------------------------------------------------------
# recover_runtime
# ---------------------------------------------------------------------------

def test_recover_runtime_from_interrupted(tmp_path):
    from runtime.state_store import transition_runtime
    rt_db = _rt_db(tmp_path)
    orch_db = _orch_db(tmp_path)
    rt = _runtime(rt_db, orch_db)
    transition_runtime(rt_db, rt.id, 'idle', reason='r', iteration=0)
    transition_runtime(rt_db, rt.id, 'interrupted', reason='crash', iteration=1)
    recovered = recover_runtime(rt_db, rt.id, reason='recovering after crash')
    assert recovered.state == 'idle'


def test_recover_runtime_from_failed(tmp_path):
    from runtime.state_store import transition_runtime
    rt_db = _rt_db(tmp_path)
    orch_db = _orch_db(tmp_path)
    rt = _runtime(rt_db, orch_db)
    transition_runtime(rt_db, rt.id, 'idle', reason='r', iteration=0)
    transition_runtime(rt_db, rt.id, 'interrupted', reason='r', iteration=0)
    transition_runtime(rt_db, rt.id, 'recovering', reason='r', iteration=0)
    transition_runtime(rt_db, rt.id, 'failed', reason='unrecoverable', iteration=0)
    transition_runtime(rt_db, rt.id, 'recovering', reason='retry', iteration=0)
    # recover_runtime: recovering → idle
    result = recover_runtime(rt_db, rt.id, reason='second attempt')
    assert result.state == 'idle'


# ---------------------------------------------------------------------------
# resume_runtime
# ---------------------------------------------------------------------------

def test_resume_runtime_continues_from_paused(tmp_path):
    rt_db = _rt_db(tmp_path)
    orch_db = _orch_db(tmp_path)
    rt = _runtime(rt_db, orch_db)
    # First run: 2 iterations.
    run_iterations(rt_db, rt.id, orch_db, _cfg(max_iterations=2))
    assert get_runtime(rt_db, rt.id).state == 'paused'
    # Resume for 2 more.
    result = resume_runtime(rt_db, rt.id, orch_db, _cfg(max_iterations=2))
    assert result.final_state == 'paused'
    assert result.iterations_completed == 2


def test_resume_runtime_invalid_from_stopped(tmp_path):
    from runtime.state_store import transition_runtime
    rt_db = _rt_db(tmp_path)
    orch_db = _orch_db(tmp_path)
    rt = _runtime(rt_db, orch_db)
    transition_runtime(rt_db, rt.id, 'stopped', reason='done', iteration=0)
    with pytest.raises(TransitionError):
        resume_runtime(rt_db, rt.id, orch_db, _cfg())


# ---------------------------------------------------------------------------
# service layer integration
# ---------------------------------------------------------------------------

def test_runtime_config_checkpoint_every_zero_rejected(tmp_path):
    with pytest.raises(ValueError, match='checkpoint_every'):
        RuntimeConfig(actor='a', checkpoint_every=0)


def test_runtime_config_checkpoint_every_negative_rejected(tmp_path):
    with pytest.raises(ValueError, match='checkpoint_every'):
        RuntimeConfig(actor='a', checkpoint_every=-1)


def test_runtime_config_checkpoint_every_one_accepted(tmp_path):
    cfg = RuntimeConfig(actor='a', checkpoint_every=1)
    assert cfg.checkpoint_every == 1


def test_recover_runtime_reason_in_lineage(tmp_path):
    from runtime.state_store import transition_runtime
    rt_db = _rt_db(tmp_path)
    orch_db = _orch_db(tmp_path)
    rt = _runtime(rt_db, orch_db)
    transition_runtime(rt_db, rt.id, 'idle', reason='r', iteration=0)
    transition_runtime(rt_db, rt.id, 'interrupted', reason='crash', iteration=3)
    recover_runtime(rt_db, rt.id, reason='post-crash recovery')
    lineage = get_runtime_lineage(rt_db, rt.id)
    # The recovering → idle lineage event must include the caller reason.
    idle_event = lineage[-1]
    assert idle_event.new_state == 'idle'
    assert 'post-crash recovery' in idle_event.reason


def test_pause_runtime_preserves_iteration_in_lineage(tmp_path):
    from runtime.state_store import transition_runtime
    rt_db = _rt_db(tmp_path)
    orch_db = _orch_db(tmp_path)
    rt = _runtime(rt_db, orch_db)
    transition_runtime(rt_db, rt.id, 'idle', reason='r', iteration=5)
    pause_runtime(rt_db, rt.id, reason='manual pause')
    lineage = get_runtime_lineage(rt_db, rt.id)
    pause_event = lineage[-1]
    assert pause_event.new_state == 'paused'
    assert pause_event.iteration == 5


def test_stop_runtime_preserves_iteration_in_lineage(tmp_path):
    from runtime.state_store import transition_runtime
    rt_db = _rt_db(tmp_path)
    orch_db = _orch_db(tmp_path)
    rt = _runtime(rt_db, orch_db)
    transition_runtime(rt_db, rt.id, 'idle', reason='r', iteration=7)
    transition_runtime(rt_db, rt.id, 'paused', reason='r', iteration=7)
    stop_runtime(rt_db, rt.id, reason='shutdown')
    lineage = get_runtime_lineage(rt_db, rt.id)
    stop_event = lineage[-1]
    assert stop_event.new_state == 'stopped'
    assert stop_event.iteration == 7


def test_count_task_retries_zero_initially(tmp_path):
    from runtime.service import count_task_retries
    orch_db = _orch_db(tmp_path)
    t = create_task(orch_db, 'task', 'research', 'actor')
    assert count_task_retries(orch_db, t.id) == 0


def test_count_task_retries_after_retry(tmp_path):
    from runtime.service import count_task_retries
    orch_db = _orch_db(tmp_path)
    t = create_task(orch_db, 'task', 'research', 'actor')
    transition_task(orch_db, t.id, 'ready', reason='r', actor='a')
    transition_task(orch_db, t.id, 'running', reason='r', actor='a')
    transition_task(orch_db, t.id, 'failed', reason='r', actor='a')
    transition_task(orch_db, t.id, 'ready', reason='retry', actor='a')
    assert count_task_retries(orch_db, t.id) == 1


def test_poll_ready_tasks_empty(tmp_path):
    from runtime.service import poll_ready_tasks
    orch_db = _orch_db(tmp_path)
    assert poll_ready_tasks(orch_db) == []


def test_poll_ready_tasks_returns_ready_only(tmp_path):
    from runtime.service import poll_ready_tasks
    orch_db = _orch_db(tmp_path)
    t1 = create_task(orch_db, 'ready-task', 'research', 'a')
    transition_task(orch_db, t1.id, 'ready', reason='r', actor='a')
    create_task(orch_db, 'pending-task', 'research', 'a')
    tasks = poll_ready_tasks(orch_db)
    assert len(tasks) == 1
    assert tasks[0].id == t1.id


def test_execute_task_transitions_to_completed(tmp_path):
    from runtime.service import execute_task
    orch_db = _orch_db(tmp_path)
    t = create_task(orch_db, 'task', 'research', 'a')
    transition_task(orch_db, t.id, 'ready', reason='r', actor='a')
    result = execute_task(orch_db, t.id, 'runner')
    assert result.state == 'completed'

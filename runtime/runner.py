"""
Supervised orchestration runner.

Executes bounded iteration loops against the task orchestration layer, recording
every state transition in runtime_lineage and saving checkpoints on schedule.

Design constraints:
- By default, every run must be bounded by at least one of: max_iterations or a
  should_stop callback. A run with neither raises ValueError unless
  config.allow_unbounded=True is explicitly set. Silent infinite loops are
  rejected at call time, not discovered at 3 AM.
- should_stop is a caller-supplied callable checked at the top of each iteration.
- KeyboardInterrupt is caught, the runtime transitions to 'interrupted', and a
  checkpoint is saved before the exception propagates.
- All state transitions are persisted before the corresponding side-effect, so
  a crash after a transition but before execution is recoverable.
"""
import time
from typing import Callable, Optional

from .checkpoints import create_checkpoint
from .handlers import TaskHandlerRegistry
from .models import Runtime, RunResult, RuntimeConfig
from .service import execute_task, poll_ready_tasks
from .state_store import (
    NotFoundError,
    get_latest_checkpoint,
    get_runtime,
    transition_runtime,
)


def run_iterations(
    state_db: str,
    runtime_id: int,
    orchestration_db: str,
    config: RuntimeConfig,
    should_stop: Optional[Callable[[], bool]] = None,
    registry: Optional[TaskHandlerRegistry] = None,
) -> RunResult:
    """
    Run up to config.max_iterations iterations of the poll → execute → checkpoint cycle.

    Returns a RunResult describing why the run stopped and how much work was done.

    Raises ValueError if both max_iterations and should_stop are absent and
    config.allow_unbounded is False — the default. Set config.allow_unbounded=True
    only for server-mode operation where an external signal drives termination.

    registry is the handler dispatch table. When None (the default), any task
    that is polled will transition to failed with reason 'missing_handler:{type}'.
    Pass an explicit TaskHandlerRegistry to enable real dispatch.

    Raises NotFoundError if runtime_id does not exist.
    """
    if config.max_iterations is None and should_stop is None and not config.allow_unbounded:
        raise ValueError(
            "unbounded runtime requires allow_unbounded=True or a should_stop callback: "
            "set config.max_iterations, supply a should_stop callable, or set "
            "config.allow_unbounded=True to acknowledge the loop will run indefinitely"
        )

    rt = get_runtime(state_db, runtime_id)

    # Allow resuming from paused by re-entering idle first.
    if rt.state == 'paused':
        rt = transition_runtime(
            state_db, runtime_id, 'idle',
            reason='Runner: resuming from paused',
            iteration=rt.current_iteration,
        )
    elif rt.state == 'initialized':
        rt = transition_runtime(
            state_db, runtime_id, 'idle',
            reason='Runner: starting from initialized',
            iteration=0,
        )

    iteration = rt.current_iteration
    tasks_executed = 0

    try:
        while True:
            if config.max_iterations is not None and iteration >= config.max_iterations:
                rt = transition_runtime(
                    state_db, runtime_id, 'paused',
                    reason=f'Runner: max_iterations ({config.max_iterations}) reached',
                    iteration=iteration,
                )
                return RunResult(
                    runtime_id=runtime_id,
                    iterations_completed=iteration,
                    tasks_executed=tasks_executed,
                    stopped_reason=f'max_iterations ({config.max_iterations}) reached',
                    final_state='paused',
                )

            if should_stop is not None and should_stop():
                rt = transition_runtime(
                    state_db, runtime_id, 'paused',
                    reason='Runner: stopped by should_stop signal',
                    iteration=iteration,
                )
                return RunResult(
                    runtime_id=runtime_id,
                    iterations_completed=iteration,
                    tasks_executed=tasks_executed,
                    stopped_reason='should_stop signal',
                    final_state='paused',
                )

            iteration += 1

            # Poll phase.
            rt = transition_runtime(
                state_db, runtime_id, 'polling',
                reason=f'Runner: polling iteration {iteration}',
                iteration=iteration,
            )
            ready_tasks = poll_ready_tasks(orchestration_db)

            if not ready_tasks:
                # No work; return to idle and wait.
                rt = transition_runtime(
                    state_db, runtime_id, 'idle',
                    reason='Runner: no ready tasks',
                    iteration=iteration,
                )
                if config.poll_interval_s > 0:
                    time.sleep(config.poll_interval_s)
                continue

            # Execute phase.
            rt = transition_runtime(
                state_db, runtime_id, 'executing',
                reason=f'Runner: executing {len(ready_tasks)} task(s)',
                iteration=iteration,
            )
            for task in ready_tasks:
                execute_task(orchestration_db, task.id, config.actor, registry=registry)
                tasks_executed += 1

            # Checkpoint phase (every N iterations).
            if iteration % config.checkpoint_every == 0:
                rt = transition_runtime(
                    state_db, runtime_id, 'checkpointing',
                    reason=f'Runner: checkpointing after iteration {iteration}',
                    iteration=iteration,
                )
                create_checkpoint(
                    state_db, runtime_id, iteration,
                    state={'iteration': iteration, 'tasks_executed': tasks_executed},
                    reason=f'Iteration {iteration} checkpoint',
                )
                rt = transition_runtime(
                    state_db, runtime_id, 'idle',
                    reason='Runner: checkpoint complete',
                    iteration=iteration,
                )
            else:
                rt = transition_runtime(
                    state_db, runtime_id, 'idle',
                    reason='Runner: execution complete',
                    iteration=iteration,
                )

            if config.poll_interval_s > 0:
                time.sleep(config.poll_interval_s)

    except KeyboardInterrupt:
        transition_runtime(
            state_db, runtime_id, 'interrupted',
            reason='Runner: KeyboardInterrupt received',
            iteration=iteration,
        )
        create_checkpoint(
            state_db, runtime_id, iteration,
            state={'iteration': iteration, 'tasks_executed': tasks_executed},
            reason='Interrupt checkpoint',
        )
        raise


def pause_runtime(
    state_db: str,
    runtime_id: int,
    reason: str,
    iteration: Optional[int] = None,
) -> Runtime:
    """
    Transition a runtime to paused from any pauseable state.

    If iteration is not supplied, the current_iteration stored in the DB is
    preserved in lineage rather than silently resetting to 0.
    """
    rt = get_runtime(state_db, runtime_id)
    eff_iter = rt.current_iteration if iteration is None else iteration
    return transition_runtime(state_db, runtime_id, 'paused',
                              reason=reason, iteration=eff_iter)


def stop_runtime(
    state_db: str,
    runtime_id: int,
    reason: str,
    iteration: Optional[int] = None,
) -> Runtime:
    """
    Transition a runtime to the terminal stopped state.

    If iteration is not supplied, the current_iteration stored in the DB is
    preserved in lineage rather than silently resetting to 0.
    """
    rt = get_runtime(state_db, runtime_id)
    eff_iter = rt.current_iteration if iteration is None else iteration
    return transition_runtime(state_db, runtime_id, 'stopped',
                              reason=reason, iteration=eff_iter)


def recover_runtime(
    state_db: str,
    runtime_id: int,
    reason: str,
    iteration: Optional[int] = None,
) -> Runtime:
    """
    Transition an interrupted or failed runtime through recovering → idle.

    If already in recovering state, skips the first transition but still
    records the caller's reason in the recovering → idle lineage event.
    If iteration is not supplied, current_iteration from the DB is preserved.
    """
    rt = get_runtime(state_db, runtime_id)
    eff_iter = rt.current_iteration if iteration is None else iteration
    if rt.state != 'recovering':
        transition_runtime(state_db, runtime_id, 'recovering',
                           reason=reason, iteration=eff_iter)
    return transition_runtime(state_db, runtime_id, 'idle',
                              reason=f'Recovery complete — {reason}',
                              iteration=eff_iter)


def resume_runtime(
    state_db: str,
    runtime_id: int,
    orchestration_db: str,
    config: RuntimeConfig,
    should_stop: Optional[Callable[[], bool]] = None,
    registry: Optional[TaskHandlerRegistry] = None,
) -> RunResult:
    """Resume a paused runtime. Delegates to run_iterations which handles paused → idle."""
    return run_iterations(state_db, runtime_id, orchestration_db, config, should_stop, registry)

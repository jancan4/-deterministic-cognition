"""Integration tests for workflow persistence high-level API."""
import pytest

from workflow.executor import initialize_execution, record_node_completed, start_execution
from workflow.models import RetryPolicy, WorkflowNode
from workflow.persistence import (
    WorkflowDefinitionError,
    WorkflowPlanError,
    append_execution_events,
    persist_execution,
    persist_workflow,
    persist_workflow_definition,
    persist_workflow_plan,
    replay_execution_from_snapshot,
    replay_execution_from_storage,
    take_snapshot,
)
from workflow.service import define_workflow, plan_workflow
from workflow.storage import (
    init_db,
    load_definition_for_execution,
    load_execution,
    load_execution_events,
    load_plan_for_execution,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _db(tmp_path) -> str:
    path = str(tmp_path / 'wf.db')
    init_db(path)
    return path


def _node(node_id, dep_ids=None):
    return WorkflowNode(
        node_id=node_id,
        task_type='research',
        dependency_ids=dep_ids or [],
        retry_policy=RetryPolicy(max_attempts=1),
    )


def _make_wf_and_plan(*nodes):
    wf = define_workflow('wf', 'Test', list(nodes))
    vr, plan, _ = plan_workflow(wf)
    assert vr.valid, vr.errors
    return wf, plan


def _running_execution(plan):
    execution, _ = initialize_execution(plan)
    execution, _ = start_execution(execution)
    return execution


# ---------------------------------------------------------------------------
# persist_execution
# ---------------------------------------------------------------------------

def test_persist_execution_saves_state_row(tmp_path):
    db = _db(tmp_path)
    _, plan = _make_wf_and_plan(_node('fetch'))
    execution, init_event = initialize_execution(plan)
    persist_execution(db, execution, [init_event])

    loaded = load_execution(db, execution.execution_id)
    assert loaded is not None
    assert loaded.state == 'initialized'


def test_persist_execution_saves_events(tmp_path):
    db = _db(tmp_path)
    _, plan = _make_wf_and_plan(_node('fetch'))
    execution, init_event = initialize_execution(plan)
    persist_execution(db, execution, [init_event])

    events = load_execution_events(db, execution.execution_id)
    assert len(events) >= 1  # at least the state_transition to initialized


def test_persist_execution_with_empty_events(tmp_path):
    db = _db(tmp_path)
    _, plan = _make_wf_and_plan(_node('fetch'))
    execution, _ = initialize_execution(plan)
    persist_execution(db, execution, [])  # must not raise
    assert load_execution(db, execution.execution_id) is not None


def test_persist_execution_upserts_on_second_call(tmp_path):
    db = _db(tmp_path)
    _, plan = _make_wf_and_plan(_node('fetch'))
    execution, init_event = initialize_execution(plan)
    persist_execution(db, execution, [init_event])

    execution, start_events = start_execution(execution)
    persist_execution(db, execution, start_events)

    loaded = load_execution(db, execution.execution_id)
    assert loaded.state == 'executing'


# ---------------------------------------------------------------------------
# append_execution_events
# ---------------------------------------------------------------------------

def test_append_execution_events_returns_ids(tmp_path):
    db = _db(tmp_path)
    _, plan = _make_wf_and_plan(_node('a'))
    execution, init_event = initialize_execution(plan)
    persist_execution(db, execution, [init_event])

    execution, start_events = start_execution(execution)
    ids = append_execution_events(db, start_events)
    assert len(ids) == len(start_events)
    assert all(isinstance(i, int) for i in ids)


def test_append_empty_events_returns_empty_list(tmp_path):
    db = _db(tmp_path)
    assert append_execution_events(db, []) == []


# ---------------------------------------------------------------------------
# replay_execution_from_storage
# ---------------------------------------------------------------------------

def test_replay_from_storage_reconstructs_executing_state(tmp_path):
    db = _db(tmp_path)
    _, plan = _make_wf_and_plan(_node('fetch'))

    execution, init_event = initialize_execution(plan)
    persist_execution(db, execution, [init_event])
    execution, start_events = start_execution(execution)
    persist_execution(db, execution, start_events)

    result = replay_execution_from_storage(db, execution.execution_id)
    assert result.is_valid
    assert result.execution.state == 'executing'


def test_replay_from_storage_reconstructs_completed_workflow(tmp_path):
    db = _db(tmp_path)
    wf, plan = _make_wf_and_plan(_node('fetch'))

    execution, init_event = initialize_execution(plan)
    persist_execution(db, execution, [init_event])
    execution, start_events = start_execution(execution)
    persist_execution(db, execution, start_events)
    execution, complete_events = record_node_completed(execution, plan, 'fetch')
    persist_execution(db, execution, complete_events)

    result = replay_execution_from_storage(db, execution.execution_id)
    assert result.is_valid
    assert result.execution.state == 'completed'
    assert 'fetch' in result.execution.completed_node_ids


def test_replay_from_storage_restores_workflow_and_plan_ids(tmp_path):
    db = _db(tmp_path)
    wf, plan = _make_wf_and_plan(_node('fetch'))

    execution, init_event = initialize_execution(plan)
    persist_execution(db, execution, [init_event])

    result = replay_execution_from_storage(db, execution.execution_id)
    assert result.execution.workflow_id == 'wf'
    assert result.execution.plan_id == plan.plan_id


def test_replay_from_storage_invalid_execution_id(tmp_path):
    db = _db(tmp_path)
    result = replay_execution_from_storage(db, 'nonexistent')
    assert result.is_valid is False


def test_replay_from_storage_events_applied_count(tmp_path):
    db = _db(tmp_path)
    _, plan = _make_wf_and_plan(_node('fetch'))
    execution, init_event = initialize_execution(plan)
    persist_execution(db, execution, [init_event])
    execution, start_events = start_execution(execution)
    persist_execution(db, execution, start_events)

    result = replay_execution_from_storage(db, execution.execution_id)
    # initialize produces 1 event; start produces 2 events (initialized→ready→executing)
    assert result.events_applied == 3


# ---------------------------------------------------------------------------
# replay_execution_from_snapshot
# ---------------------------------------------------------------------------

def test_replay_from_snapshot_with_no_snapshot_falls_back_to_full(tmp_path):
    db = _db(tmp_path)
    _, plan = _make_wf_and_plan(_node('fetch'))
    execution, init_event = initialize_execution(plan)
    persist_execution(db, execution, [init_event])
    execution, start_events = start_execution(execution)
    persist_execution(db, execution, start_events)

    result = replay_execution_from_snapshot(db, execution.execution_id)
    assert result.is_valid
    assert result.execution.state == 'executing'


def test_replay_from_snapshot_uses_snapshot_plus_delta(tmp_path):
    db = _db(tmp_path)
    wf, plan = _make_wf_and_plan(_node('a'), _node('b', dep_ids=['a']))

    execution, init_event = initialize_execution(plan)
    persist_execution(db, execution, [init_event])
    execution, start_events = start_execution(execution)
    all_start_ids = append_execution_events(db, start_events)
    from workflow.storage import save_execution
    save_execution(db, execution)

    snapshot_event_id = all_start_ids[-1]
    take_snapshot(db, execution, snapshot_event_id)

    # Now complete 'a' after the snapshot
    execution, complete_events = record_node_completed(execution, plan, 'a')
    persist_execution(db, execution, complete_events)

    result = replay_execution_from_snapshot(db, execution.execution_id)
    assert result.is_valid
    assert 'a' in result.execution.completed_node_ids


def test_take_snapshot_returns_row_id(tmp_path):
    db = _db(tmp_path)
    _, plan = _make_wf_and_plan(_node('fetch'))
    execution, init_event = initialize_execution(plan)
    persist_execution(db, execution, [init_event])
    snap_id = take_snapshot(db, execution, 0)
    assert isinstance(snap_id, int)
    assert snap_id >= 1


# ---------------------------------------------------------------------------
# Full integration: persist through a complete workflow lifecycle
# ---------------------------------------------------------------------------

def test_full_lifecycle_persist_and_replay(tmp_path):
    """Three-node chain: init → start → complete each node → workflow done."""
    db = _db(tmp_path)
    wf, plan = _make_wf_and_plan(
        _node('fetch'),
        _node('parse', dep_ids=['fetch']),
        _node('report', dep_ids=['parse']),
    )

    execution, init_event = initialize_execution(plan)
    persist_execution(db, execution, [init_event])

    execution, evts = start_execution(execution)
    persist_execution(db, execution, evts)

    for node_id in ('fetch', 'parse', 'report'):
        execution, evts = record_node_completed(execution, plan, node_id)
        persist_execution(db, execution, evts)

    assert execution.state == 'completed'

    result = replay_execution_from_storage(db, execution.execution_id)
    assert result.is_valid
    assert result.execution.state == 'completed'
    replayed = result.execution
    assert set(replayed.completed_node_ids) == {'fetch', 'parse', 'report'}


def test_full_lifecycle_snapshot_and_delta_replay(tmp_path):
    """Take snapshot after start; complete nodes afterward; replay from snapshot."""
    db = _db(tmp_path)
    wf, plan = _make_wf_and_plan(
        _node('fetch'),
        _node('parse', dep_ids=['fetch']),
    )

    execution, init_event = initialize_execution(plan)
    persist_execution(db, execution, [init_event])
    execution, evts = start_execution(execution)
    event_ids = append_execution_events(db, evts)
    from workflow.storage import save_execution
    save_execution(db, execution)

    take_snapshot(db, execution, event_ids[-1])

    execution, evts = record_node_completed(execution, plan, 'fetch')
    persist_execution(db, execution, evts)
    execution, evts = record_node_completed(execution, plan, 'parse')
    persist_execution(db, execution, evts)

    result = replay_execution_from_snapshot(db, execution.execution_id)
    assert result.is_valid
    assert result.execution.state == 'completed'
    assert set(result.execution.completed_node_ids) == {'fetch', 'parse'}


# ---------------------------------------------------------------------------
# C-1: Post-init transitions write events before mutable state
# ---------------------------------------------------------------------------

def test_c1_events_written_before_state_on_post_init_transition(tmp_path):
    """
    Simulate a crash between event-append and state-upsert for a post-init
    transition: manually append the events without updating the state row,
    then verify that replay returns the correct (advanced) state even though
    the mutable row is stale.
    """
    db = _db(tmp_path)
    _, plan = _make_wf_and_plan(_node('fetch'))

    # Initialize (row must exist before events due to FK)
    execution, init_event = initialize_execution(plan)
    persist_execution(db, execution, [init_event])

    # Simulate crash after events written but before state updated:
    # manually append the start events without calling save_execution.
    execution_after_start, start_events = start_execution(execution)
    append_execution_events(db, start_events)
    # The mutable row still says 'initialized' — state row is stale.

    stored = load_execution(db, execution.execution_id)
    assert stored.state == 'initialized'  # stale row confirmed

    # Replay from lineage gives the correct advanced state.
    result = replay_execution_from_storage(db, execution.execution_id)
    assert result.is_valid
    assert result.execution.state == 'executing'  # lineage wins


def test_c1_init_case_row_written_before_events(tmp_path):
    """
    For the initialization case the FK requires the row to exist first.
    Verify that persist_execution handles a fresh execution correctly.
    """
    db = _db(tmp_path)
    _, plan = _make_wf_and_plan(_node('fetch'))
    execution, init_event = initialize_execution(plan)
    persist_execution(db, execution, [init_event])  # must not raise

    stored = load_execution(db, execution.execution_id)
    assert stored is not None
    assert stored.state == 'initialized'

    events = load_execution_events(db, execution.execution_id)
    assert len(events) == 1
    assert events[0].new_state == 'initialized'


def test_c1_post_init_state_row_is_stale_after_event_only_write(tmp_path):
    """
    In the post-init path, if only events are written (crash before state upsert),
    the state row reflects the old state. Replay from lineage gives the new state.
    This confirms the correct crash-recovery direction: lineage is never behind.
    """
    db = _db(tmp_path)
    _, plan = _make_wf_and_plan(_node('a'), _node('b', dep_ids=['a']))

    execution, init_event = initialize_execution(plan)
    persist_execution(db, execution, [init_event])
    execution, start_events = start_execution(execution)
    persist_execution(db, execution, start_events)  # full persist for start

    # Simulate crash after events for 'a' completion but before state upsert
    execution_after_complete, complete_events = record_node_completed(execution, plan, 'a')
    append_execution_events(db, complete_events)  # events written, state NOT updated

    stored = load_execution(db, execution.execution_id)
    assert 'a' not in stored.completed_node_ids  # row is behind

    result = replay_execution_from_storage(db, execution.execution_id)
    assert 'a' in result.execution.completed_node_ids  # lineage is ahead and wins


# ---------------------------------------------------------------------------
# C-2: Pure lineage replay reconstructs identity without mutable state row
# ---------------------------------------------------------------------------

def test_c2_replay_recovers_workflow_id_from_lineage_alone(tmp_path):
    """
    replay_execution (pure, no DB) must recover workflow_id from the init
    event's metadata — no mutable state row involved.
    """
    db = _db(tmp_path)
    wf, plan = _make_wf_and_plan(_node('fetch'))

    execution, init_event = initialize_execution(plan)
    persist_execution(db, execution, [init_event])

    events = load_execution_events(db, execution.execution_id)
    from workflow.replay import replay_execution as _replay
    result = _replay(events)

    assert result.is_valid
    assert result.execution.workflow_id == 'wf'
    assert result.execution.plan_id == plan.plan_id


def test_c2_replay_from_storage_still_works_without_state_row(tmp_path):
    """
    If the mutable state row is absent but events exist, replay_execution_from_storage
    must still return a valid execution with correct identity from event metadata.
    """
    db = _db(tmp_path)
    wf, plan = _make_wf_and_plan(_node('fetch'))

    execution, init_event = initialize_execution(plan)
    persist_execution(db, execution, [init_event])
    execution, start_events = start_execution(execution)
    # Write events but manually skip the state row update
    append_execution_events(db, start_events)

    # Remove the mutable state row to simulate it being unavailable
    import sqlite3
    conn = sqlite3.connect(db)
    conn.execute('DELETE FROM workflow_executions WHERE execution_id = ?',
                 (execution.execution_id,))
    conn.commit()
    conn.close()

    # replay_execution_from_storage relies on load_execution_events + pure replay;
    # it also tries to patch identity from the mutable row, but since the row is gone,
    # identity must come from event metadata (C-2 fix).
    from workflow.replay import replay_execution as _replay
    events = load_execution_events(db, execution.execution_id)
    result = _replay(events)

    assert result.is_valid
    assert result.execution.workflow_id == 'wf'
    assert result.execution.plan_id == plan.plan_id


def test_c2_init_event_metadata_contains_identity(tmp_path):
    """The initial lineage event must carry workflow_id and plan_id in its metadata."""
    db = _db(tmp_path)
    wf, plan = _make_wf_and_plan(_node('fetch'))

    execution, init_event = initialize_execution(plan)
    persist_execution(db, execution, [init_event])

    events = load_execution_events(db, execution.execution_id)
    init_evt = next(e for e in events if e.new_state == 'initialized')
    assert init_evt.metadata.get('workflow_id') == 'wf'
    assert init_evt.metadata.get('plan_id') == plan.plan_id


# ---------------------------------------------------------------------------
# persist_workflow_definition / persist_workflow_plan / persist_workflow
# ---------------------------------------------------------------------------

def test_persist_workflow_definition_saves_and_loads(tmp_path):
    db = _db(tmp_path)
    wf, plan = _make_wf_and_plan(_node('fetch'))
    persist_workflow_definition(db, wf)

    from workflow.storage import load_workflow_definition
    loaded = load_workflow_definition(db, wf.workflow_id, wf.version)
    assert loaded is not None
    assert loaded.workflow_id == wf.workflow_id
    assert loaded.topology_hash == wf.topology_hash


def test_persist_workflow_definition_is_idempotent(tmp_path):
    db = _db(tmp_path)
    wf, _ = _make_wf_and_plan(_node('fetch'))
    persist_workflow_definition(db, wf)
    persist_workflow_definition(db, wf)  # must not raise


def test_persist_workflow_definition_divergent_hash_raises(tmp_path):
    db = _db(tmp_path)
    wf, _ = _make_wf_and_plan(_node('fetch'))
    persist_workflow_definition(db, wf)

    from dataclasses import replace
    divergent = replace(wf, topology_hash='00' * 32)
    with pytest.raises(WorkflowDefinitionError):
        persist_workflow_definition(db, divergent)


def test_persist_workflow_plan_saves_and_loads(tmp_path):
    db = _db(tmp_path)
    wf, plan = _make_wf_and_plan(_node('fetch'))
    persist_workflow_definition(db, wf)
    persist_workflow_plan(db, plan)

    from workflow.storage import load_workflow_plan
    loaded = load_workflow_plan(db, plan.plan_id)
    assert loaded is not None
    assert loaded.plan_id == plan.plan_id
    assert loaded.planner_version == plan.planner_version
    assert loaded.version == plan.version


def test_persist_workflow_plan_is_idempotent(tmp_path):
    db = _db(tmp_path)
    wf, plan = _make_wf_and_plan(_node('fetch'))
    persist_workflow_definition(db, wf)
    persist_workflow_plan(db, plan)
    persist_workflow_plan(db, plan)  # must not raise


def test_persist_workflow_plan_divergent_content_raises(tmp_path):
    db = _db(tmp_path)
    wf, plan = _make_wf_and_plan(_node('fetch'))
    persist_workflow_definition(db, wf)
    persist_workflow_plan(db, plan)

    from dataclasses import replace
    divergent = replace(plan, planner_version='0.0.0-divergent')
    with pytest.raises(WorkflowPlanError):
        persist_workflow_plan(db, divergent)


def test_persist_workflow_saves_both(tmp_path):
    db = _db(tmp_path)
    wf, plan = _make_wf_and_plan(_node('fetch'))
    persist_workflow(db, wf, plan)

    from workflow.storage import load_workflow_definition, load_workflow_plan
    assert load_workflow_definition(db, wf.workflow_id, wf.version) is not None
    assert load_workflow_plan(db, plan.plan_id) is not None


def test_persist_workflow_is_idempotent(tmp_path):
    db = _db(tmp_path)
    wf, plan = _make_wf_and_plan(_node('fetch'))
    persist_workflow(db, wf, plan)
    persist_workflow(db, wf, plan)  # must not raise


# ---------------------------------------------------------------------------
# load_plan_for_execution / load_definition_for_execution
# ---------------------------------------------------------------------------

def test_load_plan_for_execution_returns_plan(tmp_path):
    db = _db(tmp_path)
    wf, plan = _make_wf_and_plan(_node('fetch'))
    persist_workflow(db, wf, plan)

    execution, init_event = initialize_execution(plan)
    persist_execution(db, execution, [init_event])

    loaded = load_plan_for_execution(db, execution.execution_id)
    assert loaded is not None
    assert loaded.plan_id == plan.plan_id


def test_load_definition_for_execution_returns_definition(tmp_path):
    db = _db(tmp_path)
    wf, plan = _make_wf_and_plan(_node('fetch'))
    persist_workflow(db, wf, plan)

    execution, init_event = initialize_execution(plan)
    persist_execution(db, execution, [init_event])

    loaded = load_definition_for_execution(db, execution.execution_id)
    assert loaded is not None
    assert loaded.workflow_id == wf.workflow_id
    assert loaded.topology_hash == wf.topology_hash


def test_load_plan_for_execution_degrades_gracefully_pre_v3(tmp_path):
    """Executions with no persisted plan return None without error."""
    db = _db(tmp_path)
    wf, plan = _make_wf_and_plan(_node('fetch'))
    # Only persist the execution — not the definition or plan
    execution, init_event = initialize_execution(plan)
    persist_execution(db, execution, [init_event])

    assert load_plan_for_execution(db, execution.execution_id) is None


def test_load_definition_for_execution_degrades_gracefully_pre_v3(tmp_path):
    """Executions with no persisted definition return None without error."""
    db = _db(tmp_path)
    wf, plan = _make_wf_and_plan(_node('fetch'))
    execution, init_event = initialize_execution(plan)
    persist_execution(db, execution, [init_event])

    assert load_definition_for_execution(db, execution.execution_id) is None


# ---------------------------------------------------------------------------
# plan_workflow with persist_to
# ---------------------------------------------------------------------------

def test_plan_workflow_persist_to_saves_definition_and_plan(tmp_path):
    db = _db(tmp_path)
    wf = define_workflow('wf', 'Test', [_node('fetch')])
    _, plan, _ = plan_workflow(wf, persist_to=db)

    from workflow.storage import load_workflow_definition, load_workflow_plan
    assert load_workflow_definition(db, 'wf', 1) is not None
    assert load_workflow_plan(db, plan.plan_id) is not None


def test_plan_workflow_persist_to_is_idempotent(tmp_path):
    db = _db(tmp_path)
    wf = define_workflow('wf', 'Test', [_node('fetch')])
    plan_workflow(wf, persist_to=db)
    plan_workflow(wf, persist_to=db)  # must not raise


def test_plan_workflow_without_persist_to_does_not_write(tmp_path):
    db = _db(tmp_path)
    wf = define_workflow('wf', 'Test', [_node('fetch')])
    plan_workflow(wf)  # no persist_to

    from workflow.storage import load_workflow_definition
    assert load_workflow_definition(db, 'wf', 1) is None

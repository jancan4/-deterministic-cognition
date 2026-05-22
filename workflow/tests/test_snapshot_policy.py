"""Tests for workflow/snapshot_policy.py."""
import pytest

from workflow.executor import initialize_execution, record_node_completed, start_execution
from workflow.models import RetryPolicy, WorkflowNode
from workflow.persistence import persist_execution
from workflow.service import define_workflow, plan_workflow
from workflow.snapshot_policy import SnapshotPolicy, apply_snapshot_policy, should_snapshot
from workflow.storage import init_db, load_execution_events, load_latest_snapshot


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


def _make_plan(*nodes):
    wf = define_workflow('wf', 'Test', list(nodes))
    vr, plan, _ = plan_workflow(wf)
    assert vr.valid, vr.errors
    return plan


def _init_and_start(db, plan):
    execution, init_event = initialize_execution(plan)
    persist_execution(db, execution, [init_event])
    execution, start_events = start_execution(execution)
    persist_execution(db, execution, start_events)
    return execution


# ---------------------------------------------------------------------------
# should_snapshot — pure function, no I/O
# ---------------------------------------------------------------------------

def test_should_snapshot_stage_advance_triggers():
    policy = SnapshotPolicy(events_per_snapshot=100, snapshot_on_stage_advance=True)
    assert should_snapshot(policy, events_since_last_snapshot=0, stage_just_advanced=True)


def test_should_snapshot_stage_advance_disabled():
    policy = SnapshotPolicy(events_per_snapshot=100, snapshot_on_stage_advance=False)
    assert not should_snapshot(policy, events_since_last_snapshot=0, stage_just_advanced=True)


def test_should_snapshot_event_count_trigger():
    policy = SnapshotPolicy(events_per_snapshot=5, snapshot_on_stage_advance=False)
    assert not should_snapshot(policy, events_since_last_snapshot=4)
    assert should_snapshot(policy, events_since_last_snapshot=5)
    assert should_snapshot(policy, events_since_last_snapshot=6)


def test_should_snapshot_event_count_zero_disables():
    policy = SnapshotPolicy(events_per_snapshot=0, snapshot_on_stage_advance=False)
    assert not should_snapshot(policy, events_since_last_snapshot=999)


def test_should_snapshot_both_triggers_either_fires():
    policy = SnapshotPolicy(events_per_snapshot=5, snapshot_on_stage_advance=True)
    assert should_snapshot(policy, events_since_last_snapshot=1, stage_just_advanced=True)
    assert should_snapshot(policy, events_since_last_snapshot=5, stage_just_advanced=False)
    assert not should_snapshot(policy, events_since_last_snapshot=4, stage_just_advanced=False)


def test_should_snapshot_both_disabled():
    policy = SnapshotPolicy(events_per_snapshot=0, snapshot_on_stage_advance=False)
    assert not should_snapshot(policy, events_since_last_snapshot=999, stage_just_advanced=True)


# ---------------------------------------------------------------------------
# apply_snapshot_policy — integration with DB
# ---------------------------------------------------------------------------

def test_apply_snapshot_policy_returns_none_when_no_trigger(tmp_path):
    db = _db(tmp_path)
    plan = _make_plan(_node('a'))
    execution = _init_and_start(db, plan)
    events = load_execution_events(db, execution.execution_id)
    event_ids = list(range(1, len(events) + 1))

    policy = SnapshotPolicy(events_per_snapshot=100, snapshot_on_stage_advance=False)
    result = apply_snapshot_policy(db, policy, execution, event_ids, events)
    assert result is None


def test_apply_snapshot_policy_stage_advance_takes_snapshot(tmp_path):
    db = _db(tmp_path)
    plan = _make_plan(_node('a'))
    execution = _init_and_start(db, plan)
    execution, complete_events = record_node_completed(execution, plan, 'a')
    persist_execution(db, execution, complete_events)

    events = load_execution_events(db, execution.execution_id)

    from workflow.state import EVENT_STAGE_ADVANCED
    stage_events = [e for e in events if e.event_type == EVENT_STAGE_ADVANCED]

    policy = SnapshotPolicy(events_per_snapshot=100, snapshot_on_stage_advance=True)
    result = apply_snapshot_policy(
        db, policy, execution,
        new_event_ids=[1],
        new_events=stage_events,
    )
    assert result is not None
    snapshot = load_latest_snapshot(db, execution.execution_id)
    assert snapshot is not None


def test_apply_snapshot_policy_n_events_takes_snapshot(tmp_path):
    db = _db(tmp_path)
    plan = _make_plan(_node('a'))
    execution = _init_and_start(db, plan)
    events = load_execution_events(db, execution.execution_id)

    policy = SnapshotPolicy(events_per_snapshot=1, snapshot_on_stage_advance=False)
    result = apply_snapshot_policy(
        db, policy, execution,
        new_event_ids=[1],
        new_events=events,
        last_snapshot_event_id=0,
    )
    assert result is not None


def test_apply_snapshot_policy_empty_event_ids_returns_none(tmp_path):
    db = _db(tmp_path)
    plan = _make_plan(_node('a'))
    execution = _init_and_start(db, plan)
    events = load_execution_events(db, execution.execution_id)

    policy = SnapshotPolicy(events_per_snapshot=1, snapshot_on_stage_advance=True)
    result = apply_snapshot_policy(
        db, policy, execution,
        new_event_ids=[],
        new_events=events,
    )
    assert result is None


def test_apply_snapshot_policy_respects_last_snapshot_event_id(tmp_path):
    db = _db(tmp_path)
    plan = _make_plan(_node('a'))
    execution = _init_and_start(db, plan)
    events = load_execution_events(db, execution.execution_id)
    n = len(events)

    policy = SnapshotPolicy(events_per_snapshot=n + 10, snapshot_on_stage_advance=False)
    result = apply_snapshot_policy(
        db, policy, execution,
        new_event_ids=[1],
        new_events=events,
        last_snapshot_event_id=0,
    )
    assert result is None

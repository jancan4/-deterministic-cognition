"""Tests for workflow/recovery.py."""
import pytest

from workflow.executor import initialize_execution, record_node_completed, start_execution
from workflow.models import RetryPolicy, WorkflowNode
from workflow.persistence import persist_execution
from workflow.recovery import (
    RecoveryReport,
    apply_recovery,
    find_non_terminal_execution_ids,
    recover_all,
    recover_execution,
)
from workflow.service import define_workflow, plan_workflow
from workflow.storage import init_db, load_execution, save_execution


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
# find_non_terminal_execution_ids
# ---------------------------------------------------------------------------

def test_find_non_terminal_empty_db(tmp_path):
    db = _db(tmp_path)
    assert find_non_terminal_execution_ids(db) == []


def test_find_non_terminal_returns_executing(tmp_path):
    db = _db(tmp_path)
    plan = _make_plan(_node('a'))
    execution = _init_and_start(db, plan)
    ids = find_non_terminal_execution_ids(db)
    assert execution.execution_id in ids


def test_find_non_terminal_excludes_completed(tmp_path):
    db = _db(tmp_path)
    plan = _make_plan(_node('a'))
    execution = _init_and_start(db, plan)
    execution, complete_events = record_node_completed(execution, plan, 'a')
    persist_execution(db, execution, complete_events)
    assert execution.state == 'completed'
    ids = find_non_terminal_execution_ids(db)
    assert execution.execution_id not in ids


def test_find_non_terminal_multiple_executions(tmp_path):
    db = _db(tmp_path)
    wf1 = define_workflow('wf-alpha', 'Alpha', [_node('a')])
    vr1, plan1, _ = plan_workflow(wf1)
    assert vr1.valid
    wf2 = define_workflow('wf-beta', 'Beta', [_node('b')])
    vr2, plan2, _ = plan_workflow(wf2)
    assert vr2.valid
    exec1 = _init_and_start(db, plan1)
    exec2 = _init_and_start(db, plan2)

    exec1, done_events = record_node_completed(exec1, plan1, 'a')
    persist_execution(db, exec1, done_events)

    ids = find_non_terminal_execution_ids(db)
    assert exec1.execution_id not in ids
    assert exec2.execution_id in ids


# ---------------------------------------------------------------------------
# recover_execution (dry run)
# ---------------------------------------------------------------------------

def test_recover_execution_consistent(tmp_path):
    db = _db(tmp_path)
    plan = _make_plan(_node('a'))
    execution = _init_and_start(db, plan)
    report = recover_execution(db, execution.execution_id)

    assert isinstance(report, RecoveryReport)
    assert report.execution_id == execution.execution_id
    assert report.replayed_state == execution.state
    assert report.lineage_valid
    assert report.is_recoverable
    assert not report.diverged


def test_recover_execution_detects_divergence(tmp_path):
    db = _db(tmp_path)
    plan = _make_plan(_node('a'))
    execution = _init_and_start(db, plan)

    # Corrupt the mutable row — advance stage without lineage event
    corrupted = execution.__class__(
        execution_id=execution.execution_id,
        workflow_id=execution.workflow_id,
        plan_id=execution.plan_id,
        state='completed',
        active_stage_index=execution.active_stage_index + 5,
        completed_node_ids=['a', 'b', 'fake'],
        failed_node_ids=[],
        node_attempts={},
        version=execution.version + 99,
        created_at=execution.created_at,
        updated_at=execution.updated_at,
    )
    save_execution(db, corrupted)

    report = recover_execution(db, execution.execution_id)
    assert report.diverged
    assert len(report.divergence_details) > 0


def test_recover_execution_no_lineage(tmp_path):
    db = _db(tmp_path)
    plan = _make_plan(_node('a'))
    execution, init_event = initialize_execution(plan)
    # Save execution row but no events
    save_execution(db, execution)

    report = recover_execution(db, execution.execution_id)
    assert not report.is_recoverable
    assert report.replayed_state is None
    assert report.diverged


def test_recover_execution_does_not_mutate(tmp_path):
    db = _db(tmp_path)
    plan = _make_plan(_node('a'))
    execution = _init_and_start(db, plan)

    corrupted = execution.__class__(
        execution_id=execution.execution_id,
        workflow_id=execution.workflow_id,
        plan_id=execution.plan_id,
        state='blocked',
        active_stage_index=execution.active_stage_index,
        completed_node_ids=[],
        failed_node_ids=[],
        node_attempts={},
        version=execution.version + 1,
        created_at=execution.created_at,
        updated_at=execution.updated_at,
    )
    save_execution(db, corrupted)

    recover_execution(db, execution.execution_id)
    stored_after = load_execution(db, execution.execution_id)
    assert stored_after.state == 'blocked'  # not mutated


# ---------------------------------------------------------------------------
# apply_recovery
# ---------------------------------------------------------------------------

def test_apply_recovery_writes_back_replayed_state(tmp_path):
    db = _db(tmp_path)
    plan = _make_plan(_node('a'))
    execution = _init_and_start(db, plan)

    corrupted = execution.__class__(
        execution_id=execution.execution_id,
        workflow_id=execution.workflow_id,
        plan_id=execution.plan_id,
        state='blocked',
        active_stage_index=execution.active_stage_index,
        completed_node_ids=[],
        failed_node_ids=[],
        node_attempts={},
        version=execution.version + 1,
        created_at=execution.created_at,
        updated_at=execution.updated_at,
    )
    save_execution(db, corrupted)

    report = apply_recovery(db, execution.execution_id)
    assert report.is_recoverable

    restored = load_execution(db, execution.execution_id)
    assert restored.state == execution.state


def test_apply_recovery_skips_if_not_recoverable(tmp_path):
    db = _db(tmp_path)
    plan = _make_plan(_node('a'))
    execution, _ = initialize_execution(plan)
    save_execution(db, execution)  # row but no events

    report = apply_recovery(db, execution.execution_id)
    assert not report.is_recoverable

    stored = load_execution(db, execution.execution_id)
    assert stored.state == execution.state  # unchanged


# ---------------------------------------------------------------------------
# recover_all
# ---------------------------------------------------------------------------

def test_recover_all_dry_run(tmp_path):
    db = _db(tmp_path)
    plan = _make_plan(_node('a'))
    _init_and_start(db, plan)

    reports = recover_all(db, apply=False)
    assert len(reports) == 1
    assert reports[0].is_recoverable


def test_recover_all_apply(tmp_path):
    db = _db(tmp_path)
    plan = _make_plan(_node('a'))
    execution = _init_and_start(db, plan)

    corrupted = execution.__class__(
        execution_id=execution.execution_id,
        workflow_id=execution.workflow_id,
        plan_id=execution.plan_id,
        state='paused',
        active_stage_index=execution.active_stage_index,
        completed_node_ids=[],
        failed_node_ids=[],
        node_attempts={},
        version=execution.version + 1,
        created_at=execution.created_at,
        updated_at=execution.updated_at,
    )
    save_execution(db, corrupted)

    reports = recover_all(db, apply=True)
    assert len(reports) == 1
    restored = load_execution(db, execution.execution_id)
    assert restored.state == execution.state


def test_recover_all_excludes_terminal(tmp_path):
    db = _db(tmp_path)
    plan = _make_plan(_node('a'))
    execution = _init_and_start(db, plan)
    execution, done_events = record_node_completed(execution, plan, 'a')
    persist_execution(db, execution, done_events)

    reports = recover_all(db, apply=False)
    assert reports == []


def test_recover_all_empty_db(tmp_path):
    db = _db(tmp_path)
    assert recover_all(db) == []

"""Tests for workflow SQLite storage layer."""
import pytest

from workflow.state import (
    EVENT_NODE_COMPLETED,
    EVENT_NODE_SUBMITTED,
    EVENT_STATE_TRANSITION,
    WorkflowExecution,
    WorkflowExecutionLineageEvent,
)
from workflow.storage import (
    append_execution_event,
    append_execution_events,
    init_db,
    load_execution,
    load_execution_events,
    load_latest_snapshot,
    persist_snapshot,
    save_execution,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _db(tmp_path) -> str:
    path = str(tmp_path / 'wf.db')
    init_db(path)
    return path


def _execution(**kwargs) -> WorkflowExecution:
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
        version=1,
    )
    defaults.update(kwargs)
    return WorkflowExecution(**defaults)


def _event(event_type=EVENT_STATE_TRANSITION, execution_id='eid-1', **kwargs) -> WorkflowExecutionLineageEvent:
    defaults = dict(
        execution_id=execution_id,
        event_type=event_type,
        old_state=None,
        new_state='initialized',
        node_id=None,
        stage_index=0,
        reason='test',
        created_at='2026-01-01T00:00:00Z',
    )
    defaults.update(kwargs)
    return WorkflowExecutionLineageEvent(**defaults)


# ---------------------------------------------------------------------------
# init_db
# ---------------------------------------------------------------------------

def test_init_db_creates_tables(tmp_path):
    import sqlite3
    db = _db(tmp_path)
    conn = sqlite3.connect(db)
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    conn.close()
    assert 'workflow_executions' in tables
    assert 'workflow_execution_events' in tables
    assert 'workflow_snapshots' in tables


def test_init_db_is_idempotent(tmp_path):
    db = str(tmp_path / 'wf.db')
    init_db(db)
    init_db(db)  # second call must not raise


def test_init_db_sets_schema_version(tmp_path):
    import sqlite3
    db = _db(tmp_path)
    conn = sqlite3.connect(db)
    row = conn.execute('SELECT version FROM workflow_schema_version').fetchone()
    conn.close()
    assert row is not None
    assert row[0] == 1


# ---------------------------------------------------------------------------
# save_execution / load_execution
# ---------------------------------------------------------------------------

def test_save_and_load_execution_roundtrip(tmp_path):
    db = _db(tmp_path)
    ex = _execution()
    save_execution(db, ex)
    loaded = load_execution(db, 'eid-1')
    assert loaded == ex


def test_load_execution_returns_none_for_missing(tmp_path):
    db = _db(tmp_path)
    assert load_execution(db, 'nonexistent') is None


def test_save_execution_upserts_state(tmp_path):
    db = _db(tmp_path)
    ex = _execution(state='executing')
    save_execution(db, ex)

    from dataclasses import replace
    ex2 = replace(ex, state='completed', version=2)
    save_execution(db, ex2)

    loaded = load_execution(db, 'eid-1')
    assert loaded.state == 'completed'
    assert loaded.version == 2


def test_save_execution_preserves_node_lists(tmp_path):
    db = _db(tmp_path)
    ex = _execution(
        completed_node_ids=['a', 'b'],
        failed_node_ids=['c'],
        node_attempts={'c': 2},
    )
    save_execution(db, ex)
    loaded = load_execution(db, 'eid-1')
    assert loaded.completed_node_ids == ['a', 'b']
    assert loaded.failed_node_ids == ['c']
    assert loaded.node_attempts == {'c': 2}


def test_save_execution_sorts_node_ids(tmp_path):
    db = _db(tmp_path)
    ex = _execution(completed_node_ids=['z', 'a', 'm'])
    save_execution(db, ex)
    loaded = load_execution(db, 'eid-1')
    assert loaded.completed_node_ids == ['a', 'm', 'z']


def test_save_multiple_executions(tmp_path):
    db = _db(tmp_path)
    ex1 = _execution(execution_id='eid-1')
    ex2 = _execution(execution_id='eid-2', workflow_id='wf2')
    save_execution(db, ex1)
    save_execution(db, ex2)
    assert load_execution(db, 'eid-1') is not None
    assert load_execution(db, 'eid-2') is not None


# ---------------------------------------------------------------------------
# append_execution_event / load_execution_events
# ---------------------------------------------------------------------------

def test_append_event_returns_row_id(tmp_path):
    db = _db(tmp_path)
    save_execution(db, _execution())
    row_id = append_execution_event(db, _event())
    assert isinstance(row_id, int)
    assert row_id >= 1


def test_load_events_returns_empty_when_none(tmp_path):
    db = _db(tmp_path)
    save_execution(db, _execution())
    events = load_execution_events(db, 'eid-1')
    assert events == []


def test_load_events_returns_in_insertion_order(tmp_path):
    db = _db(tmp_path)
    save_execution(db, _execution())
    e1 = _event(new_state='initialized')
    e2 = _event(event_type=EVENT_NODE_COMPLETED, node_id='fetch', new_state=None)
    append_execution_event(db, e1)
    append_execution_event(db, e2)

    events = load_execution_events(db, 'eid-1')
    assert len(events) == 2
    assert events[0].event_type == EVENT_STATE_TRANSITION
    assert events[1].event_type == EVENT_NODE_COMPLETED


def test_load_events_scoped_to_execution_id(tmp_path):
    db = _db(tmp_path)
    ex1 = _execution(execution_id='eid-1')
    ex2 = _execution(execution_id='eid-2', plan_id='plan-2')
    save_execution(db, ex1)
    save_execution(db, ex2)
    append_execution_event(db, _event(execution_id='eid-1'))
    append_execution_event(db, _event(execution_id='eid-2'))

    assert len(load_execution_events(db, 'eid-1')) == 1
    assert len(load_execution_events(db, 'eid-2')) == 1


def test_load_events_after_event_id(tmp_path):
    db = _db(tmp_path)
    save_execution(db, _execution())
    id1 = append_execution_event(db, _event())
    id2 = append_execution_event(db, _event(new_state='ready'))

    events = load_execution_events(db, 'eid-1', after_event_id=id1)
    assert len(events) == 1
    assert events[0].new_state == 'ready'


def test_append_execution_events_batch(tmp_path):
    db = _db(tmp_path)
    save_execution(db, _execution())
    evts = [
        _event(new_state='initialized'),
        _event(new_state='ready'),
        _event(new_state='executing'),
    ]
    ids = append_execution_events(db, evts)
    assert len(ids) == 3
    assert all(isinstance(i, int) for i in ids)

    loaded = load_execution_events(db, 'eid-1')
    assert len(loaded) == 3


def test_append_events_empty_list_returns_empty(tmp_path):
    db = _db(tmp_path)
    assert append_execution_events(db, []) == []


def test_event_fields_preserved(tmp_path):
    db = _db(tmp_path)
    save_execution(db, _execution())
    evt = _event(
        event_type=EVENT_NODE_SUBMITTED,
        node_id='fetch',
        stage_index=2,
        reason='submitted by coordinator',
        new_state=None,
    )
    append_execution_event(db, evt)

    loaded = load_execution_events(db, 'eid-1')
    e = loaded[0]
    assert e.event_type == EVENT_NODE_SUBMITTED
    assert e.node_id == 'fetch'
    assert e.stage_index == 2
    assert e.reason == 'submitted by coordinator'


# ---------------------------------------------------------------------------
# persist_snapshot / load_latest_snapshot
# ---------------------------------------------------------------------------

def test_persist_and_load_snapshot(tmp_path):
    db = _db(tmp_path)
    ex = _execution()
    save_execution(db, ex)
    snap_id = append_execution_event(db, _event())
    persist_snapshot(db, ex, snap_id)

    result = load_latest_snapshot(db, 'eid-1')
    assert result is not None
    snapshot, last_event_id = result
    assert snapshot == ex
    assert last_event_id == snap_id


def test_load_latest_snapshot_returns_none_when_absent(tmp_path):
    db = _db(tmp_path)
    save_execution(db, _execution())
    assert load_latest_snapshot(db, 'eid-1') is None


def test_load_latest_snapshot_returns_most_recent(tmp_path):
    db = _db(tmp_path)
    ex = _execution()
    save_execution(db, ex)
    id1 = append_execution_event(db, _event(new_state='initialized'))
    id2 = append_execution_event(db, _event(new_state='ready'))

    persist_snapshot(db, ex, id1)

    from dataclasses import replace
    ex2 = replace(ex, state='executing', version=3)
    persist_snapshot(db, ex2, id2)

    result = load_latest_snapshot(db, 'eid-1')
    assert result is not None
    snapshot, last_event_id = result
    assert snapshot.state == 'executing'
    assert last_event_id == id2


def test_snapshot_preserves_completed_node_ids(tmp_path):
    db = _db(tmp_path)
    ex = _execution(completed_node_ids=['a', 'b', 'c'])
    save_execution(db, ex)
    eid = append_execution_event(db, _event())
    persist_snapshot(db, ex, eid)

    result = load_latest_snapshot(db, 'eid-1')
    snapshot, _ = result
    assert snapshot.completed_node_ids == ['a', 'b', 'c']

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
    WorkflowDefinitionError,
    WorkflowPlanError,
    append_execution_event,
    append_execution_events,
    init_db,
    load_definition_for_execution,
    load_execution,
    load_execution_events,
    load_latest_snapshot,
    load_plan_for_execution,
    load_workflow_definition,
    load_workflow_plan,
    persist_snapshot,
    save_execution,
    save_workflow_definition,
    save_workflow_plan,
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
    assert row[0] == 3  # schema version 3 adds workflow_definitions and workflow_plans


def test_init_db_creates_definition_and_plan_tables(tmp_path):
    import sqlite3
    db = _db(tmp_path)
    conn = sqlite3.connect(db)
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    conn.close()
    assert 'workflow_definitions' in tables
    assert 'workflow_plans' in tables


def test_init_db_migrates_schema_version_from_v2(tmp_path):
    """Existing v2 DBs have their schema_version row updated to v3."""
    import sqlite3
    db = str(tmp_path / 'wf.db')
    # Manually create a v2 database
    conn = sqlite3.connect(db)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.executescript("""
        CREATE TABLE workflow_schema_version (version INTEGER NOT NULL);
        CREATE TABLE workflow_executions (
            execution_id TEXT PRIMARY KEY, workflow_id TEXT NOT NULL,
            plan_id TEXT NOT NULL, state TEXT NOT NULL, active_stage_index INTEGER NOT NULL,
            completed_node_ids_json TEXT NOT NULL DEFAULT '[]',
            failed_node_ids_json TEXT NOT NULL DEFAULT '[]',
            node_attempts_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL, version INTEGER NOT NULL
        );
        CREATE TABLE workflow_execution_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, execution_id TEXT NOT NULL,
            event_type TEXT NOT NULL, old_state TEXT, new_state TEXT, node_id TEXT,
            stage_index INTEGER NOT NULL DEFAULT 0, reason TEXT NOT NULL,
            created_at TEXT NOT NULL, metadata_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE workflow_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT, execution_id TEXT NOT NULL,
            snapshot_json TEXT NOT NULL, last_event_id INTEGER NOT NULL, created_at TEXT NOT NULL
        );
        INSERT INTO workflow_schema_version (version) VALUES (2);
    """)
    conn.commit()
    conn.close()

    init_db(db)  # should migrate v2 → v3

    conn = sqlite3.connect(db)
    row = conn.execute('SELECT version FROM workflow_schema_version').fetchone()
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    conn.close()
    assert row[0] == 3
    assert 'workflow_definitions' in tables
    assert 'workflow_plans' in tables


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


def test_event_metadata_roundtrip(tmp_path):
    """metadata dict is serialized to metadata_json and restored on load."""
    db = _db(tmp_path)
    save_execution(db, _execution())
    evt = _event(
        new_state='initialized',
        metadata={'workflow_id': 'wf-42', 'plan_id': 'plan-99'},
    )
    append_execution_event(db, evt)

    loaded = load_execution_events(db, 'eid-1')
    assert loaded[0].metadata == {'workflow_id': 'wf-42', 'plan_id': 'plan-99'}


def test_event_metadata_defaults_to_empty_dict(tmp_path):
    """Events without explicit metadata load as empty dict."""
    db = _db(tmp_path)
    save_execution(db, _execution())
    append_execution_event(db, _event())  # no metadata kwarg

    loaded = load_execution_events(db, 'eid-1')
    assert loaded[0].metadata == {}


def test_event_metadata_preserved_in_batch_append(tmp_path):
    """metadata is correctly persisted for each event in a batch."""
    db = _db(tmp_path)
    save_execution(db, _execution())
    evts = [
        _event(new_state='initialized', metadata={'workflow_id': 'wf-1', 'plan_id': 'p-1'}),
        _event(new_state='ready', metadata={}),
        _event(new_state='executing', metadata={'extra': 'data'}),
    ]
    append_execution_events(db, evts)

    loaded = load_execution_events(db, 'eid-1')
    assert loaded[0].metadata == {'workflow_id': 'wf-1', 'plan_id': 'p-1'}
    assert loaded[1].metadata == {}
    assert loaded[2].metadata == {'extra': 'data'}


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


# ---------------------------------------------------------------------------
# save_workflow_definition / load_workflow_definition
# ---------------------------------------------------------------------------

def _definition(workflow_id='wf', version=1, name='Test WF'):
    from workflow.service import define_workflow
    from workflow.models import WorkflowNode
    nodes = [WorkflowNode(node_id='fetch', task_type='research', dependency_ids=[])]
    return define_workflow(workflow_id, name, nodes, version=version)


def _plan(definition):
    from workflow.service import plan_workflow
    _, plan, _ = plan_workflow(definition)
    return plan


def test_save_and_load_workflow_definition_roundtrip(tmp_path):
    db = _db(tmp_path)
    defn = _definition()
    save_workflow_definition(db, defn)
    loaded = load_workflow_definition(db, 'wf', 1)
    assert loaded is not None
    assert loaded.workflow_id == 'wf'
    assert loaded.version == 1
    assert loaded.topology_hash == defn.topology_hash


def test_load_workflow_definition_returns_none_when_absent(tmp_path):
    db = _db(tmp_path)
    assert load_workflow_definition(db, 'nonexistent', 1) is None


def test_save_workflow_definition_is_idempotent(tmp_path):
    db = _db(tmp_path)
    defn = _definition()
    save_workflow_definition(db, defn)
    save_workflow_definition(db, defn)  # must not raise
    loaded = load_workflow_definition(db, 'wf', 1)
    assert loaded is not None


def test_save_workflow_definition_divergent_topology_hash_raises(tmp_path):
    """Same (workflow_id, version) with different topology_hash must raise."""
    import pytest
    db = _db(tmp_path)
    defn = _definition()
    save_workflow_definition(db, defn)

    # Manufacture a divergent definition with the same id/version but different hash
    from dataclasses import replace
    divergent = replace(defn, topology_hash='deadbeef' * 8)
    with pytest.raises(WorkflowDefinitionError):
        save_workflow_definition(db, divergent)


def test_save_workflow_definition_different_versions_coexist(tmp_path):
    db = _db(tmp_path)
    defn_v1 = _definition(version=1)
    defn_v2 = _definition(version=2)
    save_workflow_definition(db, defn_v1)
    save_workflow_definition(db, defn_v2)
    assert load_workflow_definition(db, 'wf', 1) is not None
    assert load_workflow_definition(db, 'wf', 2) is not None


# ---------------------------------------------------------------------------
# save_workflow_plan / load_workflow_plan
# ---------------------------------------------------------------------------

def test_save_and_load_workflow_plan_roundtrip(tmp_path):
    db = _db(tmp_path)
    defn = _definition()
    save_workflow_definition(db, defn)
    plan = _plan(defn)
    save_workflow_plan(db, plan)
    loaded = load_workflow_plan(db, plan.plan_id)
    assert loaded is not None
    assert loaded.plan_id == plan.plan_id
    assert loaded.workflow_id == plan.workflow_id
    assert loaded.version == plan.version
    assert loaded.planner_version == plan.planner_version


def test_load_workflow_plan_returns_none_when_absent(tmp_path):
    db = _db(tmp_path)
    assert load_workflow_plan(db, 'nonexistent-plan-id') is None


def test_save_workflow_plan_is_idempotent(tmp_path):
    db = _db(tmp_path)
    defn = _definition()
    save_workflow_definition(db, defn)
    plan = _plan(defn)
    save_workflow_plan(db, plan)
    save_workflow_plan(db, plan)  # must not raise


def test_save_workflow_plan_divergent_content_raises(tmp_path):
    """Same plan_id with different plan_json must raise WorkflowPlanError."""
    import pytest
    db = _db(tmp_path)
    defn = _definition()
    save_workflow_definition(db, defn)
    plan = _plan(defn)
    save_workflow_plan(db, plan)

    from dataclasses import replace
    divergent = replace(plan, planner_version='divergent-version')
    with pytest.raises(WorkflowPlanError):
        save_workflow_plan(db, divergent)


def test_workflow_plan_planner_version_is_indexed(tmp_path):
    """planner_version column is indexed — verify the index exists."""
    import sqlite3
    db = _db(tmp_path)
    conn = sqlite3.connect(db)
    indices = {r[1] for r in conn.execute(
        "SELECT * FROM sqlite_master WHERE type='index'"
    ).fetchall()}
    conn.close()
    assert 'idx_workflow_plans_planner_version' in indices


# ---------------------------------------------------------------------------
# load_plan_for_execution / load_definition_for_execution
# ---------------------------------------------------------------------------

def test_load_plan_for_execution_returns_plan(tmp_path):
    db = _db(tmp_path)
    defn = _definition()
    plan = _plan(defn)
    save_workflow_definition(db, defn)
    save_workflow_plan(db, plan)
    ex = _execution(plan_id=plan.plan_id)
    save_execution(db, ex)

    loaded = load_plan_for_execution(db, 'eid-1')
    assert loaded is not None
    assert loaded.plan_id == plan.plan_id


def test_load_plan_for_execution_returns_none_when_execution_absent(tmp_path):
    db = _db(tmp_path)
    assert load_plan_for_execution(db, 'nonexistent') is None


def test_load_plan_for_execution_returns_none_when_plan_absent(tmp_path):
    db = _db(tmp_path)
    ex = _execution(plan_id='no-such-plan')
    save_execution(db, ex)
    assert load_plan_for_execution(db, 'eid-1') is None


def test_load_definition_for_execution_returns_definition(tmp_path):
    db = _db(tmp_path)
    defn = _definition()
    plan = _plan(defn)
    save_workflow_definition(db, defn)
    save_workflow_plan(db, plan)
    ex = _execution(plan_id=plan.plan_id)
    save_execution(db, ex)

    loaded = load_definition_for_execution(db, 'eid-1')
    assert loaded is not None
    assert loaded.workflow_id == 'wf'
    assert loaded.topology_hash == defn.topology_hash


def test_load_definition_for_execution_returns_none_for_pre_v3_execution(tmp_path):
    """Executions with no persisted plan/definition degrade gracefully."""
    db = _db(tmp_path)
    ex = _execution(plan_id='pre-v3-plan-id')
    save_execution(db, ex)
    assert load_definition_for_execution(db, 'eid-1') is None

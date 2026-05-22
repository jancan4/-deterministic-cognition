"""
Hermetic integration tests for orchestration/service.py.

All tests use tmp_path and leave no persistent state.
"""

import json
import pytest
from orchestration import service
from orchestration.service import NotFoundError, ValidationError
from orchestration.transitions import TransitionError


def _create(db, **kw):
    defaults = dict(
        title='Test task',
        task_type='analysis',
        actor='tester',
    )
    defaults.update(kw)
    return service.create_task(db, **defaults)


# ---------------------------------------------------------------------------
# init_db
# ---------------------------------------------------------------------------

class TestInitDb:
    def test_creates_tables(self, tmp_path):
        import sqlite3
        db = str(tmp_path / 't.db')
        service.init_db(db)
        conn = sqlite3.connect(db)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        conn.close()
        assert {'tasks', 'task_lineage', 'task_dependencies'}.issubset(tables)

    def test_idempotent(self, tmp_path):
        db = str(tmp_path / 't.db')
        service.init_db(db)
        service.init_db(db)  # must not raise


# ---------------------------------------------------------------------------
# create_task
# ---------------------------------------------------------------------------

class TestCreateTask:
    def test_creates_in_pending_state(self, tmp_path):
        db = str(tmp_path / 't.db')
        service.init_db(db)
        task = _create(db)
        assert task.state == 'pending'

    def test_returns_task_with_id(self, tmp_path):
        db = str(tmp_path / 't.db')
        service.init_db(db)
        task = _create(db)
        assert task.id == 1

    def test_version_starts_at_1(self, tmp_path):
        db = str(tmp_path / 't.db')
        service.init_db(db)
        task = _create(db)
        assert task.version == 1

    def test_stores_title_and_actor(self, tmp_path):
        db = str(tmp_path / 't.db')
        service.init_db(db)
        task = _create(db, title='My Task', actor='quant')
        assert task.title == 'My Task'
        assert task.actor == 'quant'

    def test_stores_description(self, tmp_path):
        db = str(tmp_path / 't.db')
        service.init_db(db)
        task = _create(db, description='Detailed description')
        assert task.description == 'Detailed description'

    def test_stores_tags_sorted(self, tmp_path):
        db = str(tmp_path / 't.db')
        service.init_db(db)
        task = _create(db, tags=['z', 'a', 'm'])
        assert task.tags == ['a', 'm', 'z']

    def test_stores_metadata(self, tmp_path):
        db = str(tmp_path / 't.db')
        service.init_db(db)
        task = _create(db, metadata={'run_id': 42})
        assert task.metadata == {'run_id': 42}

    def test_default_priority_3(self, tmp_path):
        db = str(tmp_path / 't.db')
        service.init_db(db)
        task = _create(db)
        assert task.priority == 3

    def test_custom_priority(self, tmp_path):
        db = str(tmp_path / 't.db')
        service.init_db(db)
        task = _create(db, priority=1)
        assert task.priority == 1

    def test_creates_initial_lineage_event(self, tmp_path):
        db = str(tmp_path / 't.db')
        service.init_db(db)
        task = _create(db)
        lineage = service.get_lineage(db, task.id)
        assert len(lineage) == 1
        assert lineage[0].old_state is None
        assert lineage[0].new_state == 'pending'

    def test_lineage_reason_captured(self, tmp_path):
        db = str(tmp_path / 't.db')
        service.init_db(db)
        task = _create(db, reason='Initial research task')
        lineage = service.get_lineage(db, task.id)
        assert lineage[0].reason == 'Initial research task'

    def test_empty_title_rejected(self, tmp_path):
        db = str(tmp_path / 't.db')
        service.init_db(db)
        with pytest.raises(ValidationError):
            _create(db, title='')

    def test_invalid_task_type_rejected(self, tmp_path):
        db = str(tmp_path / 't.db')
        service.init_db(db)
        with pytest.raises(ValidationError):
            _create(db, task_type='nonexistent')

    def test_empty_actor_rejected(self, tmp_path):
        db = str(tmp_path / 't.db')
        service.init_db(db)
        with pytest.raises(ValidationError):
            _create(db, actor='')

    def test_priority_out_of_range_rejected(self, tmp_path):
        db = str(tmp_path / 't.db')
        service.init_db(db)
        with pytest.raises(ValidationError):
            _create(db, priority=6)
        with pytest.raises(ValidationError):
            _create(db, priority=0)

    def test_priority_bool_rejected(self, tmp_path):
        db = str(tmp_path / 't.db')
        service.init_db(db)
        with pytest.raises(ValidationError):
            _create(db, priority=True)

    def test_all_valid_task_types(self, tmp_path):
        from orchestration.models import VALID_TASK_TYPES
        db = str(tmp_path / 't.db')
        service.init_db(db)
        for ttype in VALID_TASK_TYPES:
            task = _create(db, task_type=ttype)
            assert task.task_type == ttype


# ---------------------------------------------------------------------------
# get_task
# ---------------------------------------------------------------------------

class TestGetTask:
    def test_returns_task_lineage_deps(self, tmp_path):
        db = str(tmp_path / 't.db')
        service.init_db(db)
        t = _create(db)
        task, lineage, deps = service.get_task(db, t.id)
        assert task.id == t.id
        assert len(lineage) >= 1
        assert isinstance(deps, list)

    def test_not_found_raises(self, tmp_path):
        db = str(tmp_path / 't.db')
        service.init_db(db)
        with pytest.raises(NotFoundError):
            service.get_task(db, 9999)

    def test_lineage_ordered_by_id(self, tmp_path):
        db = str(tmp_path / 't.db')
        service.init_db(db)
        t = _create(db)
        service.transition_task(db, t.id, 'ready', reason='ok', actor='u')
        service.transition_task(db, t.id, 'running', reason='ok', actor='u')
        _, lineage, _ = service.get_task(db, t.id)
        ids = [ev.id for ev in lineage]
        assert ids == sorted(ids)


# ---------------------------------------------------------------------------
# list_tasks
# ---------------------------------------------------------------------------

class TestListTasks:
    def test_returns_all_tasks(self, tmp_path):
        db = str(tmp_path / 't.db')
        service.init_db(db)
        _create(db)
        _create(db)
        tasks = service.list_tasks(db)
        assert len(tasks) == 2

    def test_filter_by_state(self, tmp_path):
        db = str(tmp_path / 't.db')
        service.init_db(db)
        t1 = _create(db)
        t2 = _create(db)
        service.transition_task(db, t1.id, 'ready', reason='ok', actor='u')
        pending = service.list_tasks(db, state='pending')
        assert all(t.state == 'pending' for t in pending)
        assert t2.id in [t.id for t in pending]

    def test_filter_by_type(self, tmp_path):
        db = str(tmp_path / 't.db')
        service.init_db(db)
        _create(db, task_type='analysis')
        _create(db, task_type='validation')
        tasks = service.list_tasks(db, task_type='analysis')
        assert all(t.task_type == 'analysis' for t in tasks)
        assert len(tasks) == 1

    def test_filter_by_actor(self, tmp_path):
        db = str(tmp_path / 't.db')
        service.init_db(db)
        _create(db, actor='alice')
        _create(db, actor='bob')
        tasks = service.list_tasks(db, actor='alice')
        assert all(t.actor == 'alice' for t in tasks)
        assert len(tasks) == 1

    def test_ordered_by_priority_then_id(self, tmp_path):
        db = str(tmp_path / 't.db')
        service.init_db(db)
        _create(db, priority=3, title='A')
        _create(db, priority=1, title='B')
        _create(db, priority=3, title='C')
        tasks = service.list_tasks(db)
        assert tasks[0].priority == 1
        assert tasks[1].id < tasks[2].id  # same priority → id ascending

    def test_limit_respected(self, tmp_path):
        db = str(tmp_path / 't.db')
        service.init_db(db)
        for _ in range(5):
            _create(db)
        tasks = service.list_tasks(db, limit=3)
        assert len(tasks) == 3

    def test_invalid_state_raises(self, tmp_path):
        db = str(tmp_path / 't.db')
        service.init_db(db)
        with pytest.raises(ValidationError):
            service.list_tasks(db, state='nonexistent')

    def test_empty_db_returns_empty(self, tmp_path):
        db = str(tmp_path / 't.db')
        service.init_db(db)
        assert service.list_tasks(db) == []


# ---------------------------------------------------------------------------
# transition_task — valid transitions and lineage
# ---------------------------------------------------------------------------

class TestTransitionTask:
    def test_transitions_state(self, tmp_path):
        db = str(tmp_path / 't.db')
        service.init_db(db)
        t = _create(db)
        updated = service.transition_task(db, t.id, 'ready', reason='ok', actor='u')
        assert updated.state == 'ready'

    def test_increments_version(self, tmp_path):
        db = str(tmp_path / 't.db')
        service.init_db(db)
        t = _create(db)
        updated = service.transition_task(db, t.id, 'ready', reason='ok', actor='u')
        assert updated.version == 2

    def test_creates_lineage_event(self, tmp_path):
        db = str(tmp_path / 't.db')
        service.init_db(db)
        t = _create(db)
        service.transition_task(db, t.id, 'ready', reason='dependencies cleared', actor='u')
        lineage = service.get_lineage(db, t.id)
        assert len(lineage) == 2
        assert lineage[1].old_state == 'pending'
        assert lineage[1].new_state == 'ready'
        assert lineage[1].reason == 'dependencies cleared'

    def test_lineage_captures_actor(self, tmp_path):
        db = str(tmp_path / 't.db')
        service.init_db(db)
        t = _create(db)
        service.transition_task(db, t.id, 'ready', reason='ok', actor='quant-lead')
        lineage = service.get_lineage(db, t.id)
        assert lineage[1].actor == 'quant-lead'

    def test_lineage_captures_metadata(self, tmp_path):
        db = str(tmp_path / 't.db')
        service.init_db(db)
        t = _create(db)
        service.transition_task(db, t.id, 'ready', reason='ok', actor='u',
                                metadata={'run_id': 99})
        lineage = service.get_lineage(db, t.id)
        assert lineage[1].metadata == {'run_id': 99}

    def test_invalid_transition_raises(self, tmp_path):
        db = str(tmp_path / 't.db')
        service.init_db(db)
        t = _create(db)
        with pytest.raises(TransitionError):
            service.transition_task(db, t.id, 'completed', reason='skip', actor='u')

    def test_not_found_raises(self, tmp_path):
        db = str(tmp_path / 't.db')
        service.init_db(db)
        with pytest.raises(NotFoundError):
            service.transition_task(db, 9999, 'ready', reason='ok', actor='u')

    def test_empty_reason_raises(self, tmp_path):
        db = str(tmp_path / 't.db')
        service.init_db(db)
        t = _create(db)
        with pytest.raises(ValidationError):
            service.transition_task(db, t.id, 'ready', reason='', actor='u')

    def test_empty_actor_raises(self, tmp_path):
        db = str(tmp_path / 't.db')
        service.init_db(db)
        t = _create(db)
        with pytest.raises(ValidationError):
            service.transition_task(db, t.id, 'ready', reason='ok', actor='')

    def test_full_happy_path_lineage(self, tmp_path):
        db = str(tmp_path / 't.db')
        service.init_db(db)
        t = _create(db)
        service.transition_task(db, t.id, 'ready',     reason='r', actor='u')
        service.transition_task(db, t.id, 'running',   reason='r', actor='u')
        service.transition_task(db, t.id, 'completed', reason='r', actor='u')
        lineage = service.get_lineage(db, t.id)
        states = [ev.new_state for ev in lineage]
        assert states == ['pending', 'ready', 'running', 'completed']


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------

class TestCancellation:
    @pytest.mark.parametrize('from_state,transitions', [
        ('pending',  []),
        ('ready',    [('pending', 'ready')]),
        ('blocked',  [('pending', 'blocked')]),
        ('failed',   [('pending', 'ready'), ('ready', 'running'), ('running', 'failed')]),
    ])
    def test_cancellation_from_valid_states(self, tmp_path, from_state, transitions):
        db = str(tmp_path / 't.db')
        service.init_db(db)
        t = _create(db)
        for old, new in transitions:
            service.transition_task(db, t.id, new, reason='setup', actor='u')
        cancelled = service.transition_task(db, t.id, 'cancelled', reason='user cancel', actor='u')
        assert cancelled.state == 'cancelled'

    def test_cannot_cancel_completed(self, tmp_path):
        db = str(tmp_path / 't.db')
        service.init_db(db)
        t = _create(db)
        service.transition_task(db, t.id, 'ready',     reason='r', actor='u')
        service.transition_task(db, t.id, 'running',   reason='r', actor='u')
        service.transition_task(db, t.id, 'completed', reason='r', actor='u')
        with pytest.raises(TransitionError):
            service.transition_task(db, t.id, 'cancelled', reason='r', actor='u')

    def test_cancelled_is_terminal(self, tmp_path):
        db = str(tmp_path / 't.db')
        service.init_db(db)
        t = _create(db)
        service.transition_task(db, t.id, 'cancelled', reason='r', actor='u')
        with pytest.raises(TransitionError):
            service.transition_task(db, t.id, 'pending', reason='r', actor='u')

    def test_cancellation_lineage(self, tmp_path):
        db = str(tmp_path / 't.db')
        service.init_db(db)
        t = _create(db)
        service.transition_task(db, t.id, 'cancelled', reason='no longer needed', actor='pm')
        lineage = service.get_lineage(db, t.id)
        last = lineage[-1]
        assert last.new_state == 'cancelled'
        assert last.reason == 'no longer needed'
        assert last.actor == 'pm'


# ---------------------------------------------------------------------------
# Superseding
# ---------------------------------------------------------------------------

class TestSuperseding:
    def test_supersedes_completed_task(self, tmp_path):
        db = str(tmp_path / 't.db')
        service.init_db(db)
        t = _create(db)
        service.transition_task(db, t.id, 'ready',     reason='r', actor='u')
        service.transition_task(db, t.id, 'running',   reason='r', actor='u')
        service.transition_task(db, t.id, 'completed', reason='r', actor='u')
        sup = service.transition_task(db, t.id, 'superseded', reason='v2 replaces', actor='u')
        assert sup.state == 'superseded'

    def test_superseded_is_terminal(self, tmp_path):
        db = str(tmp_path / 't.db')
        service.init_db(db)
        t = _create(db)
        service.transition_task(db, t.id, 'ready',     reason='r', actor='u')
        service.transition_task(db, t.id, 'running',   reason='r', actor='u')
        service.transition_task(db, t.id, 'completed', reason='r', actor='u')
        service.transition_task(db, t.id, 'superseded',reason='r', actor='u')
        with pytest.raises(TransitionError):
            service.transition_task(db, t.id, 'pending', reason='r', actor='u')

    def test_cannot_supersede_pending(self, tmp_path):
        db = str(tmp_path / 't.db')
        service.init_db(db)
        t = _create(db)
        with pytest.raises(TransitionError):
            service.transition_task(db, t.id, 'superseded', reason='r', actor='u')


# ---------------------------------------------------------------------------
# Retry lineage
# ---------------------------------------------------------------------------

class TestRetryLineage:
    def test_failed_to_ready_retry_path(self, tmp_path):
        db = str(tmp_path / 't.db')
        service.init_db(db)
        t = _create(db)
        service.transition_task(db, t.id, 'ready',   reason='r', actor='u')
        service.transition_task(db, t.id, 'running', reason='r', actor='u')
        service.transition_task(db, t.id, 'failed',  reason='timeout', actor='u')
        retry = service.transition_task(db, t.id, 'ready', reason='retry attempt 2', actor='u')
        assert retry.state == 'ready'

    def test_retry_creates_lineage(self, tmp_path):
        db = str(tmp_path / 't.db')
        service.init_db(db)
        t = _create(db)
        service.transition_task(db, t.id, 'ready',   reason='r', actor='u')
        service.transition_task(db, t.id, 'running', reason='r', actor='u')
        service.transition_task(db, t.id, 'failed',  reason='r', actor='u')
        service.transition_task(db, t.id, 'ready',   reason='retry', actor='u')
        lineage = service.get_lineage(db, t.id)
        states = [ev.new_state for ev in lineage]
        assert states == ['pending', 'ready', 'running', 'failed', 'ready']

    def test_multiple_retries_lineage(self, tmp_path):
        db = str(tmp_path / 't.db')
        service.init_db(db)
        t = _create(db)
        service.transition_task(db, t.id, 'ready',     reason='r', actor='u')
        service.transition_task(db, t.id, 'running',   reason='r', actor='u')
        service.transition_task(db, t.id, 'failed',    reason='r', actor='u')
        service.transition_task(db, t.id, 'ready',     reason='retry 1', actor='u')
        service.transition_task(db, t.id, 'running',   reason='r', actor='u')
        service.transition_task(db, t.id, 'failed',    reason='r', actor='u')
        service.transition_task(db, t.id, 'ready',     reason='retry 2', actor='u')
        service.transition_task(db, t.id, 'running',   reason='r', actor='u')
        service.transition_task(db, t.id, 'completed', reason='done', actor='u')
        lineage = service.get_lineage(db, t.id)
        failed_count = sum(1 for ev in lineage if ev.new_state == 'failed')
        assert failed_count == 2
        assert lineage[-1].new_state == 'completed'


# ---------------------------------------------------------------------------
# Dependency blocking
# ---------------------------------------------------------------------------

class TestDependencies:
    def test_add_dependency(self, tmp_path):
        db = str(tmp_path / 't.db')
        service.init_db(db)
        t1 = _create(db)
        t2 = _create(db)
        dep = service.add_dependency(db, t2.id, t1.id, 'task_completion')
        assert dep.task_id == t2.id
        assert dep.depends_on_id == t1.id
        assert dep.dependency_type == 'task_completion'

    def test_self_dependency_rejected(self, tmp_path):
        db = str(tmp_path / 't.db')
        service.init_db(db)
        t = _create(db)
        with pytest.raises(ValidationError, match='itself'):
            service.add_dependency(db, t.id, t.id, 'task_completion')

    def test_duplicate_dependency_rejected(self, tmp_path):
        db = str(tmp_path / 't.db')
        service.init_db(db)
        t1 = _create(db)
        t2 = _create(db)
        service.add_dependency(db, t2.id, t1.id, 'task_completion')
        with pytest.raises(ValidationError):
            service.add_dependency(db, t2.id, t1.id, 'task_completion')

    def test_invalid_dependency_type_rejected(self, tmp_path):
        db = str(tmp_path / 't.db')
        service.init_db(db)
        t1 = _create(db)
        t2 = _create(db)
        with pytest.raises(ValidationError):
            service.add_dependency(db, t2.id, t1.id, 'nonexistent_type')

    def test_all_valid_dependency_types(self, tmp_path):
        from orchestration.models import VALID_DEPENDENCY_TYPES
        db = str(tmp_path / 't.db')
        service.init_db(db)
        tasks = [_create(db) for _ in range(len(VALID_DEPENDENCY_TYPES) + 1)]
        for i, dtype in enumerate(VALID_DEPENDENCY_TYPES):
            dep = service.add_dependency(db, tasks[-1].id, tasks[i].id, dtype)
            assert dep.dependency_type == dtype

    def test_missing_task_raises(self, tmp_path):
        db = str(tmp_path / 't.db')
        service.init_db(db)
        t = _create(db)
        with pytest.raises(NotFoundError):
            service.add_dependency(db, t.id, 9999, 'task_completion')


# ---------------------------------------------------------------------------
# Blocking dependency detection
# ---------------------------------------------------------------------------

class TestGetBlockingDependencies:
    def test_no_dependencies_returns_empty(self, tmp_path):
        db = str(tmp_path / 't.db')
        service.init_db(db)
        t = _create(db)
        assert service.get_blocking_dependencies(db, t.id) == []

    def test_incomplete_dep_is_blocking(self, tmp_path):
        db = str(tmp_path / 't.db')
        service.init_db(db)
        upstream = _create(db)
        downstream = _create(db)
        service.add_dependency(db, downstream.id, upstream.id, 'task_completion')
        blocking = service.get_blocking_dependencies(db, downstream.id)
        assert len(blocking) == 1
        assert blocking[0].depends_on_id == upstream.id

    def test_completed_dep_not_blocking(self, tmp_path):
        db = str(tmp_path / 't.db')
        service.init_db(db)
        upstream = _create(db)
        service.transition_task(db, upstream.id, 'ready',     reason='r', actor='u')
        service.transition_task(db, upstream.id, 'running',   reason='r', actor='u')
        service.transition_task(db, upstream.id, 'completed', reason='r', actor='u')
        downstream = _create(db)
        service.add_dependency(db, downstream.id, upstream.id, 'task_completion')
        assert service.get_blocking_dependencies(db, downstream.id) == []

    def test_mixed_deps(self, tmp_path):
        db = str(tmp_path / 't.db')
        service.init_db(db)
        done = _create(db)
        service.transition_task(db, done.id, 'ready',     reason='r', actor='u')
        service.transition_task(db, done.id, 'running',   reason='r', actor='u')
        service.transition_task(db, done.id, 'completed', reason='r', actor='u')
        pending_dep = _create(db)
        downstream = _create(db)
        service.add_dependency(db, downstream.id, done.id,        'task_completion')
        service.add_dependency(db, downstream.id, pending_dep.id, 'task_completion')
        blocking = service.get_blocking_dependencies(db, downstream.id)
        assert len(blocking) == 1
        assert blocking[0].depends_on_id == pending_dep.id

    def test_not_found_raises(self, tmp_path):
        db = str(tmp_path / 't.db')
        service.init_db(db)
        with pytest.raises(NotFoundError):
            service.get_blocking_dependencies(db, 9999)


# ---------------------------------------------------------------------------
# check_and_unblock
# ---------------------------------------------------------------------------

class TestCheckAndUnblock:
    def test_unblocks_when_all_deps_complete(self, tmp_path):
        db = str(tmp_path / 't.db')
        service.init_db(db)
        upstream = _create(db)
        service.transition_task(db, upstream.id, 'ready',     reason='r', actor='u')
        service.transition_task(db, upstream.id, 'running',   reason='r', actor='u')
        service.transition_task(db, upstream.id, 'completed', reason='r', actor='u')
        downstream = _create(db)
        service.add_dependency(db, downstream.id, upstream.id, 'task_completion')
        service.transition_task(db, downstream.id, 'blocked', reason='waiting', actor='u')
        result = service.check_and_unblock(db, downstream.id)
        assert result is True
        task, _, _ = service.get_task(db, downstream.id)
        assert task.state == 'ready'

    def test_stays_blocked_when_dep_incomplete(self, tmp_path):
        db = str(tmp_path / 't.db')
        service.init_db(db)
        upstream = _create(db)
        downstream = _create(db)
        service.add_dependency(db, downstream.id, upstream.id, 'task_completion')
        service.transition_task(db, downstream.id, 'blocked', reason='waiting', actor='u')
        result = service.check_and_unblock(db, downstream.id)
        assert result is False
        task, _, _ = service.get_task(db, downstream.id)
        assert task.state == 'blocked'

    def test_non_blocked_task_returns_false(self, tmp_path):
        db = str(tmp_path / 't.db')
        service.init_db(db)
        t = _create(db)
        assert service.check_and_unblock(db, t.id) is False

    def test_unblock_creates_lineage(self, tmp_path):
        db = str(tmp_path / 't.db')
        service.init_db(db)
        upstream = _create(db)
        service.transition_task(db, upstream.id, 'ready',     reason='r', actor='u')
        service.transition_task(db, upstream.id, 'running',   reason='r', actor='u')
        service.transition_task(db, upstream.id, 'completed', reason='r', actor='u')
        downstream = _create(db)
        service.add_dependency(db, downstream.id, upstream.id, 'task_completion')
        service.transition_task(db, downstream.id, 'blocked', reason='waiting', actor='u')
        service.check_and_unblock(db, downstream.id)
        lineage = service.get_lineage(db, downstream.id)
        last = lineage[-1]
        assert last.old_state == 'blocked'
        assert last.new_state == 'ready'

    def test_not_found_raises(self, tmp_path):
        db = str(tmp_path / 't.db')
        service.init_db(db)
        with pytest.raises(NotFoundError):
            service.check_and_unblock(db, 9999)


# ---------------------------------------------------------------------------
# Dependency snapshot in lineage
# ---------------------------------------------------------------------------

class TestDependencySnapshot:
    def test_snapshot_empty_on_creation(self, tmp_path):
        db = str(tmp_path / 't.db')
        service.init_db(db)
        t = _create(db)
        lineage = service.get_lineage(db, t.id)
        assert lineage[0].dependency_snapshot == []

    def test_snapshot_captures_deps_at_transition(self, tmp_path):
        db = str(tmp_path / 't.db')
        service.init_db(db)
        upstream = _create(db)
        downstream = _create(db)
        service.add_dependency(db, downstream.id, upstream.id, 'task_completion')
        service.transition_task(db, downstream.id, 'blocked', reason='waiting', actor='u')
        lineage = service.get_lineage(db, downstream.id)
        assert upstream.id in lineage[-1].dependency_snapshot

    def test_snapshot_is_sorted(self, tmp_path):
        db = str(tmp_path / 't.db')
        service.init_db(db)
        u1 = _create(db)
        u2 = _create(db)
        u3 = _create(db)
        downstream = _create(db)
        service.add_dependency(db, downstream.id, u3.id, 'task_completion')
        service.add_dependency(db, downstream.id, u1.id, 'task_completion')
        service.add_dependency(db, downstream.id, u2.id, 'task_completion')
        service.transition_task(db, downstream.id, 'blocked', reason='r', actor='u')
        lineage = service.get_lineage(db, downstream.id)
        snap = lineage[-1].dependency_snapshot
        assert snap == sorted(snap)


# ---------------------------------------------------------------------------
# Transition auditability (get_lineage)
# ---------------------------------------------------------------------------

class TestTransitionAuditability:
    def test_lineage_ordered_chronologically(self, tmp_path):
        db = str(tmp_path / 't.db')
        service.init_db(db)
        t = _create(db)
        service.transition_task(db, t.id, 'ready',     reason='r', actor='u')
        service.transition_task(db, t.id, 'running',   reason='r', actor='u')
        service.transition_task(db, t.id, 'completed', reason='r', actor='u')
        lineage = service.get_lineage(db, t.id)
        ids = [ev.id for ev in lineage]
        assert ids == sorted(ids)

    def test_lineage_old_state_matches_sequence(self, tmp_path):
        db = str(tmp_path / 't.db')
        service.init_db(db)
        t = _create(db)
        service.transition_task(db, t.id, 'ready',   reason='r', actor='u')
        service.transition_task(db, t.id, 'running', reason='r', actor='u')
        lineage = service.get_lineage(db, t.id)
        assert lineage[0].old_state is None
        assert lineage[0].new_state == 'pending'
        assert lineage[1].old_state == 'pending'
        assert lineage[1].new_state == 'ready'
        assert lineage[2].old_state == 'ready'
        assert lineage[2].new_state == 'running'

    def test_get_lineage_not_found_raises(self, tmp_path):
        db = str(tmp_path / 't.db')
        service.init_db(db)
        with pytest.raises(NotFoundError):
            service.get_lineage(db, 9999)


# ---------------------------------------------------------------------------
# Execution history
# ---------------------------------------------------------------------------

class TestGetExecutionHistory:
    def test_returns_all_lineage_events(self, tmp_path):
        db = str(tmp_path / 't.db')
        service.init_db(db)
        t1 = _create(db)
        t2 = _create(db)
        service.transition_task(db, t1.id, 'ready', reason='r', actor='u')
        history = service.get_execution_history(db)
        # 2 creation events + 1 transition = 3
        assert len(history) == 3

    def test_ordered_by_lineage_id(self, tmp_path):
        db = str(tmp_path / 't.db')
        service.init_db(db)
        t = _create(db)
        service.transition_task(db, t.id, 'ready',   reason='r', actor='u')
        service.transition_task(db, t.id, 'running', reason='r', actor='u')
        history = service.get_execution_history(db)
        ids = [ev.id for ev in history]
        assert ids == sorted(ids)

    def test_empty_db_returns_empty(self, tmp_path):
        db = str(tmp_path / 't.db')
        service.init_db(db)
        assert service.get_execution_history(db) == []


# ---------------------------------------------------------------------------
# export_lineage — determinism and structure
# ---------------------------------------------------------------------------

class TestExportLineage:
    def test_top_level_keys(self, tmp_path):
        db = str(tmp_path / 't.db')
        service.init_db(db)
        payload = service.export_lineage(db)
        assert 'schema_version' in payload
        assert 'tasks' in payload
        assert 'task_lineage' in payload
        assert 'task_dependencies' in payload

    def test_schema_version(self, tmp_path):
        db = str(tmp_path / 't.db')
        service.init_db(db)
        assert service.export_lineage(db)['schema_version'] == 1

    def test_tasks_ordered_by_id(self, tmp_path):
        db = str(tmp_path / 't.db')
        service.init_db(db)
        _create(db)
        _create(db)
        payload = service.export_lineage(db)
        ids = [t['id'] for t in payload['tasks']]
        assert ids == sorted(ids)

    def test_lineage_ordered_by_id(self, tmp_path):
        db = str(tmp_path / 't.db')
        service.init_db(db)
        t = _create(db)
        service.transition_task(db, t.id, 'ready', reason='r', actor='u')
        payload = service.export_lineage(db)
        ids = [e['id'] for e in payload['task_lineage']]
        assert ids == sorted(ids)

    def test_deterministic_repeated_export(self, tmp_path):
        db = str(tmp_path / 't.db')
        service.init_db(db)
        t = _create(db, tags=['a', 'b'], metadata={'k': 1})
        service.transition_task(db, t.id, 'ready', reason='r', actor='u')
        p1 = service.export_lineage(db)
        p2 = service.export_lineage(db)
        assert p1 == p2

    def test_empty_db_export(self, tmp_path):
        db = str(tmp_path / 't.db')
        service.init_db(db)
        payload = service.export_lineage(db)
        assert payload['tasks'] == []
        assert payload['task_lineage'] == []
        assert payload['task_dependencies'] == []

    def test_task_dict_has_required_fields(self, tmp_path):
        db = str(tmp_path / 't.db')
        service.init_db(db)
        _create(db)
        payload = service.export_lineage(db)
        t = payload['tasks'][0]
        for field in ('id', 'title', 'task_type', 'state', 'priority', 'actor',
                      'tags', 'metadata', 'created_at', 'updated_at', 'version'):
            assert field in t

    def test_lineage_dict_has_required_fields(self, tmp_path):
        db = str(tmp_path / 't.db')
        service.init_db(db)
        _create(db)
        payload = service.export_lineage(db)
        ev = payload['task_lineage'][0]
        for field in ('id', 'task_id', 'old_state', 'new_state', 'reason',
                      'actor', 'dependency_snapshot', 'metadata', 'created_at'):
            assert field in ev


# ---------------------------------------------------------------------------
# Deterministic ordering
# ---------------------------------------------------------------------------

class TestDeterministicOrdering:
    def test_list_tasks_deterministic(self, tmp_path):
        db = str(tmp_path / 't.db')
        service.init_db(db)
        for i in range(5):
            _create(db, priority=(i % 3) + 1)
        r1 = [t.id for t in service.list_tasks(db)]
        r2 = [t.id for t in service.list_tasks(db)]
        assert r1 == r2

    def test_get_lineage_deterministic(self, tmp_path):
        db = str(tmp_path / 't.db')
        service.init_db(db)
        t = _create(db)
        service.transition_task(db, t.id, 'ready',   reason='r', actor='u')
        service.transition_task(db, t.id, 'running', reason='r', actor='u')
        r1 = [ev.id for ev in service.get_lineage(db, t.id)]
        r2 = [ev.id for ev in service.get_lineage(db, t.id)]
        assert r1 == r2

    def test_execution_history_deterministic(self, tmp_path):
        db = str(tmp_path / 't.db')
        service.init_db(db)
        for _ in range(3):
            _create(db)
        r1 = [ev.id for ev in service.get_execution_history(db)]
        r2 = [ev.id for ev in service.get_execution_history(db)]
        assert r1 == r2

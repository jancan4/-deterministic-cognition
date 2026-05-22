import pytest

from runtime.models import RuntimeConfig, VALID_RUNTIME_STATES, TransitionError
from runtime.state_store import (
    NotFoundError,
    ValidationError,
    get_all_checkpoints,
    get_latest_checkpoint,
    get_runtime,
    get_runtime_lineage,
    init_db,
    list_runtimes,
    register_runtime,
    save_checkpoint,
    transition_runtime,
)


def _cfg(actor='test-actor'):
    return RuntimeConfig(actor=actor)


def _db(tmp_path):
    db = str(tmp_path / 'runtime.db')
    init_db(db)
    return db


# ---------------------------------------------------------------------------
# init_db
# ---------------------------------------------------------------------------

def test_init_db_idempotent(tmp_path):
    db = _db(tmp_path)
    init_db(db)  # second call must not raise


# ---------------------------------------------------------------------------
# register_runtime
# ---------------------------------------------------------------------------

def test_register_runtime(tmp_path):
    db = _db(tmp_path)
    rt = register_runtime(db, 'test-runtime', '/path/to/orch.db', _cfg())
    assert rt.id == 1
    assert rt.name == 'test-runtime'
    assert rt.state == 'initialized'
    assert rt.orchestration_db == '/path/to/orch.db'
    assert rt.current_iteration == 0
    assert rt.version == 1


def test_register_runtime_creates_lineage(tmp_path):
    db = _db(tmp_path)
    rt = register_runtime(db, 'r', '/orch.db', _cfg())
    lineage = get_runtime_lineage(db, rt.id)
    assert len(lineage) == 1
    assert lineage[0].old_state is None
    assert lineage[0].new_state == 'initialized'


def test_register_runtime_empty_name_rejected(tmp_path):
    db = _db(tmp_path)
    with pytest.raises(ValidationError, match="name"):
        register_runtime(db, '', '/orch.db', _cfg())


def test_register_runtime_empty_orchestration_db_rejected(tmp_path):
    db = _db(tmp_path)
    with pytest.raises(ValidationError, match="orchestration_db"):
        register_runtime(db, 'r', '', _cfg())


def test_register_runtime_empty_actor_rejected(tmp_path):
    db = _db(tmp_path)
    with pytest.raises(ValidationError, match="actor"):
        register_runtime(db, 'r', '/orch.db', RuntimeConfig(actor=''))


def test_register_runtime_whitespace_name_rejected(tmp_path):
    db = _db(tmp_path)
    with pytest.raises(ValidationError):
        register_runtime(db, '   ', '/orch.db', _cfg())


# ---------------------------------------------------------------------------
# get_runtime / list_runtimes
# ---------------------------------------------------------------------------

def test_get_runtime_not_found(tmp_path):
    db = _db(tmp_path)
    with pytest.raises(NotFoundError):
        get_runtime(db, 999)


def test_get_runtime_returns_correct(tmp_path):
    db = _db(tmp_path)
    rt = register_runtime(db, 'my-runtime', '/orch.db', _cfg())
    fetched = get_runtime(db, rt.id)
    assert fetched.id == rt.id
    assert fetched.state == 'initialized'


def test_list_runtimes_empty(tmp_path):
    db = _db(tmp_path)
    assert list_runtimes(db) == []


def test_list_runtimes_ordered_by_id(tmp_path):
    db = _db(tmp_path)
    r1 = register_runtime(db, 'r1', '/o.db', _cfg())
    r2 = register_runtime(db, 'r2', '/o.db', _cfg())
    runtimes = list_runtimes(db)
    assert [r.id for r in runtimes] == [r1.id, r2.id]


# ---------------------------------------------------------------------------
# transition_runtime
# ---------------------------------------------------------------------------

def test_transition_runtime_initialized_to_idle(tmp_path):
    db = _db(tmp_path)
    rt = register_runtime(db, 'r', '/o.db', _cfg())
    updated = transition_runtime(db, rt.id, 'idle', reason='starting', iteration=0)
    assert updated.state == 'idle'
    assert updated.version == 2


def test_transition_runtime_increments_version(tmp_path):
    db = _db(tmp_path)
    rt = register_runtime(db, 'r', '/o.db', _cfg())
    v1 = transition_runtime(db, rt.id, 'idle', reason='r', iteration=0)
    v2 = transition_runtime(db, rt.id, 'polling', reason='r', iteration=1)
    assert v2.version == v1.version + 1


def test_transition_runtime_updates_iteration(tmp_path):
    db = _db(tmp_path)
    rt = register_runtime(db, 'r', '/o.db', _cfg())
    transition_runtime(db, rt.id, 'idle', reason='r', iteration=0)
    updated = transition_runtime(db, rt.id, 'polling', reason='r', iteration=5)
    assert updated.current_iteration == 5


def test_transition_runtime_writes_lineage(tmp_path):
    db = _db(tmp_path)
    rt = register_runtime(db, 'r', '/o.db', _cfg())
    transition_runtime(db, rt.id, 'idle', reason='start', iteration=0)
    lineage = get_runtime_lineage(db, rt.id)
    assert len(lineage) == 2
    assert lineage[1].old_state == 'initialized'
    assert lineage[1].new_state == 'idle'
    assert lineage[1].reason == 'start'


def test_transition_runtime_invalid_raises(tmp_path):
    db = _db(tmp_path)
    rt = register_runtime(db, 'r', '/o.db', _cfg())
    with pytest.raises(TransitionError):
        transition_runtime(db, rt.id, 'completed', reason='bad', iteration=0)


def test_transition_runtime_self_transition_raises(tmp_path):
    db = _db(tmp_path)
    rt = register_runtime(db, 'r', '/o.db', _cfg())
    transition_runtime(db, rt.id, 'idle', reason='r', iteration=0)
    with pytest.raises(TransitionError, match='Self-transition'):
        transition_runtime(db, rt.id, 'idle', reason='no-op', iteration=0)


def test_transition_runtime_initialized_to_interrupted_raises(tmp_path):
    """initialized → interrupted is not a valid arc — nothing ran to be interrupted."""
    db = _db(tmp_path)
    rt = register_runtime(db, 'r', '/o.db', _cfg())
    with pytest.raises(TransitionError):
        transition_runtime(db, rt.id, 'interrupted', reason='bad', iteration=0)


def test_transition_runtime_terminal_raises(tmp_path):
    db = _db(tmp_path)
    rt = register_runtime(db, 'r', '/o.db', _cfg())
    transition_runtime(db, rt.id, 'idle', reason='r', iteration=0)
    transition_runtime(db, rt.id, 'stopped', reason='done', iteration=0)
    with pytest.raises(TransitionError):
        transition_runtime(db, rt.id, 'idle', reason='bad', iteration=0)


def test_transition_runtime_not_found(tmp_path):
    db = _db(tmp_path)
    with pytest.raises(NotFoundError):
        transition_runtime(db, 999, 'idle', reason='r', iteration=0)


def test_transition_runtime_empty_reason_rejected(tmp_path):
    db = _db(tmp_path)
    rt = register_runtime(db, 'r', '/o.db', _cfg())
    with pytest.raises(ValidationError, match="reason"):
        transition_runtime(db, rt.id, 'idle', reason='', iteration=0)


# ---------------------------------------------------------------------------
# get_runtime_lineage
# ---------------------------------------------------------------------------

def test_get_runtime_lineage_ordered_ascending(tmp_path):
    db = _db(tmp_path)
    rt = register_runtime(db, 'r', '/o.db', _cfg())
    transition_runtime(db, rt.id, 'idle', reason='r', iteration=0)
    transition_runtime(db, rt.id, 'polling', reason='r', iteration=1)
    lineage = get_runtime_lineage(db, rt.id)
    states = [e.new_state for e in lineage]
    assert states == ['initialized', 'idle', 'polling']


def test_get_runtime_lineage_not_found(tmp_path):
    db = _db(tmp_path)
    with pytest.raises(NotFoundError):
        get_runtime_lineage(db, 999)


# ---------------------------------------------------------------------------
# save_checkpoint / get_latest_checkpoint / get_all_checkpoints
# ---------------------------------------------------------------------------

def test_save_checkpoint(tmp_path):
    db = _db(tmp_path)
    rt = register_runtime(db, 'r', '/o.db', _cfg())
    cp = save_checkpoint(db, rt.id, 1, {'iteration': 1, 'tasks_executed': 3}, 'iter 1')
    assert cp.runtime_id == rt.id
    assert cp.iteration == 1
    assert cp.state == {'iteration': 1, 'tasks_executed': 3}
    assert cp.reason == 'iter 1'


def test_save_checkpoint_empty_reason_rejected(tmp_path):
    db = _db(tmp_path)
    rt = register_runtime(db, 'r', '/o.db', _cfg())
    with pytest.raises(ValidationError, match="reason"):
        save_checkpoint(db, rt.id, 1, {}, '')


def test_save_checkpoint_not_found(tmp_path):
    db = _db(tmp_path)
    with pytest.raises(NotFoundError):
        save_checkpoint(db, 999, 1, {}, 'r')


def test_get_latest_checkpoint_empty(tmp_path):
    db = _db(tmp_path)
    rt = register_runtime(db, 'r', '/o.db', _cfg())
    assert get_latest_checkpoint(db, rt.id) is None


def test_get_latest_checkpoint_returns_latest(tmp_path):
    db = _db(tmp_path)
    rt = register_runtime(db, 'r', '/o.db', _cfg())
    save_checkpoint(db, rt.id, 1, {'iteration': 1}, 'first')
    save_checkpoint(db, rt.id, 2, {'iteration': 2}, 'second')
    cp = get_latest_checkpoint(db, rt.id)
    assert cp.iteration == 2


def test_get_latest_checkpoint_not_found(tmp_path):
    db = _db(tmp_path)
    with pytest.raises(NotFoundError):
        get_latest_checkpoint(db, 999)


def test_get_all_checkpoints_ordered_ascending(tmp_path):
    db = _db(tmp_path)
    rt = register_runtime(db, 'r', '/o.db', _cfg())
    save_checkpoint(db, rt.id, 1, {'iteration': 1}, 'first')
    save_checkpoint(db, rt.id, 2, {'iteration': 2}, 'second')
    save_checkpoint(db, rt.id, 3, {'iteration': 3}, 'third')
    cps = get_all_checkpoints(db, rt.id)
    assert [cp.iteration for cp in cps] == [1, 2, 3]


def test_get_all_checkpoints_not_found(tmp_path):
    db = _db(tmp_path)
    with pytest.raises(NotFoundError):
        get_all_checkpoints(db, 999)


def test_checkpoint_state_preserves_types(tmp_path):
    db = _db(tmp_path)
    rt = register_runtime(db, 'r', '/o.db', _cfg())
    state = {'count': 42, 'active': True, 'label': 'done', 'nested': {'x': 1}}
    cp = save_checkpoint(db, rt.id, 1, state, 'r')
    assert cp.state == state


def test_multiple_runtimes_isolated(tmp_path):
    db = _db(tmp_path)
    r1 = register_runtime(db, 'r1', '/o.db', _cfg())
    r2 = register_runtime(db, 'r2', '/o.db', _cfg())
    save_checkpoint(db, r1.id, 1, {'owner': 'r1'}, 'r')
    assert get_latest_checkpoint(db, r2.id) is None
    assert get_latest_checkpoint(db, r1.id).state == {'owner': 'r1'}

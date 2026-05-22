import json

import pytest

from runtime.checkpoints import create_checkpoint, restore_from_checkpoint, serialize_checkpoint
from runtime.models import Checkpoint, RuntimeConfig
from runtime.state_store import init_db, register_runtime


def _db(tmp_path):
    db = str(tmp_path / 'runtime.db')
    init_db(db)
    return db


def _runtime(db):
    return register_runtime(db, 'r', '/o.db', RuntimeConfig(actor='test'))


# ---------------------------------------------------------------------------
# serialize_checkpoint
# ---------------------------------------------------------------------------

def test_serialize_checkpoint_returns_valid_json(tmp_path):
    result = serialize_checkpoint({'a': 1, 'b': 2})
    parsed = json.loads(result)
    assert parsed == {'a': 1, 'b': 2}


def test_serialize_checkpoint_sort_keys(tmp_path):
    result = serialize_checkpoint({'z': 3, 'a': 1, 'm': 2})
    assert result == '{"a": 1, "m": 2, "z": 3}'


def test_serialize_checkpoint_deterministic(tmp_path):
    state = {'iteration': 5, 'tasks_executed': 12, 'label': 'active'}
    assert serialize_checkpoint(state) == serialize_checkpoint(state)


def test_serialize_checkpoint_nested(tmp_path):
    state = {'outer': {'inner': [1, 2, 3]}, 'count': 0}
    result = serialize_checkpoint(state)
    assert json.loads(result) == state


def test_serialize_empty_state(tmp_path):
    assert serialize_checkpoint({}) == '{}'


# ---------------------------------------------------------------------------
# create_checkpoint
# ---------------------------------------------------------------------------

def test_create_checkpoint_persists(tmp_path):
    db = _db(tmp_path)
    rt = _runtime(db)
    cp = create_checkpoint(db, rt.id, 3, {'iteration': 3, 'tasks_executed': 7}, 'iter 3')
    assert cp.id is not None
    assert cp.runtime_id == rt.id
    assert cp.iteration == 3
    assert cp.state == {'iteration': 3, 'tasks_executed': 7}
    assert cp.reason == 'iter 3'


def test_create_checkpoint_empty_reason_rejected(tmp_path):
    from runtime.state_store import ValidationError
    db = _db(tmp_path)
    rt = _runtime(db)
    with pytest.raises(ValidationError):
        create_checkpoint(db, rt.id, 1, {}, '')


def test_create_checkpoint_multiple(tmp_path):
    from runtime.state_store import get_all_checkpoints
    db = _db(tmp_path)
    rt = _runtime(db)
    create_checkpoint(db, rt.id, 1, {'iteration': 1}, 'first')
    create_checkpoint(db, rt.id, 2, {'iteration': 2}, 'second')
    all_cps = get_all_checkpoints(db, rt.id)
    assert len(all_cps) == 2
    assert all_cps[0].iteration == 1
    assert all_cps[1].iteration == 2


# ---------------------------------------------------------------------------
# restore_from_checkpoint
# ---------------------------------------------------------------------------

def test_restore_from_checkpoint_returns_state(tmp_path):
    db = _db(tmp_path)
    rt = _runtime(db)
    state = {'iteration': 5, 'tasks_executed': 10}
    cp = create_checkpoint(db, rt.id, 5, state, 'r')
    restored = restore_from_checkpoint(cp)
    assert restored == state


def test_restore_from_checkpoint_is_copy(tmp_path):
    db = _db(tmp_path)
    rt = _runtime(db)
    state = {'x': 1}
    cp = create_checkpoint(db, rt.id, 1, state, 'r')
    restored = restore_from_checkpoint(cp)
    restored['x'] = 999
    # Original checkpoint state must not be mutated.
    assert cp.state['x'] == 1


def test_restore_from_checkpoint_pure(tmp_path):
    """restore_from_checkpoint does not write to any database."""
    db = _db(tmp_path)
    rt = _runtime(db)
    cp = create_checkpoint(db, rt.id, 1, {'k': 'v'}, 'r')

    from runtime.state_store import get_all_checkpoints
    before = get_all_checkpoints(db, rt.id)
    restore_from_checkpoint(cp)
    after = get_all_checkpoints(db, rt.id)
    assert len(before) == len(after)


def test_create_and_restore_roundtrip(tmp_path):
    db = _db(tmp_path)
    rt = _runtime(db)
    state = {'iteration': 42, 'tasks_executed': 100, 'label': 'mid-run'}
    cp = create_checkpoint(db, rt.id, 42, state, 'mid-run checkpoint')
    restored = restore_from_checkpoint(cp)
    assert restored == state


def test_restore_empty_state(tmp_path):
    db = _db(tmp_path)
    rt = _runtime(db)
    cp = create_checkpoint(db, rt.id, 0, {}, 'empty')
    assert restore_from_checkpoint(cp) == {}


def test_restore_preserves_types(tmp_path):
    db = _db(tmp_path)
    rt = _runtime(db)
    state = {'count': 5, 'active': True, 'ratio': 0.75, 'label': 'ok'}
    cp = create_checkpoint(db, rt.id, 1, state, 'r')
    restored = restore_from_checkpoint(cp)
    assert restored['count'] == 5
    assert restored['active'] is True
    assert restored['ratio'] == 0.75
    assert restored['label'] == 'ok'

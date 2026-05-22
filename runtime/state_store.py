import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .models import (
    TERMINAL_RUNTIME_STATES,
    VALID_RUNTIME_STATES,
    RuntimeConfig,
    Runtime,
    RuntimeLineageEvent,
    Checkpoint,
    TransitionError,
    validate_runtime_transition,
)

_SCHEMA = Path(__file__).parent / 'schema.sql'


class ValidationError(ValueError):
    pass


class NotFoundError(KeyError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA foreign_keys=ON')
    return conn


def init_db(db_path: str) -> None:
    with _connect(db_path) as conn:
        conn.executescript(_SCHEMA.read_text())


def register_runtime(
    db_path: str,
    name: str,
    orchestration_db: str,
    config: RuntimeConfig,
) -> Runtime:
    if not name or not name.strip():
        raise ValidationError("'name' must not be empty")
    if not orchestration_db or not orchestration_db.strip():
        raise ValidationError("'orchestration_db' must not be empty")
    if not config.actor or not config.actor.strip():
        raise ValidationError("'config.actor' must not be empty")

    now = _now()
    config_json = json.dumps(config.to_dict(), sort_keys=True)

    with _connect(db_path) as conn:
        cur = conn.execute(
            'INSERT INTO runtimes'
            ' (name, state, orchestration_db, config_json,'
            '  current_iteration, created_at, updated_at, version)'
            ' VALUES (?,?,?,?,0,?,?,1)',
            (name, 'initialized', orchestration_db, config_json, now, now),
        )
        runtime_id = cur.lastrowid
        conn.execute(
            'INSERT INTO runtime_lineage'
            ' (runtime_id, old_state, new_state, reason, iteration, metadata_json, created_at)'
            ' VALUES (?,?,?,?,0,?,?)',
            (runtime_id, None, 'initialized', 'Runtime registered', '{}', now),
        )
        row = conn.execute('SELECT * FROM runtimes WHERE id = ?', (runtime_id,)).fetchone()
        return Runtime.from_row(row)


def get_runtime(db_path: str, runtime_id: int) -> Runtime:
    with _connect(db_path) as conn:
        row = conn.execute('SELECT * FROM runtimes WHERE id = ?', (runtime_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"Runtime {runtime_id} not found")
        return Runtime.from_row(row)


def list_runtimes(db_path: str) -> List[Runtime]:
    with _connect(db_path) as conn:
        rows = conn.execute('SELECT * FROM runtimes ORDER BY id ASC').fetchall()
        return [Runtime.from_row(r) for r in rows]


def transition_runtime(
    db_path: str,
    runtime_id: int,
    new_state: str,
    reason: str,
    iteration: int = 0,
    metadata: Optional[Dict[str, Any]] = None,
) -> Runtime:
    if not reason or not reason.strip():
        raise ValidationError("'reason' must not be empty")

    now = _now()
    meta_json = json.dumps(metadata or {}, sort_keys=True)

    with _connect(db_path) as conn:
        row = conn.execute('SELECT * FROM runtimes WHERE id = ?', (runtime_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"Runtime {runtime_id} not found")

        old_state = row['state']
        old_version = row['version']

        try:
            validate_runtime_transition(old_state, new_state)
        except TransitionError:
            raise

        new_version = old_version + 1
        conn.execute(
            'UPDATE runtimes SET state = ?, current_iteration = ?, version = ?, updated_at = ?'
            ' WHERE id = ?',
            (new_state, iteration, new_version, now, runtime_id),
        )
        conn.execute(
            'INSERT INTO runtime_lineage'
            ' (runtime_id, old_state, new_state, reason, iteration, metadata_json, created_at)'
            ' VALUES (?,?,?,?,?,?,?)',
            (runtime_id, old_state, new_state, reason, iteration, meta_json, now),
        )
        row = conn.execute('SELECT * FROM runtimes WHERE id = ?', (runtime_id,)).fetchone()
        return Runtime.from_row(row)


def get_runtime_lineage(db_path: str, runtime_id: int) -> List[RuntimeLineageEvent]:
    with _connect(db_path) as conn:
        if conn.execute('SELECT id FROM runtimes WHERE id = ?', (runtime_id,)).fetchone() is None:
            raise NotFoundError(f"Runtime {runtime_id} not found")
        rows = conn.execute(
            'SELECT * FROM runtime_lineage WHERE runtime_id = ? ORDER BY id ASC',
            (runtime_id,),
        ).fetchall()
        return [RuntimeLineageEvent.from_row(r) for r in rows]


def save_checkpoint(
    db_path: str,
    runtime_id: int,
    iteration: int,
    state: Dict[str, Any],
    reason: str,
) -> Checkpoint:
    if not reason or not reason.strip():
        raise ValidationError("'reason' must not be empty")

    now = _now()
    state_json = json.dumps(state, sort_keys=True)

    with _connect(db_path) as conn:
        if conn.execute('SELECT id FROM runtimes WHERE id = ?', (runtime_id,)).fetchone() is None:
            raise NotFoundError(f"Runtime {runtime_id} not found")
        cur = conn.execute(
            'INSERT INTO runtime_checkpoints'
            ' (runtime_id, iteration, state_json, reason, created_at)'
            ' VALUES (?,?,?,?,?)',
            (runtime_id, iteration, state_json, reason, now),
        )
        row = conn.execute(
            'SELECT * FROM runtime_checkpoints WHERE id = ?', (cur.lastrowid,)
        ).fetchone()
        return Checkpoint.from_row(row)


def get_latest_checkpoint(db_path: str, runtime_id: int) -> Optional[Checkpoint]:
    with _connect(db_path) as conn:
        if conn.execute('SELECT id FROM runtimes WHERE id = ?', (runtime_id,)).fetchone() is None:
            raise NotFoundError(f"Runtime {runtime_id} not found")
        row = conn.execute(
            'SELECT * FROM runtime_checkpoints WHERE runtime_id = ?'
            ' ORDER BY id DESC LIMIT 1',
            (runtime_id,),
        ).fetchone()
        return Checkpoint.from_row(row) if row else None


def get_all_checkpoints(db_path: str, runtime_id: int) -> List[Checkpoint]:
    with _connect(db_path) as conn:
        if conn.execute('SELECT id FROM runtimes WHERE id = ?', (runtime_id,)).fetchone() is None:
            raise NotFoundError(f"Runtime {runtime_id} not found")
        rows = conn.execute(
            'SELECT * FROM runtime_checkpoints WHERE runtime_id = ? ORDER BY id ASC',
            (runtime_id,),
        ).fetchall()
        return [Checkpoint.from_row(r) for r in rows]

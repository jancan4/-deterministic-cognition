"""Tests for memory/embedding_pins.py and Phase 2D schema migration."""
import sqlite3

import pytest

from memory.artifact_governance import (
    EMBEDDING_VISIBLE_FIELDS_VERSION,
    compute_pin_identity,
)
from memory.embedding_pins import (
    PinRecord,
    create_pin,
    get_active_pin,
    list_pins,
    supersede_pin,
)
from memory.service import init_db


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / 'pins_test.db')
    init_db(path)
    return path


def _stub_pin_kwargs(**overrides) -> dict:
    base = {
        'adapter_name': 'stub_embedding',
        'adapter_version': '1.0.0',
        'model_name': 'stub-model',
        'model_digest': None,
        'dimensions': 4,
        'provider_name': 'stub',
        'pinned_by': 'test-operator',
        'pin_scope': 'global',
    }
    base.update(overrides)
    return base


def _ollama_pin_kwargs(**overrides) -> dict:
    base = {
        'adapter_name': 'ollama-embedding',
        'adapter_version': '1.0.0',
        'model_name': 'nomic-embed-text',
        'model_digest': 'sha256:abc123def456',
        'dimensions': 768,
        'provider_name': 'ollama',
        'pinned_by': 'test-operator',
        'pin_scope': 'global',
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Schema v5 migration
# ---------------------------------------------------------------------------

class TestSchemaV5Migration:
    def test_schema_version_is_6(self, db):
        conn = sqlite3.connect(db)
        row = conn.execute('SELECT version FROM memory_schema_version').fetchone()
        conn.close()
        assert row[0] == 6

    def test_embedding_model_pins_table_exists(self, db):
        conn = sqlite3.connect(db)
        cols = {r[1] for r in conn.execute('PRAGMA table_info(embedding_model_pins)')}
        conn.close()
        required = {
            'id', 'pin_scope', 'adapter_name', 'adapter_version',
            'model_name', 'model_digest', 'dimensions',
            'embedding_visible_fields_version', 'pin_identity',
            'provider_name', 'status', 'pinned_at', 'pinned_by',
            'superseded_at', 'superseded_reason', 'notes',
        }
        assert required <= cols

    def test_pins_indices_exist(self, db):
        conn = sqlite3.connect(db)
        indices = {r[1] for r in conn.execute('PRAGMA index_list(embedding_model_pins)')}
        conn.close()
        assert 'idx_pins_scope_status' in indices
        assert 'idx_pins_identity' in indices
        assert 'idx_pins_pinned_at' in indices

    def test_v4_db_migrates_to_v5(self, tmp_path):
        """A DB at version 4 should be upgraded to 5 by init_db()."""
        from memory.service import _connect
        db_path = str(tmp_path / 'v4.db')
        # Create a minimal v4 DB manually.
        conn = _connect(db_path)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS memory_schema_version (version INTEGER NOT NULL);
            INSERT INTO memory_schema_version (version) VALUES (4);
            CREATE TABLE IF NOT EXISTS memory_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL, title TEXT NOT NULL, summary TEXT NOT NULL,
                evidence TEXT, source TEXT NOT NULL, confidence INTEGER NOT NULL,
                status TEXT NOT NULL, tags_json TEXT NOT NULL DEFAULT '[]',
                related_ids_json TEXT NOT NULL DEFAULT '[]',
                created_by TEXT NOT NULL, created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS memory_revisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT, memory_id INTEGER NOT NULL,
                old_value_json TEXT NOT NULL, new_value_json TEXT NOT NULL,
                reason TEXT NOT NULL, created_at TEXT NOT NULL, created_by TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS memory_links (
                id INTEGER PRIMARY KEY AUTOINCREMENT, source_id INTEGER NOT NULL,
                target_id INTEGER NOT NULL, relationship TEXT NOT NULL, created_at TEXT NOT NULL,
                UNIQUE (source_id, target_id, relationship)
            );
            CREATE TABLE IF NOT EXISTS retrieval_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT, query_hash TEXT NOT NULL,
                session_id TEXT, query_json TEXT NOT NULL, scoring_version TEXT NOT NULL,
                scoring_params_json TEXT NOT NULL, result_event_ids_json TEXT NOT NULL,
                result_count INTEGER NOT NULL, executed_at TEXT NOT NULL,
                actor TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active'
            );
            CREATE TABLE IF NOT EXISTS event_embeddings (
                id INTEGER PRIMARY KEY AUTOINCREMENT, memory_event_id INTEGER NOT NULL,
                content_hash TEXT NOT NULL, vector_json TEXT NOT NULL,
                dimensions INTEGER NOT NULL, model_name TEXT NOT NULL,
                model_version TEXT NOT NULL, model_digest TEXT, provider_name TEXT NOT NULL,
                adapter_name TEXT NOT NULL, adapter_version TEXT NOT NULL,
                producer_version TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'candidate',
                generated_at TEXT NOT NULL, invalidated_at TEXT, invalidated_reason TEXT,
                provenance_json TEXT NOT NULL
            );
        """)
        conn.commit()
        conn.close()

        init_db(db_path)

        conn = sqlite3.connect(db_path)
        version = conn.execute('SELECT version FROM memory_schema_version').fetchone()[0]
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        conn.close()
        assert version == 6
        assert 'embedding_model_pins' in tables


# ---------------------------------------------------------------------------
# compute_pin_identity
# ---------------------------------------------------------------------------

class TestComputePinIdentity:
    def test_returns_16_char_hex(self):
        h = compute_pin_identity('stub', '1.0.0', 'stub-model', None, 4, '1')
        assert len(h) == 16
        int(h, 16)

    def test_is_deterministic(self):
        h1 = compute_pin_identity('stub', '1.0.0', 'stub-model', None, 4, '1')
        h2 = compute_pin_identity('stub', '1.0.0', 'stub-model', None, 4, '1')
        assert h1 == h2

    def test_none_digest_is_stable_and_differs_from_real_digest(self):
        # None (no digest) produces a stable identity.
        h_none1 = compute_pin_identity('stub', '1.0.0', 'model', None, 4, '1')
        h_none2 = compute_pin_identity('stub', '1.0.0', 'model', None, 4, '1')
        assert h_none1 == h_none2
        # A real SHA256 digest must produce a different identity from None.
        h_real = compute_pin_identity('stub', '1.0.0', 'model',
                                      'sha256:abc123def456789012345678901234567890123', 4, '1')
        assert h_none1 != h_real

    def test_adapter_name_change_changes_identity(self):
        h1 = compute_pin_identity('stub', '1.0.0', 'model', None, 4, '1')
        h2 = compute_pin_identity('ollama', '1.0.0', 'model', None, 4, '1')
        assert h1 != h2

    def test_adapter_version_change_changes_identity(self):
        h1 = compute_pin_identity('stub', '1.0.0', 'model', None, 4, '1')
        h2 = compute_pin_identity('stub', '2.0.0', 'model', None, 4, '1')
        assert h1 != h2

    def test_model_name_change_changes_identity(self):
        h1 = compute_pin_identity('stub', '1.0.0', 'model-a', None, 4, '1')
        h2 = compute_pin_identity('stub', '1.0.0', 'model-b', None, 4, '1')
        assert h1 != h2

    def test_model_digest_change_changes_identity(self):
        h1 = compute_pin_identity('stub', '1.0.0', 'model', 'abc123', 4, '1')
        h2 = compute_pin_identity('stub', '1.0.0', 'model', 'xyz789', 4, '1')
        assert h1 != h2

    def test_dimensions_change_changes_identity(self):
        h1 = compute_pin_identity('stub', '1.0.0', 'model', None, 4, '1')
        h2 = compute_pin_identity('stub', '1.0.0', 'model', None, 768, '1')
        assert h1 != h2

    def test_evfv_change_changes_identity(self):
        h1 = compute_pin_identity('stub', '1.0.0', 'model', None, 4, '1')
        h2 = compute_pin_identity('stub', '1.0.0', 'model', None, 4, '2')
        assert h1 != h2

    def test_stable_under_ordering_differences(self):
        # Identity must be stable regardless of Python call-site ordering.
        # Both calls use the same positional arguments — just testing stability.
        h1 = compute_pin_identity('ollama-embedding', '1.0.0', 'nomic-embed-text',
                                   'sha256:abc', 768, '1')
        h2 = compute_pin_identity('ollama-embedding', '1.0.0', 'nomic-embed-text',
                                   'sha256:abc', 768, '1')
        assert h1 == h2


# ---------------------------------------------------------------------------
# create_pin
# ---------------------------------------------------------------------------

class TestCreatePin:
    def test_creates_active_pin(self, db):
        pin = create_pin(db, **_stub_pin_kwargs())
        assert pin.status == 'active'
        assert pin.id is not None
        assert pin.id > 0

    def test_pin_fields_stored_correctly(self, db):
        pin = create_pin(db, **_stub_pin_kwargs(
            model_digest='abc123',
            notes='smoke test',
        ))
        assert pin.adapter_name == 'stub_embedding'
        assert pin.adapter_version == '1.0.0'
        assert pin.model_name == 'stub-model'
        assert pin.model_digest == 'abc123'
        assert pin.dimensions == 4
        assert pin.provider_name == 'stub'
        assert pin.pinned_by == 'test-operator'
        assert pin.pin_scope == 'global'
        assert pin.notes == 'smoke test'
        assert pin.embedding_visible_fields_version == EMBEDDING_VISIBLE_FIELDS_VERSION

    def test_pin_identity_is_computed_and_stored(self, db):
        pin = create_pin(db, **_stub_pin_kwargs())
        expected = compute_pin_identity(
            'stub_embedding', '1.0.0', 'stub-model', None, 4,
            EMBEDDING_VISIBLE_FIELDS_VERSION,
        )
        assert pin.pin_identity == expected

    def test_supersedes_previous_active_pin(self, db):
        pin1 = create_pin(db, **_stub_pin_kwargs())
        pin2 = create_pin(db, **_stub_pin_kwargs(model_name='stub-model-v2'))
        assert pin2.status == 'active'
        # Re-read pin1 from DB.
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM embedding_model_pins WHERE id=?", (pin1.id,)).fetchone()
        conn.close()
        assert row['status'] == 'superseded'
        assert row['superseded_at'] is not None
        assert row['superseded_reason'] is not None

    def test_at_most_one_active_per_scope_after_multiple_creates(self, db):
        create_pin(db, **_stub_pin_kwargs())
        create_pin(db, **_stub_pin_kwargs(model_name='v2'))
        create_pin(db, **_stub_pin_kwargs(model_name='v3'))
        conn = sqlite3.connect(db)
        count = conn.execute(
            "SELECT COUNT(*) FROM embedding_model_pins WHERE pin_scope='global' AND status='active'"
        ).fetchone()[0]
        conn.close()
        assert count == 1

    def test_different_scopes_independent(self, db):
        pin_g = create_pin(db, **_stub_pin_kwargs(pin_scope='global'))
        pin_w = create_pin(db, **_stub_pin_kwargs(pin_scope='workflow:ingest'))
        assert pin_g.status == 'active'
        assert pin_w.status == 'active'
        conn = sqlite3.connect(db)
        global_count = conn.execute(
            "SELECT COUNT(*) FROM embedding_model_pins WHERE pin_scope='global' AND status='active'"
        ).fetchone()[0]
        workflow_count = conn.execute(
            "SELECT COUNT(*) FROM embedding_model_pins WHERE pin_scope='workflow:ingest' AND status='active'"
        ).fetchone()[0]
        conn.close()
        assert global_count == 1
        assert workflow_count == 1

    def test_none_model_digest_stored_as_null(self, db):
        pin = create_pin(db, **_stub_pin_kwargs(model_digest=None))
        assert pin.model_digest is None
        conn = sqlite3.connect(db)
        row = conn.execute("SELECT model_digest FROM embedding_model_pins WHERE id=?", (pin.id,)).fetchone()
        conn.close()
        assert row[0] is None


# ---------------------------------------------------------------------------
# get_active_pin
# ---------------------------------------------------------------------------

class TestGetActivePin:
    def test_returns_none_when_no_pin(self, db):
        result = get_active_pin(db, pin_scope='global')
        assert result is None

    def test_returns_active_pin(self, db):
        created = create_pin(db, **_stub_pin_kwargs())
        pin = get_active_pin(db, pin_scope='global')
        assert pin is not None
        assert pin.id == created.id
        assert pin.status == 'active'

    def test_returns_none_after_supersession(self, db):
        pin1 = create_pin(db, **_stub_pin_kwargs())
        supersede_pin(db, pin1.id, reason='manual supersession')
        result = get_active_pin(db, pin_scope='global')
        assert result is None

    def test_scope_isolation(self, db):
        create_pin(db, **_stub_pin_kwargs(pin_scope='global'))
        result = get_active_pin(db, pin_scope='workflow:ingest')
        assert result is None

    def test_returns_latest_after_multiple_creates(self, db):
        create_pin(db, **_stub_pin_kwargs())
        pin2 = create_pin(db, **_stub_pin_kwargs(model_name='v2'))
        pin = get_active_pin(db)
        assert pin is not None
        assert pin.id == pin2.id


# ---------------------------------------------------------------------------
# supersede_pin
# ---------------------------------------------------------------------------

class TestSupersedePIn:
    def test_supersedes_active_pin(self, db):
        pin = create_pin(db, **_stub_pin_kwargs())
        superseded = supersede_pin(db, pin.id, reason='operator decision')
        assert superseded.status == 'superseded'
        assert superseded.superseded_at is not None
        assert superseded.superseded_reason == 'operator decision'

    def test_raises_on_nonexistent_pin(self, db):
        with pytest.raises(ValueError, match="not found"):
            supersede_pin(db, 9999, reason='irrelevant')

    def test_raises_on_already_superseded_pin(self, db):
        pin = create_pin(db, **_stub_pin_kwargs())
        supersede_pin(db, pin.id, reason='first')
        with pytest.raises(ValueError, match="Only 'active'"):
            supersede_pin(db, pin.id, reason='second')


# ---------------------------------------------------------------------------
# list_pins
# ---------------------------------------------------------------------------

class TestListPins:
    def test_empty_list_when_no_pins(self, db):
        pins = list_pins(db)
        assert pins == []

    def test_returns_all_pins_newest_first(self, db):
        p1 = create_pin(db, **_stub_pin_kwargs())
        p2 = create_pin(db, **_stub_pin_kwargs(model_name='v2'))
        pins = list_pins(db)
        assert len(pins) == 2
        assert pins[0].id == p2.id
        assert pins[1].id == p1.id

    def test_scope_isolation(self, db):
        create_pin(db, **_stub_pin_kwargs(pin_scope='global'))
        create_pin(db, **_stub_pin_kwargs(pin_scope='workflow:ingest'))
        global_pins = list_pins(db, pin_scope='global')
        workflow_pins = list_pins(db, pin_scope='workflow:ingest')
        assert len(global_pins) == 1
        assert len(workflow_pins) == 1

    def test_pin_record_to_dict(self, db):
        pin = create_pin(db, **_stub_pin_kwargs())
        d = pin.to_dict()
        assert d['status'] == 'active'
        assert d['adapter_name'] == 'stub_embedding'
        assert d['pin_identity'] == pin.pin_identity

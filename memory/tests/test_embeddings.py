"""Tests for Phase 2B/2C/2D embedding substrate.

Covers: schema v5, migration, governance registration, EmbeddingRow,
embed_event idempotency and invalidation, get_embeddings, get_active_embedding,
promote_embedding lifecycle and audit persistence, pin validation governance,
and governance invariants.
"""
import json
import sqlite3

import pytest

import memory.artifact_governance as gov
from memory import service
from memory.artifact_governance import (
    ArtifactStatus,
    GovernanceInvalidationError,
    GovernancePinError,
    GovernanceSchemaError,
    VALID_ARTIFACT_STATUSES,
    compute_content_hash,
    validate_artifact_table_schema,
)
from memory.embedding_pins import create_pin
from memory.embeddings import (
    EmbeddingRow,
    _REQUIRED_COLUMNS,
    _REQUIRED_INDICES,
    embed_event,
    get_active_embedding,
    get_embeddings,
    invalidate_stale_embeddings,
    promote_embedding,
)
from models.embedding_adapter import StubEmbeddingAdapter


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _db(tmp_path) -> str:
    db = str(tmp_path / 'mem.db')
    service.init_db(db)
    return db


def _add(db, **kw) -> object:
    defaults = dict(
        event_type='hypothesis',
        title='Interest rate divergence',
        summary='Fed holds while ECB cuts — USD strength expected.',
        source='tester',
        confidence=3,
        status='proposed',
        created_by='tester',
    )
    defaults.update(kw)
    return service.add_memory_event(db, **defaults)


def _stub(dimensions: int = 4) -> StubEmbeddingAdapter:
    return StubEmbeddingAdapter(dimensions=dimensions)


def _pin_for(db: str, adapter, pin_scope: str = 'global'):
    """Create a governed pin matching the given adapter. Returns PinRecord."""
    return create_pin(
        db,
        adapter_name=adapter.adapter_name,
        adapter_version=adapter.adapter_version,
        model_name=adapter.model_name,
        model_digest=adapter.model_digest,
        dimensions=adapter.dimensions,
        provider_name=adapter.provider_name,
        pinned_by='test-operator',
        pin_scope=pin_scope,
    )


# ---------------------------------------------------------------------------
# Schema v5: fresh DB
# ---------------------------------------------------------------------------

class TestSchemaV4FreshDB:
    def test_schema_version_is_16(self, tmp_path):
        db = _db(tmp_path)
        conn = sqlite3.connect(db)
        row = conn.execute('SELECT version FROM memory_schema_version').fetchone()
        conn.close()
        assert row[0] == 16

    def test_event_embeddings_table_exists(self, tmp_path):
        db = _db(tmp_path)
        conn = sqlite3.connect(db)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        conn.close()
        assert 'event_embeddings' in tables

    def test_event_embeddings_has_required_columns(self, tmp_path):
        db = _db(tmp_path)
        conn = sqlite3.connect(db)
        cols = {row[1] for row in conn.execute('PRAGMA table_info(event_embeddings)')}
        conn.close()
        assert set(_REQUIRED_COLUMNS).issubset(cols)

    def test_event_embeddings_indices_exist(self, tmp_path):
        db = _db(tmp_path)
        conn = sqlite3.connect(db)
        idx = {row[1] for row in conn.execute('PRAGMA index_list(event_embeddings)')}
        conn.close()
        assert set(_REQUIRED_INDICES).issubset(idx)

    def test_status_defaults_to_candidate(self, tmp_path):
        db = _db(tmp_path)
        event = _add(db)
        adapter = _stub()
        embed_event(db, event, adapter)
        conn = sqlite3.connect(db)
        row = conn.execute(
            'SELECT status FROM event_embeddings ORDER BY id DESC LIMIT 1'
        ).fetchone()
        conn.close()
        assert row[0] == 'candidate'

    def test_generated_at_is_set_on_insert(self, tmp_path):
        db = _db(tmp_path)
        event = _add(db)
        adapter = _stub()
        embed_event(db, event, adapter)
        conn = sqlite3.connect(db)
        row = conn.execute(
            'SELECT generated_at FROM event_embeddings ORDER BY id DESC LIMIT 1'
        ).fetchone()
        conn.close()
        assert row[0] is not None
        assert 'T' in row[0]

    def test_invalidated_at_is_null_on_insert(self, tmp_path):
        db = _db(tmp_path)
        event = _add(db)
        adapter = _stub()
        embed_event(db, event, adapter)
        conn = sqlite3.connect(db)
        row = conn.execute(
            'SELECT invalidated_at, invalidated_reason FROM event_embeddings '
            'ORDER BY id DESC LIMIT 1'
        ).fetchone()
        conn.close()
        assert row[0] is None
        assert row[1] is None

    def test_unique_constraint_on_event_content_producer(self, tmp_path):
        db = _db(tmp_path)
        event = _add(db)
        adapter = _stub()
        embed_event(db, event, adapter)
        # Direct INSERT of duplicate must raise IntegrityError.
        content_hash = compute_content_hash(event.title, event.summary)
        conn = sqlite3.connect(db)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO event_embeddings "
                "(memory_event_id, content_hash, vector_json, dimensions, "
                " model_name, model_version, provider_name, adapter_name, "
                " adapter_version, producer_version, status, generated_at, "
                " provenance_json) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (event.id, content_hash, '[1.0]', 1, 'm', 'v', 'stub', 'a', 'v',
                 adapter.producer_version, 'candidate',
                 '2026-01-01T00:00:00Z', '{}'),
            )
        conn.close()


# ---------------------------------------------------------------------------
# Schema v4: v3 to v4 migration
# ---------------------------------------------------------------------------

_V3_DDL = """
    CREATE TABLE memory_schema_version (version INTEGER NOT NULL);
    INSERT INTO memory_schema_version VALUES (3);
    CREATE TABLE memory_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_type TEXT NOT NULL, title TEXT NOT NULL, summary TEXT NOT NULL,
        evidence TEXT, source TEXT NOT NULL, confidence INTEGER NOT NULL,
        status TEXT NOT NULL, tags_json TEXT NOT NULL DEFAULT '[]',
        related_ids_json TEXT NOT NULL DEFAULT '[]', created_by TEXT NOT NULL,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        version INTEGER NOT NULL DEFAULT 1
    );
    CREATE TABLE memory_revisions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, memory_id INTEGER NOT NULL,
        old_value_json TEXT NOT NULL, new_value_json TEXT NOT NULL,
        reason TEXT NOT NULL, created_at TEXT NOT NULL, created_by TEXT NOT NULL
    );
    CREATE TABLE memory_links (
        id INTEGER PRIMARY KEY AUTOINCREMENT, source_id INTEGER NOT NULL,
        target_id INTEGER NOT NULL, relationship TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(source_id, target_id, relationship)
    );
    CREATE TABLE retrieval_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        query_hash TEXT NOT NULL, session_id TEXT,
        query_json TEXT NOT NULL, scoring_version TEXT NOT NULL,
        scoring_params_json TEXT NOT NULL, result_event_ids_json TEXT NOT NULL,
        result_count INTEGER NOT NULL, executed_at TEXT NOT NULL,
        actor TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'active'
    );
    CREATE INDEX IF NOT EXISTS idx_retrieval_log_status ON retrieval_log(status);
"""


class TestSchemaV4Migration:
    def _v3_db(self, tmp_path) -> str:
        db = str(tmp_path / 'mem_v3.db')
        conn = sqlite3.connect(db)
        conn.executescript(_V3_DDL)
        conn.close()
        return db

    def test_migrate_from_v3_bumps_version_to_16(self, tmp_path):
        db = self._v3_db(tmp_path)
        service.init_db(db)
        conn = sqlite3.connect(db)
        row = conn.execute('SELECT version FROM memory_schema_version').fetchone()
        conn.close()
        assert row[0] == 16

    def test_migrate_from_v3_creates_event_embeddings(self, tmp_path):
        db = self._v3_db(tmp_path)
        service.init_db(db)
        conn = sqlite3.connect(db)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        conn.close()
        assert 'event_embeddings' in tables

    def test_migrate_from_v3_creates_all_indices(self, tmp_path):
        db = self._v3_db(tmp_path)
        service.init_db(db)
        conn = sqlite3.connect(db)
        idx = {row[1] for row in conn.execute('PRAGMA index_list(event_embeddings)')}
        conn.close()
        assert set(_REQUIRED_INDICES).issubset(idx)

    def test_migration_from_v3_is_idempotent(self, tmp_path):
        db = self._v3_db(tmp_path)
        service.init_db(db)
        service.init_db(db)
        conn = sqlite3.connect(db)
        row = conn.execute('SELECT version FROM memory_schema_version').fetchone()
        conn.close()
        assert row[0] == 16

    def test_v3_existing_data_preserved(self, tmp_path):
        db = self._v3_db(tmp_path)
        # Insert a retrieval_log row in the v3 DB before migration.
        conn = sqlite3.connect(db)
        conn.execute(
            "INSERT INTO retrieval_log "
            "(query_hash, query_json, scoring_version, scoring_params_json, "
            " result_event_ids_json, result_count, executed_at, actor, status) "
            "VALUES ('abc', '{}', '1.0.0', '{}', '[]', 0, '2026-01-01T00:00:00Z', "
            "        'tester', 'active')"
        )
        conn.commit()
        conn.close()
        service.init_db(db)
        conn2 = sqlite3.connect(db)
        count = conn2.execute('SELECT COUNT(*) FROM retrieval_log').fetchone()[0]
        conn2.close()
        assert count == 1


# ---------------------------------------------------------------------------
# Governance registration
# ---------------------------------------------------------------------------

class TestGovernanceRegistration:
    def test_event_embeddings_in_governed_tables(self):
        assert 'event_embeddings' in gov._GOVERNED_ARTIFACT_TABLES

    def test_validate_schema_passes_on_event_embeddings(self, tmp_path):
        db = _db(tmp_path)
        conn = sqlite3.connect(db)
        validate_artifact_table_schema(
            conn, 'event_embeddings',
            required_columns=_REQUIRED_COLUMNS,
            required_indices=_REQUIRED_INDICES,
        )
        conn.close()

    def test_mark_invalidated_accepted_for_event_embeddings(self, tmp_path):
        db = _db(tmp_path)
        event = _add(db)
        adapter = _stub()
        row_id = embed_event(db, event, adapter)
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        gov.mark_invalidated(conn, 'event_embeddings', row_id, 'test', '2026-01-01T00:00:00Z')
        conn.commit()
        row = conn.execute(
            'SELECT status FROM event_embeddings WHERE id = ?', (row_id,)
        ).fetchone()
        conn.close()
        assert row['status'] == 'invalidated'

    def test_mark_superseded_accepted_for_event_embeddings(self, tmp_path):
        db = _db(tmp_path)
        event = _add(db)
        adapter = _stub()
        row_id = embed_event(db, event, adapter)
        # Manually promote to active so mark_superseded can run.
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        conn.execute(
            "UPDATE event_embeddings SET status = 'active' WHERE id = ?", (row_id,)
        )
        gov.mark_superseded(conn, 'event_embeddings', row_id, 'model upgraded', '2026-01-01T00:00:00Z')
        conn.commit()
        row = conn.execute(
            'SELECT status FROM event_embeddings WHERE id = ?', (row_id,)
        ).fetchone()
        conn.close()
        assert row['status'] == 'superseded'

    def test_retrieval_log_not_in_governed_tables(self):
        # retrieval_log must remain outside the governed allowlist.
        with pytest.raises(GovernanceSchemaError, match="not in the governed artifact table allowlist"):
            conn = sqlite3.connect(':memory:')
            gov.mark_invalidated(conn, 'retrieval_log', 1, 'reason', '2026-01-01T00:00:00Z')

    def test_valid_artifact_statuses_contains_four(self):
        assert VALID_ARTIFACT_STATUSES == {
            'candidate', 'active', 'superseded', 'invalidated'
        }


# ---------------------------------------------------------------------------
# embed_event: content_hash and vector
# ---------------------------------------------------------------------------

class TestEmbedEvent:
    def test_content_hash_correct(self, tmp_path):
        db = _db(tmp_path)
        event = _add(db)
        adapter = _stub()
        row_id = embed_event(db, event, adapter)
        conn = sqlite3.connect(db)
        row = conn.execute(
            'SELECT content_hash FROM event_embeddings WHERE id = ?', (row_id,)
        ).fetchone()
        conn.close()
        expected = compute_content_hash(event.title, event.summary)
        assert row[0] == expected

    def test_vector_json_preserves_order(self, tmp_path):
        db = _db(tmp_path)
        event = _add(db)
        adapter = _stub(dimensions=6)
        expected_vector = adapter.embed(f"{event.title}\n{event.summary}")
        row_id = embed_event(db, event, adapter)
        conn = sqlite3.connect(db)
        row = conn.execute(
            'SELECT vector_json FROM event_embeddings WHERE id = ?', (row_id,)
        ).fetchone()
        conn.close()
        stored = json.loads(row[0])
        assert stored == expected_vector

    def test_dimensions_stored_correctly(self, tmp_path):
        db = _db(tmp_path)
        event = _add(db)
        adapter = _stub(dimensions=8)
        row_id = embed_event(db, event, adapter)
        conn = sqlite3.connect(db)
        row = conn.execute(
            'SELECT dimensions FROM event_embeddings WHERE id = ?', (row_id,)
        ).fetchone()
        conn.close()
        assert row[0] == 8

    def test_dimensions_mismatch_raises(self, tmp_path):
        db = _db(tmp_path)
        event = _add(db)

        class BadAdapter(StubEmbeddingAdapter):
            @property
            def dimensions(self) -> int:
                return 999  # lies about dimensions

        adapter = BadAdapter(dimensions=4)
        with pytest.raises(ValueError, match="dimensions"):
            embed_event(db, event, adapter)

    def test_provenance_json_stored(self, tmp_path):
        db = _db(tmp_path)
        event = _add(db)
        adapter = _stub()
        row_id = embed_event(db, event, adapter)
        conn = sqlite3.connect(db)
        row = conn.execute(
            'SELECT provenance_json FROM event_embeddings WHERE id = ?', (row_id,)
        ).fetchone()
        conn.close()
        prov = json.loads(row[0])
        assert prov['adapter_name'] == 'stub_embedding'
        assert prov['provider_name'] == 'stub'

    def test_model_digest_is_null_for_stub(self, tmp_path):
        db = _db(tmp_path)
        event = _add(db)
        adapter = _stub()
        row_id = embed_event(db, event, adapter)
        conn = sqlite3.connect(db)
        row = conn.execute(
            'SELECT model_digest FROM event_embeddings WHERE id = ?', (row_id,)
        ).fetchone()
        conn.close()
        assert row[0] is None


# ---------------------------------------------------------------------------
# embed_event: idempotency
# ---------------------------------------------------------------------------

class TestEmbedEventIdempotency:
    def test_same_content_and_producer_returns_existing_id(self, tmp_path):
        db = _db(tmp_path)
        event = _add(db)
        adapter = _stub()
        id1 = embed_event(db, event, adapter)
        id2 = embed_event(db, event, adapter)
        assert id1 == id2

    def test_idempotent_does_not_insert_duplicate(self, tmp_path):
        db = _db(tmp_path)
        event = _add(db)
        adapter = _stub()
        embed_event(db, event, adapter)
        embed_event(db, event, adapter)
        conn = sqlite3.connect(db)
        count = conn.execute(
            'SELECT COUNT(*) FROM event_embeddings WHERE memory_event_id = ?',
            (event.id,)
        ).fetchone()[0]
        conn.close()
        assert count == 1

    def test_new_row_on_content_hash_change(self, tmp_path):
        db = _db(tmp_path)
        event = _add(db, title='Original title', summary='Original summary')
        adapter = _stub()
        id1 = embed_event(db, event, adapter)

        # Simulate a content change: update the event object directly.
        updated = service.update_status(db, event.id, 'accepted', 'approved', 'tester')
        # update_status doesn't change title/summary — manufacture a new event
        # by creating a second event with different content for a clean new-hash test.
        event2 = _add(db, title='Different title', summary='Different summary')
        id2 = embed_event(db, event2, adapter)
        assert id1 != id2

    def test_new_row_on_producer_version_change(self, tmp_path):
        db = _db(tmp_path)
        event = _add(db)
        adapter_v1 = _stub()
        id1 = embed_event(db, event, adapter_v1)

        class V2Adapter(StubEmbeddingAdapter):
            VERSION = '2.0.0'

            @property
            def producer_version(self) -> str:
                return f"{self.VERSION}:{self.MODEL_VERSION}:stub-no-model-digest"

        adapter_v2 = V2Adapter(dimensions=4)
        id2 = embed_event(db, event, adapter_v2)
        assert id1 != id2

    def test_active_row_blocks_new_insert(self, tmp_path):
        """A manually promoted active row prevents a new candidate insert."""
        db = _db(tmp_path)
        event = _add(db)
        adapter = _stub()
        row_id = embed_event(db, event, adapter)
        conn = sqlite3.connect(db)
        conn.execute(
            "UPDATE event_embeddings SET status = 'active' WHERE id = ?", (row_id,)
        )
        conn.commit()
        conn.close()
        returned_id = embed_event(db, event, adapter)
        assert returned_id == row_id


# ---------------------------------------------------------------------------
# invalidate_stale_embeddings
# ---------------------------------------------------------------------------

class TestInvalidateStaleEmbeddings:
    def test_no_op_when_content_hash_matches(self, tmp_path):
        db = _db(tmp_path)
        event = _add(db)
        adapter = _stub()
        embed_event(db, event, adapter)
        count = invalidate_stale_embeddings(db, event)
        assert count == 0

    def test_invalidates_candidate_when_content_changed(self, tmp_path):
        db = _db(tmp_path)
        event = _add(db, title='Old title', summary='Old summary')
        adapter = _stub()
        embed_event(db, event, adapter)

        # Build an event object with different title/summary for the same id.
        from dataclasses import replace
        changed_event = replace(event, title='New title', summary='New summary')
        count = invalidate_stale_embeddings(db, changed_event)
        assert count == 1

        conn = sqlite3.connect(db)
        row = conn.execute(
            'SELECT status FROM event_embeddings WHERE memory_event_id = ?',
            (event.id,)
        ).fetchone()
        conn.close()
        assert row[0] == 'invalidated'

    def test_invalidates_active_when_content_changed(self, tmp_path):
        db = _db(tmp_path)
        event = _add(db, title='Old title', summary='Old summary')
        adapter = _stub()
        row_id = embed_event(db, event, adapter)

        conn = sqlite3.connect(db)
        conn.execute(
            "UPDATE event_embeddings SET status = 'active' WHERE id = ?", (row_id,)
        )
        conn.commit()
        conn.close()

        from dataclasses import replace
        changed_event = replace(event, title='New title', summary='New summary')
        count = invalidate_stale_embeddings(db, changed_event)
        assert count == 1

        conn2 = sqlite3.connect(db)
        row = conn2.execute(
            'SELECT status FROM event_embeddings WHERE id = ?', (row_id,)
        ).fetchone()
        conn2.close()
        assert row[0] == 'invalidated'

    def test_terminal_rows_not_invalidated(self, tmp_path):
        db = _db(tmp_path)
        event = _add(db, title='Old title', summary='Old summary')
        adapter = _stub()
        row_id = embed_event(db, event, adapter)

        # Manually set the row to superseded (terminal).
        conn = sqlite3.connect(db)
        conn.execute(
            "UPDATE event_embeddings SET status = 'superseded', "
            "invalidated_at = '2026-01-01T00:00:00Z', "
            "invalidated_reason = 'pre-existing' WHERE id = ?",
            (row_id,)
        )
        conn.commit()
        conn.close()

        from dataclasses import replace
        changed_event = replace(event, title='New title', summary='New summary')
        count = invalidate_stale_embeddings(db, changed_event)
        assert count == 0

    def test_invalidation_reason_names_old_and_new_hash(self, tmp_path):
        db = _db(tmp_path)
        event = _add(db, title='Old title', summary='Old summary')
        adapter = _stub()
        embed_event(db, event, adapter)
        old_hash = compute_content_hash(event.title, event.summary)

        from dataclasses import replace
        changed_event = replace(event, title='New title', summary='New summary')
        new_hash = compute_content_hash(changed_event.title, changed_event.summary)
        invalidate_stale_embeddings(db, changed_event)

        conn = sqlite3.connect(db)
        row = conn.execute(
            'SELECT invalidated_reason FROM event_embeddings WHERE memory_event_id = ?',
            (event.id,)
        ).fetchone()
        conn.close()
        assert old_hash in row[0]
        assert new_hash in row[0]

    def test_invalidates_multiple_stale_rows(self, tmp_path):
        db = _db(tmp_path)
        event = _add(db, title='Old title', summary='Old summary')

        class AltProducer(StubEmbeddingAdapter):
            VERSION = '2.0.0'

            @property
            def producer_version(self) -> str:
                return f"{self.VERSION}:{self.MODEL_VERSION}:stub-no-model-digest"

        adapter1 = _stub()
        adapter2 = AltProducer(dimensions=4)
        embed_event(db, event, adapter1)
        embed_event(db, event, adapter2)

        from dataclasses import replace
        changed = replace(event, title='New title', summary='New summary')
        count = invalidate_stale_embeddings(db, changed)
        assert count == 2


# ---------------------------------------------------------------------------
# get_embeddings
# ---------------------------------------------------------------------------

class TestGetEmbeddings:
    def test_returns_all_rows_for_event(self, tmp_path):
        db = _db(tmp_path)
        event = _add(db)

        class AltProducer(StubEmbeddingAdapter):
            VERSION = '2.0.0'

            @property
            def producer_version(self) -> str:
                return f"{self.VERSION}:{self.MODEL_VERSION}:stub-no-model-digest"

        embed_event(db, event, _stub())
        embed_event(db, event, AltProducer(dimensions=4))
        rows = get_embeddings(db, event.id)
        assert len(rows) == 2

    def test_returns_empty_list_for_unknown_event(self, tmp_path):
        db = _db(tmp_path)
        rows = get_embeddings(db, 9999)
        assert rows == []

    def test_filters_by_status_candidate(self, tmp_path):
        db = _db(tmp_path)
        event = _add(db)
        adapter = _stub()
        row_id = embed_event(db, event, adapter)

        conn = sqlite3.connect(db)
        conn.execute(
            "UPDATE event_embeddings SET status = 'invalidated', "
            "invalidated_at = '2026-01-01T00:00:00Z', "
            "invalidated_reason = 'test' WHERE id = ?",
            (row_id,)
        )
        conn.commit()
        conn.close()

        candidate_rows = get_embeddings(db, event.id, status='candidate')
        invalidated_rows = get_embeddings(db, event.id, status='invalidated')
        assert len(candidate_rows) == 0
        assert len(invalidated_rows) == 1

    def test_from_row_deserializes_correctly(self, tmp_path):
        db = _db(tmp_path)
        event = _add(db)
        adapter = _stub(dimensions=4)
        embed_event(db, event, adapter)
        rows = get_embeddings(db, event.id)
        assert len(rows) == 1
        r = rows[0]
        assert r.memory_event_id == event.id
        assert r.dimensions == 4
        assert r.status == ArtifactStatus.CANDIDATE
        assert isinstance(r.vector, list)
        assert len(r.vector) == 4


# ---------------------------------------------------------------------------
# get_active_embedding
# ---------------------------------------------------------------------------

class TestGetActiveEmbedding:
    def test_returns_none_when_no_active_row(self, tmp_path):
        db = _db(tmp_path)
        event = _add(db)
        adapter = _stub()
        embed_event(db, event, adapter)
        result = get_active_embedding(db, event.id)
        assert result is None

    def test_returns_none_for_unknown_event(self, tmp_path):
        db = _db(tmp_path)
        assert get_active_embedding(db, 9999) is None

    def test_returns_active_row_when_present(self, tmp_path):
        db = _db(tmp_path)
        event = _add(db)
        adapter = _stub()
        row_id = embed_event(db, event, adapter)

        conn = sqlite3.connect(db)
        conn.execute(
            "UPDATE event_embeddings SET status = 'active' WHERE id = ?", (row_id,)
        )
        conn.commit()
        conn.close()

        result = get_active_embedding(db, event.id)
        assert result is not None
        assert result.id == row_id
        assert result.status == 'active'

    def test_does_not_return_candidate(self, tmp_path):
        db = _db(tmp_path)
        event = _add(db)
        embed_event(db, event, _stub())
        assert get_active_embedding(db, event.id) is None


# ---------------------------------------------------------------------------
# Governance invariants
# ---------------------------------------------------------------------------

class TestGovernanceInvariants:
    def test_embed_event_does_not_mutate_memory_events(self, tmp_path):
        db = _db(tmp_path)
        event = _add(db)
        before = service.get_memory_event(db, event.id)
        adapter = _stub()
        embed_event(db, event, adapter)
        after = service.get_memory_event(db, event.id)
        assert before[0].status == after[0].status
        assert before[0].version == after[0].version
        assert before[0].updated_at == after[0].updated_at

    def test_embed_event_row_is_candidate_not_active(self, tmp_path):
        db = _db(tmp_path)
        event = _add(db)
        row_id = embed_event(db, event, _stub())
        rows = get_embeddings(db, event.id)
        assert all(r.status == 'candidate' for r in rows)

    def test_event_embeddings_not_in_export_memory(self, tmp_path):
        db = _db(tmp_path)
        event = _add(db)
        embed_event(db, event, _stub())
        export = service.export_memory(db)
        keys = set(export.keys())
        assert 'event_embeddings' not in keys
        assert 'retrieval_log' not in keys

    def test_continuity_export_keys_are_canonical_only(self, tmp_path):
        db = _db(tmp_path)
        export = service.export_memory(db)
        assert set(export.keys()) == {'schema_version', 'memory_events', 'memory_revisions', 'memory_links'}


# ---------------------------------------------------------------------------
# Phase 2C: promote_embedding
# ---------------------------------------------------------------------------

class TestPromoteEmbedding:
    def test_promote_sets_status_active(self, tmp_path):
        db = _db(tmp_path)
        event = _add(db)
        adapter = _stub()
        _pin_for(db, adapter)
        row_id = embed_event(db, event, adapter)
        promote_embedding(db, row_id, reason='validated', operator='quant')
        rows = get_embeddings(db, event.id)
        assert rows[0].status == 'active'

    def test_promote_returns_active_embedding_row(self, tmp_path):
        db = _db(tmp_path)
        event = _add(db)
        adapter = _stub()
        _pin_for(db, adapter)
        row_id = embed_event(db, event, adapter)
        result = promote_embedding(db, row_id, reason='validated', operator='quant')
        assert isinstance(result, EmbeddingRow)
        assert result.id == row_id
        assert result.status == 'active'

    def test_promote_supersedes_prior_active_row(self, tmp_path):
        db = _db(tmp_path)
        event = _add(db)
        adapter_v1 = _stub()

        class V2Adapter(StubEmbeddingAdapter):
            VERSION = '2.0.0'
            @property
            def adapter_version(self) -> str:
                return self.VERSION
            @property
            def producer_version(self) -> str:
                return f"{self.VERSION}:{self.MODEL_VERSION}:stub-no-model-digest"

        adapter_v2 = V2Adapter(dimensions=4)
        _pin_for(db, adapter_v1)
        id1 = embed_event(db, event, adapter_v1)
        promote_embedding(db, id1, reason='first', operator='quant')
        _pin_for(db, adapter_v2)
        id2 = embed_event(db, event, adapter_v2)
        promote_embedding(db, id2, reason='upgraded model', operator='quant')

        rows = get_embeddings(db, event.id)
        by_id = {r.id: r for r in rows}
        assert by_id[id1].status == 'superseded'
        assert by_id[id2].status == 'active'

    def test_promote_at_most_one_active_invariant(self, tmp_path):
        """Promoting a candidate supersedes ALL prior active rows, not just one."""
        db = _db(tmp_path)
        event = _add(db)

        class V2Adapter(StubEmbeddingAdapter):
            VERSION = '2.0.0'
            @property
            def adapter_version(self) -> str:
                return self.VERSION
            @property
            def producer_version(self) -> str:
                return f"{self.VERSION}:{self.MODEL_VERSION}:stub-no-model-digest"

        class V3Adapter(StubEmbeddingAdapter):
            VERSION = '3.0.0'
            @property
            def adapter_version(self) -> str:
                return self.VERSION
            @property
            def producer_version(self) -> str:
                return f"{self.VERSION}:{self.MODEL_VERSION}:stub-no-model-digest"

        adapter_v3 = V3Adapter(dimensions=4)
        id1 = embed_event(db, event, _stub())
        id2 = embed_event(db, event, V2Adapter(dimensions=4))
        id3 = embed_event(db, event, adapter_v3)

        # Manually force two active rows (bypassing service layer) to test the invariant.
        conn = sqlite3.connect(db)
        conn.execute("UPDATE event_embeddings SET status='active' WHERE id IN (?,?)", (id1, id2))
        conn.commit()
        conn.close()

        # Pin must match id3 (V3Adapter) at promotion time.
        _pin_for(db, adapter_v3)
        promote_embedding(db, id3, reason='new best', operator='quant')

        rows = get_embeddings(db, event.id)
        active = [r for r in rows if r.status == 'active']
        superseded = [r for r in rows if r.status == 'superseded']
        assert len(active) == 1
        assert active[0].id == id3
        assert len(superseded) == 2

    def test_promote_does_not_affect_other_events_active_rows(self, tmp_path):
        db = _db(tmp_path)
        adapter = _stub()
        _pin_for(db, adapter)
        event_a = _add(db, title='Event A', summary='Summary A')
        event_b = _add(db, title='Event B', summary='Summary B')
        id_a = embed_event(db, event_a, adapter)
        id_b = embed_event(db, event_b, adapter)
        promote_embedding(db, id_a, reason='ok', operator='quant')
        promote_embedding(db, id_b, reason='ok', operator='quant')

        rows_a = get_embeddings(db, event_a.id)
        rows_b = get_embeddings(db, event_b.id)
        assert rows_a[0].status == 'active'
        assert rows_b[0].status == 'active'

    def test_promote_rejects_already_active_row(self, tmp_path):
        db = _db(tmp_path)
        event = _add(db)
        adapter = _stub()
        _pin_for(db, adapter)
        row_id = embed_event(db, event, adapter)
        promote_embedding(db, row_id, reason='first', operator='quant')
        with pytest.raises(GovernanceInvalidationError, match="Only 'candidate'"):
            promote_embedding(db, row_id, reason='again', operator='quant')

    def test_promote_rejects_superseded_row(self, tmp_path):
        db = _db(tmp_path)
        event = _add(db)

        class V2Adapter(StubEmbeddingAdapter):
            VERSION = '2.0.0'
            @property
            def adapter_version(self) -> str:
                return self.VERSION
            @property
            def producer_version(self) -> str:
                return f"{self.VERSION}:{self.MODEL_VERSION}:stub-no-model-digest"

        adapter_v1 = _stub()
        adapter_v2 = V2Adapter(dimensions=4)
        _pin_for(db, adapter_v1)
        id1 = embed_event(db, event, adapter_v1)
        id2 = embed_event(db, event, adapter_v2)
        promote_embedding(db, id1, reason='first', operator='quant')
        _pin_for(db, adapter_v2)
        promote_embedding(db, id2, reason='upgrade', operator='quant')
        with pytest.raises(GovernanceInvalidationError, match="Only 'candidate'"):
            promote_embedding(db, id1, reason='retry', operator='quant')

    def test_promote_rejects_invalidated_row(self, tmp_path):
        db = _db(tmp_path)
        event = _add(db)
        row_id = embed_event(db, event, _stub())
        conn = sqlite3.connect(db)
        conn.execute(
            "UPDATE event_embeddings SET status='invalidated', "
            "invalidated_at='2026-01-01T00:00:00Z', invalidated_reason='test' WHERE id=?",
            (row_id,)
        )
        conn.commit()
        conn.close()
        with pytest.raises(GovernanceInvalidationError, match="Only 'candidate'"):
            promote_embedding(db, row_id, reason='try', operator='quant')

    def test_promote_raises_on_unknown_id(self, tmp_path):
        db = _db(tmp_path)
        with pytest.raises(GovernanceInvalidationError, match="not found"):
            promote_embedding(db, 9999, reason='ok', operator='quant')

    def test_promote_rejects_empty_reason(self, tmp_path):
        db = _db(tmp_path)
        event = _add(db)
        row_id = embed_event(db, event, _stub())
        with pytest.raises(ValueError, match="reason"):
            promote_embedding(db, row_id, reason='', operator='quant')

    def test_promote_rejects_whitespace_reason(self, tmp_path):
        db = _db(tmp_path)
        event = _add(db)
        row_id = embed_event(db, event, _stub())
        with pytest.raises(ValueError, match="reason"):
            promote_embedding(db, row_id, reason='   ', operator='quant')

    def test_promote_rejects_empty_operator(self, tmp_path):
        db = _db(tmp_path)
        event = _add(db)
        row_id = embed_event(db, event, _stub())
        with pytest.raises(ValueError, match="operator"):
            promote_embedding(db, row_id, reason='ok', operator='')

    def test_promote_does_not_mutate_memory_events(self, tmp_path):
        db = _db(tmp_path)
        event = _add(db)
        adapter = _stub()
        before = service.get_memory_event(db, event.id)
        _pin_for(db, adapter)
        row_id = embed_event(db, event, adapter)
        promote_embedding(db, row_id, reason='validated', operator='quant')
        after = service.get_memory_event(db, event.id)
        assert before[0].status == after[0].status
        assert before[0].version == after[0].version
        assert before[0].updated_at == after[0].updated_at

    def test_get_active_embedding_returns_promoted_row(self, tmp_path):
        db = _db(tmp_path)
        event = _add(db)
        adapter = _stub()
        _pin_for(db, adapter)
        row_id = embed_event(db, event, adapter)
        promote_embedding(db, row_id, reason='validated', operator='quant')
        result = get_active_embedding(db, event.id)
        assert result is not None
        assert result.id == row_id
        assert result.status == 'active'

    def test_get_active_embedding_returns_most_recent_after_supersede(self, tmp_path):
        db = _db(tmp_path)
        event = _add(db)

        class V2Adapter(StubEmbeddingAdapter):
            VERSION = '2.0.0'
            @property
            def adapter_version(self) -> str:
                return self.VERSION
            @property
            def producer_version(self) -> str:
                return f"{self.VERSION}:{self.MODEL_VERSION}:stub-no-model-digest"

        adapter_v1 = _stub()
        adapter_v2 = V2Adapter(dimensions=4)
        _pin_for(db, adapter_v1)
        id1 = embed_event(db, event, adapter_v1)
        promote_embedding(db, id1, reason='first', operator='quant')
        _pin_for(db, adapter_v2)
        id2 = embed_event(db, event, adapter_v2)
        promote_embedding(db, id2, reason='upgrade', operator='quant')
        result = get_active_embedding(db, event.id)
        assert result is not None
        assert result.id == id2

    # Audit metadata tests
    def test_audit_persists_operator(self, tmp_path):
        db = _db(tmp_path)
        event = _add(db)
        adapter = _stub()
        _pin_for(db, adapter)
        row_id = embed_event(db, event, adapter)
        result = promote_embedding(db, row_id, reason='validated', operator='risk-engine')
        prov = json.loads(result.provenance_json)
        assert prov['promotion']['operator'] == 'risk-engine'

    def test_audit_persists_reason(self, tmp_path):
        db = _db(tmp_path)
        event = _add(db)
        adapter = _stub()
        _pin_for(db, adapter)
        row_id = embed_event(db, event, adapter)
        result = promote_embedding(db, row_id, reason='model validated by quant', operator='quant')
        prov = json.loads(result.provenance_json)
        assert prov['promotion']['reason'] == 'model validated by quant'

    def test_audit_persists_promoted_at(self, tmp_path):
        db = _db(tmp_path)
        event = _add(db)
        adapter = _stub()
        _pin_for(db, adapter)
        row_id = embed_event(db, event, adapter)
        result = promote_embedding(db, row_id, reason='ok', operator='quant')
        prov = json.loads(result.provenance_json)
        assert 'promoted_at' in prov['promotion']
        assert 'T' in prov['promotion']['promoted_at']

    def test_audit_persists_previous_active_ids_empty_when_no_prior(self, tmp_path):
        db = _db(tmp_path)
        event = _add(db)
        adapter = _stub()
        _pin_for(db, adapter)
        row_id = embed_event(db, event, adapter)
        result = promote_embedding(db, row_id, reason='ok', operator='quant')
        prov = json.loads(result.provenance_json)
        assert prov['promotion']['previous_active_embedding_ids'] == []

    def test_audit_persists_previous_active_ids_with_superseded(self, tmp_path):
        db = _db(tmp_path)
        event = _add(db)

        class V2Adapter(StubEmbeddingAdapter):
            VERSION = '2.0.0'
            @property
            def adapter_version(self) -> str:
                return self.VERSION
            @property
            def producer_version(self) -> str:
                return f"{self.VERSION}:{self.MODEL_VERSION}:stub-no-model-digest"

        adapter_v1 = _stub()
        adapter_v2 = V2Adapter(dimensions=4)
        _pin_for(db, adapter_v1)
        id1 = embed_event(db, event, adapter_v1)
        promote_embedding(db, id1, reason='first', operator='quant')
        _pin_for(db, adapter_v2)
        id2 = embed_event(db, event, adapter_v2)
        result = promote_embedding(db, id2, reason='upgrade', operator='quant')
        prov = json.loads(result.provenance_json)
        assert id1 in prov['promotion']['previous_active_embedding_ids']

    def test_audit_governance_action_field(self, tmp_path):
        db = _db(tmp_path)
        event = _add(db)
        adapter = _stub()
        _pin_for(db, adapter)
        row_id = embed_event(db, event, adapter)
        result = promote_embedding(db, row_id, reason='ok', operator='quant')
        prov = json.loads(result.provenance_json)
        assert prov['promotion']['governance_action'] == 'promote_embedding'

    def test_audit_preserves_generation_provenance_fields(self, tmp_path):
        db = _db(tmp_path)
        event = _add(db)
        adapter = _stub()
        _pin_for(db, adapter)
        row_id = embed_event(db, event, adapter)
        result = promote_embedding(db, row_id, reason='ok', operator='quant')
        prov = json.loads(result.provenance_json)
        # Generation provenance must still be present after promotion.
        assert 'adapter_name' in prov
        assert prov['adapter_name'] == 'stub_embedding'
        assert 'provider_name' in prov
        assert 'dimensions' in prov
        assert 'promotion' in prov  # audit sub-key added alongside generation fields

    def test_audit_promoted_embedding_id_field(self, tmp_path):
        db = _db(tmp_path)
        event = _add(db)
        adapter = _stub()
        _pin_for(db, adapter)
        row_id = embed_event(db, event, adapter)
        result = promote_embedding(db, row_id, reason='ok', operator='quant')
        prov = json.loads(result.provenance_json)
        assert prov['promotion']['promoted_embedding_id'] == row_id

    def test_audit_memory_event_id_field(self, tmp_path):
        db = _db(tmp_path)
        event = _add(db)
        adapter = _stub()
        _pin_for(db, adapter)
        row_id = embed_event(db, event, adapter)
        result = promote_embedding(db, row_id, reason='ok', operator='quant')
        prov = json.loads(result.provenance_json)
        assert prov['promotion']['memory_event_id'] == event.id

    def test_audit_persists_pin_id_and_scope(self, tmp_path):
        db = _db(tmp_path)
        event = _add(db)
        adapter = _stub()
        pin = _pin_for(db, adapter)
        row_id = embed_event(db, event, adapter)
        result = promote_embedding(db, row_id, reason='ok', operator='quant')
        prov = json.loads(result.provenance_json)
        assert prov['promotion']['pin_id'] == pin.id
        assert prov['promotion']['pin_scope'] == 'global'
        assert prov['promotion']['pin_identity'] == pin.pin_identity


# ---------------------------------------------------------------------------
# Phase 2D: promote_embedding pin validation
# ---------------------------------------------------------------------------

class TestPromoteEmbeddingPinValidation:
    """
    User-specified acceptance tests for pin validation in promote_embedding().

    Invariants under test:
    - candidate generated under old pin fails promotion after pin supersession
    - old active embedding remains active after pin supersession (no auto-invalidation)
    - pin identity changes when embedding_visible_fields_version changes
    - pin identity stable under provenance_json ordering differences
    - promote_embedding validates CURRENT active pin, not historical pin snapshot
    - stale candidate remains candidate after failed promotion (not auto-invalidated)
    - no automatic invalidation triggered by pin changes
    """

    def test_candidate_generated_under_old_pin_fails_promotion_after_supersession(self, tmp_path):
        """
        Candidate embedded under pin v1 must fail promotion once pin is superseded
        by pin v2 (different adapter version).
        """
        db = _db(tmp_path)
        event = _add(db)
        adapter_v1 = _stub()

        class V2Adapter(StubEmbeddingAdapter):
            VERSION = '2.0.0'
            @property
            def adapter_version(self) -> str:
                return self.VERSION
            @property
            def producer_version(self) -> str:
                return f"{self.VERSION}:{self.MODEL_VERSION}:stub-no-model-digest"

        adapter_v2 = V2Adapter(dimensions=4)

        _pin_for(db, adapter_v1)          # set pin for v1
        row_id = embed_event(db, event, adapter_v1)  # generate under pin v1
        _pin_for(db, adapter_v2)          # supersede v1 pin; v2 now active

        with pytest.raises(GovernancePinError):
            promote_embedding(db, row_id, reason='ok', operator='quant')

    def test_old_active_embedding_remains_active_after_pin_supersession(self, tmp_path):
        """
        An active embedding must NOT be auto-invalidated when the pin is superseded.
        Pin changes do not affect existing active artifacts.
        """
        db = _db(tmp_path)
        event = _add(db)
        adapter_v1 = _stub()

        class V2Adapter(StubEmbeddingAdapter):
            VERSION = '2.0.0'
            @property
            def adapter_version(self) -> str:
                return self.VERSION
            @property
            def producer_version(self) -> str:
                return f"{self.VERSION}:{self.MODEL_VERSION}:stub-no-model-digest"

        _pin_for(db, adapter_v1)
        row_id = embed_event(db, event, adapter_v1)
        promote_embedding(db, row_id, reason='initial', operator='quant')

        # Supersede pin — active embedding must remain active.
        _pin_for(db, V2Adapter(dimensions=4))

        rows = get_embeddings(db, event.id, status='active')
        assert len(rows) == 1
        assert rows[0].id == row_id
        assert rows[0].status == 'active'

    def test_no_automatic_invalidation_triggered_by_pin_change(self, tmp_path):
        """
        Changing (superseding) the active pin must not automatically invalidate any
        candidate or active embedding rows.
        """
        db = _db(tmp_path)
        event = _add(db)
        adapter_v1 = _stub()

        class V2Adapter(StubEmbeddingAdapter):
            VERSION = '2.0.0'
            @property
            def adapter_version(self) -> str:
                return self.VERSION
            @property
            def producer_version(self) -> str:
                return f"{self.VERSION}:{self.MODEL_VERSION}:stub-no-model-digest"

        _pin_for(db, adapter_v1)
        embed_event(db, event, adapter_v1)  # candidate row under pin v1

        before = get_embeddings(db, event.id)
        assert all(r.status == 'candidate' for r in before)

        _pin_for(db, V2Adapter(dimensions=4))  # change pin

        after = get_embeddings(db, event.id)
        # No automatic invalidation — row still candidate.
        assert all(r.status == 'candidate' for r in after)

    def test_stale_candidate_remains_candidate_after_failed_promotion(self, tmp_path):
        """
        A candidate that fails pin validation must remain in 'candidate' status.
        It must NOT be auto-invalidated by the failed promotion attempt.
        """
        db = _db(tmp_path)
        event = _add(db)
        adapter_v1 = _stub()

        class V2Adapter(StubEmbeddingAdapter):
            VERSION = '2.0.0'
            @property
            def adapter_version(self) -> str:
                return self.VERSION
            @property
            def producer_version(self) -> str:
                return f"{self.VERSION}:{self.MODEL_VERSION}:stub-no-model-digest"

        _pin_for(db, adapter_v1)
        row_id = embed_event(db, event, adapter_v1)
        _pin_for(db, V2Adapter(dimensions=4))  # change pin after embedding

        with pytest.raises(GovernancePinError):
            promote_embedding(db, row_id, reason='ok', operator='quant')

        # Candidate must still be in 'candidate' status after failed promotion.
        rows = get_embeddings(db, event.id, status='candidate')
        assert len(rows) == 1
        assert rows[0].id == row_id
        assert rows[0].status == 'candidate'

    def test_promote_validates_current_pin_not_historical_pin(self, tmp_path):
        """
        promote_embedding() must validate against the CURRENT active pin,
        not the pin that was active at generation time.
        """
        db = _db(tmp_path)
        event = _add(db)
        adapter_v1 = _stub()

        class V2Adapter(StubEmbeddingAdapter):
            VERSION = '2.0.0'
            @property
            def adapter_version(self) -> str:
                return self.VERSION
            @property
            def producer_version(self) -> str:
                return f"{self.VERSION}:{self.MODEL_VERSION}:stub-no-model-digest"

        _pin_for(db, adapter_v1)              # pin v1 active at generation time
        row_id = embed_event(db, event, adapter_v1)  # generated under pin v1
        _pin_for(db, V2Adapter(dimensions=4))  # pin v2 is now the CURRENT pin

        # Promotion validates against CURRENT pin (v2), not generation-time pin (v1).
        # candidate was made with v1 identity → mismatch → GovernancePinError
        with pytest.raises(GovernancePinError):
            promote_embedding(db, row_id, reason='ok', operator='quant')

    def test_promotion_succeeds_when_candidate_matches_current_pin(self, tmp_path):
        """
        A candidate generated under the current active pin must succeed promotion.
        This is the happy path for governed embedding activation.
        """
        db = _db(tmp_path)
        event = _add(db)
        adapter = _stub()
        _pin_for(db, adapter)
        row_id = embed_event(db, event, adapter)
        result = promote_embedding(db, row_id, reason='validated', operator='quant')
        assert result.status == 'active'

    def test_raises_when_no_active_pin_exists(self, tmp_path):
        """
        promote_embedding() must raise GovernancePinError when no active pin
        exists for the candidate's scope.
        """
        db = _db(tmp_path)
        event = _add(db)
        adapter = _stub()
        # No pin created — promote should fail immediately.
        row_id = embed_event(db, event, adapter)
        with pytest.raises(GovernancePinError, match="no active pin"):
            promote_embedding(db, row_id, reason='ok', operator='quant')

    def test_pin_identity_changes_when_evfv_changes(self, tmp_path):
        """
        A pin created with a different embedding_visible_fields_version must have
        a different pin_identity — confirming the version is part of the identity hash.
        """
        from memory.artifact_governance import compute_pin_identity
        h1 = compute_pin_identity('stub', '1.0.0', 'model', None, 4, '1')
        h2 = compute_pin_identity('stub', '1.0.0', 'model', None, 4, '2')
        assert h1 != h2

    def test_pin_identity_stable_under_provenance_json_ordering(self, tmp_path):
        """
        compute_pin_identity must be stable regardless of call-site argument
        ordering — it is a pure function of its inputs.
        """
        from memory.artifact_governance import compute_pin_identity
        h1 = compute_pin_identity('ollama-embedding', '1.0.0',
                                   'nomic-embed-text', 'sha256:abc', 768, '1')
        h2 = compute_pin_identity('ollama-embedding', '1.0.0',
                                   'nomic-embed-text', 'sha256:abc', 768, '1')
        assert h1 == h2

    def test_promotion_fails_with_wrong_scope_pin(self, tmp_path):
        """
        A candidate embedded with pin_scope='global' must fail if the only active pin
        is for scope='workflow:ingest'.
        """
        db = _db(tmp_path)
        event = _add(db)
        adapter = _stub()
        # Create pin only for workflow scope, not global.
        _pin_for(db, adapter, pin_scope='workflow:ingest')
        # embed with default scope='global' (no global pin active)
        row_id = embed_event(db, event, adapter, pin_scope='global')
        with pytest.raises(GovernancePinError, match="no active pin"):
            promote_embedding(db, row_id, reason='ok', operator='quant')

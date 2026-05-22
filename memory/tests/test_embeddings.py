"""Tests for Phase 2B embedding substrate.

Covers: schema v4, migration, governance registration, EmbeddingRow,
embed_event idempotency and invalidation, get_embeddings, get_active_embedding,
and governance invariants (no memory_events mutation, no continuity bundle inclusion).
"""
import json
import sqlite3

import pytest

import memory.artifact_governance as gov
from memory import service
from memory.artifact_governance import (
    ArtifactStatus,
    GovernanceSchemaError,
    VALID_ARTIFACT_STATUSES,
    compute_content_hash,
    validate_artifact_table_schema,
)
from memory.embeddings import (
    EmbeddingRow,
    _REQUIRED_COLUMNS,
    _REQUIRED_INDICES,
    embed_event,
    get_active_embedding,
    get_embeddings,
    invalidate_stale_embeddings,
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


# ---------------------------------------------------------------------------
# Schema v4: fresh DB
# ---------------------------------------------------------------------------

class TestSchemaV4FreshDB:
    def test_schema_version_is_4(self, tmp_path):
        db = _db(tmp_path)
        conn = sqlite3.connect(db)
        row = conn.execute('SELECT version FROM memory_schema_version').fetchone()
        conn.close()
        assert row[0] == 4

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

    def test_migrate_from_v3_bumps_version_to_4(self, tmp_path):
        db = self._v3_db(tmp_path)
        service.init_db(db)
        conn = sqlite3.connect(db)
        row = conn.execute('SELECT version FROM memory_schema_version').fetchone()
        conn.close()
        assert row[0] == 4

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
        assert row[0] == 4

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

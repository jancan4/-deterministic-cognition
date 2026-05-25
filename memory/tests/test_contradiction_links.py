"""Tests for Phase 4A: contradiction link provenance (schema v9)."""
import sqlite3

import pytest

from memory.service import (
    NotFoundError,
    ValidationError,
    add_memory_event,
    create_contradiction_link,
    init_db,
    link_memory_events,
    retract_contradiction_link,
)
from memory.governance import detect_conflicts


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / 'contradiction_test.db')
    init_db(path)
    return path


def _add(db, **kw):
    defaults = dict(
        event_type='hypothesis',
        title='Test',
        summary='Test summary',
        source='test',
        confidence=3,
        status='active',
        created_by='tester',
    )
    defaults.update(kw)
    return add_memory_event(db, **defaults)


def _raw(db, sql, params=()):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    row = conn.execute(sql, params).fetchone()
    conn.close()
    return row


# ---------------------------------------------------------------------------
# Schema v9 migration
# ---------------------------------------------------------------------------

class TestSchemaV10Migration:
    def test_fresh_db_has_schema_version_16(self, db):
        conn = sqlite3.connect(db)
        version = conn.execute('SELECT version FROM memory_schema_version').fetchone()[0]
        conn.close()
        assert version == 16

    def test_fresh_db_memory_links_has_v8_columns(self, db):
        conn = sqlite3.connect(db)
        cols = {r[1] for r in conn.execute('PRAGMA table_info(memory_links)')}
        conn.close()
        required = {
            'id', 'source_id', 'target_id', 'relationship', 'created_at',
            'created_by', 'reason', 'link_confidence', 'link_metadata_json',
            'status', 'retracted_at', 'retracted_reason', 'retracted_by',
        }
        assert required <= cols

    def test_v8_indices_exist(self, db):
        conn = sqlite3.connect(db)
        indices = {r[1] for r in conn.execute('PRAGMA index_list(memory_links)')}
        conn.close()
        assert 'idx_links_status' in indices
        assert 'idx_links_contradicts' in indices

    def test_v7_db_migrates_to_v9(self, tmp_path):
        """A DB at v7 should be upgraded to v9 by init_db()."""
        from memory.service import _connect
        db_path = str(tmp_path / 'v7.db')
        conn = _connect(db_path)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS memory_schema_version (version INTEGER NOT NULL);
            INSERT INTO memory_schema_version (version) VALUES (7);
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
                target_id INTEGER NOT NULL, relationship TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE (source_id, target_id, relationship)
            );
            CREATE TABLE IF NOT EXISTS retrieval_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT, query_hash TEXT NOT NULL,
                session_id TEXT, query_json TEXT NOT NULL, scoring_version TEXT NOT NULL,
                scoring_params_json TEXT NOT NULL, result_event_ids_json TEXT NOT NULL,
                result_count INTEGER NOT NULL, executed_at TEXT NOT NULL,
                actor TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active',
                semantic_mode TEXT NOT NULL DEFAULT 'none',
                semantic_provenance_json TEXT
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
            CREATE TABLE IF NOT EXISTS embedding_model_pins (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pin_scope TEXT NOT NULL DEFAULT 'global',
                adapter_name TEXT NOT NULL, adapter_version TEXT NOT NULL,
                model_name TEXT NOT NULL, model_digest TEXT, dimensions INTEGER NOT NULL,
                embedding_visible_fields_version TEXT NOT NULL DEFAULT '1',
                pin_identity TEXT NOT NULL, provider_name TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active', pinned_at TEXT NOT NULL,
                pinned_by TEXT NOT NULL, superseded_at TEXT, superseded_reason TEXT, notes TEXT
            );
            CREATE TABLE IF NOT EXISTS context_assembly_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                assembly_hash TEXT NOT NULL UNIQUE, session_id TEXT NOT NULL,
                assembly_version TEXT NOT NULL, assembled_at TEXT NOT NULL,
                db_path TEXT NOT NULL, policy_json TEXT NOT NULL,
                query_vector_hash TEXT, query_vector_provenance_json TEXT,
                entries_accepted INTEGER NOT NULL, entries_rejected_budget INTEGER NOT NULL DEFAULT 0,
                entries_rejected_filter INTEGER NOT NULL DEFAULT 0,
                char_budget_used INTEGER NOT NULL, char_budget_limit INTEGER NOT NULL,
                compression_mode TEXT NOT NULL DEFAULT 'none',
                assembly_snapshot_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active', superseded_at TEXT, superseded_reason TEXT
            );
        """)
        conn.commit()
        conn.close()

        init_db(db_path)

        conn = sqlite3.connect(db_path)
        version = conn.execute('SELECT version FROM memory_schema_version').fetchone()[0]
        cols = {r[1] for r in conn.execute('PRAGMA table_info(memory_links)')}
        indices = {r[1] for r in conn.execute('PRAGMA index_list(memory_links)')}
        conn.close()

        assert version == 16
        assert 'status' in cols
        assert 'created_by' in cols
        assert 'idx_links_status' in indices
        assert 'idx_links_contradicts' in indices

    def test_migration_idempotent(self, db):
        init_db(db)  # second call
        conn = sqlite3.connect(db)
        version = conn.execute('SELECT version FROM memory_schema_version').fetchone()[0]
        conn.close()
        assert version == 16

    def test_existing_links_backfilled_active(self, tmp_path):
        """Links created before v8 migration must have status='active' after migration."""
        from memory.service import _connect
        db_path = str(tmp_path / 'backfill_test.db')
        conn = _connect(db_path)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS memory_schema_version (version INTEGER NOT NULL);
            INSERT INTO memory_schema_version (version) VALUES (7);
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
                target_id INTEGER NOT NULL, relationship TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE (source_id, target_id, relationship)
            );
        """)
        # Insert a pre-v8 link (no status column yet)
        conn.execute(
            "INSERT INTO memory_links (source_id, target_id, relationship, created_at)"
            " VALUES (1, 2, 'contradicts', '2026-01-01T00:00:00Z')"
        )
        conn.commit()
        conn.close()

        # Now run init_db which will migrate to v8
        # First finish setting up the required tables so init_db doesn't crash
        conn = _connect(db_path)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS retrieval_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT, query_hash TEXT NOT NULL,
                session_id TEXT, query_json TEXT NOT NULL, scoring_version TEXT NOT NULL,
                scoring_params_json TEXT NOT NULL, result_event_ids_json TEXT NOT NULL,
                result_count INTEGER NOT NULL, executed_at TEXT NOT NULL,
                actor TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active',
                semantic_mode TEXT NOT NULL DEFAULT 'none', semantic_provenance_json TEXT
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
            CREATE TABLE IF NOT EXISTS embedding_model_pins (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pin_scope TEXT NOT NULL DEFAULT 'global',
                adapter_name TEXT NOT NULL, adapter_version TEXT NOT NULL,
                model_name TEXT NOT NULL, model_digest TEXT, dimensions INTEGER NOT NULL,
                embedding_visible_fields_version TEXT NOT NULL DEFAULT '1',
                pin_identity TEXT NOT NULL, provider_name TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active', pinned_at TEXT NOT NULL,
                pinned_by TEXT NOT NULL, superseded_at TEXT, superseded_reason TEXT, notes TEXT
            );
            CREATE TABLE IF NOT EXISTS context_assembly_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                assembly_hash TEXT NOT NULL UNIQUE, session_id TEXT NOT NULL,
                assembly_version TEXT NOT NULL, assembled_at TEXT NOT NULL,
                db_path TEXT NOT NULL, policy_json TEXT NOT NULL,
                query_vector_hash TEXT, query_vector_provenance_json TEXT,
                entries_accepted INTEGER NOT NULL, entries_rejected_budget INTEGER NOT NULL DEFAULT 0,
                entries_rejected_filter INTEGER NOT NULL DEFAULT 0,
                char_budget_used INTEGER NOT NULL, char_budget_limit INTEGER NOT NULL,
                compression_mode TEXT NOT NULL DEFAULT 'none',
                assembly_snapshot_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active', superseded_at TEXT, superseded_reason TEXT
            );
        """)
        conn.commit()
        conn.close()

        init_db(db_path)

        conn = sqlite3.connect(db_path)
        row = conn.execute('SELECT status FROM memory_links WHERE source_id=1').fetchone()
        conn.close()
        assert row[0] == 'active'


# ---------------------------------------------------------------------------
# create_contradiction_link
# ---------------------------------------------------------------------------

class TestCreateContradictionLink:
    def test_creates_active_link(self, db):
        e1 = _add(db, title='E1')
        e2 = _add(db, title='E2')
        link = create_contradiction_link(
            db, e1.id, e2.id,
            created_by='operator', reason='E1 and E2 are mutually exclusive',
            link_confidence=4,
        )
        assert link.status == 'active'
        assert link.id is not None and link.id > 0

    def test_stores_provenance_fields(self, db):
        e1 = _add(db, title='E1')
        e2 = _add(db, title='E2')
        meta = {'detection_method': 'manual', 'session': 'abc123'}
        link = create_contradiction_link(
            db, e1.id, e2.id,
            created_by='quant-lead',
            reason='Conflicting regime signals',
            link_confidence=3,
            link_metadata=meta,
        )
        assert link.created_by == 'quant-lead'
        assert link.reason == 'Conflicting regime signals'
        assert link.link_confidence == 3
        assert link.relationship == 'contradicts'
        assert link.link_metadata_json is not None
        import json
        assert json.loads(link.link_metadata_json) == meta

    def test_null_metadata_stored_as_null(self, db):
        e1 = _add(db, title='E1')
        e2 = _add(db, title='E2')
        link = create_contradiction_link(
            db, e1.id, e2.id, created_by='op', reason='conflict', link_confidence=2,
        )
        assert link.link_metadata_json is None

    def test_requires_created_by_non_empty(self, db):
        e1 = _add(db, title='E1')
        e2 = _add(db, title='E2')
        with pytest.raises(ValidationError, match="created_by"):
            create_contradiction_link(
                db, e1.id, e2.id, created_by='', reason='conflict', link_confidence=3,
            )

    def test_requires_reason_non_empty(self, db):
        e1 = _add(db, title='E1')
        e2 = _add(db, title='E2')
        with pytest.raises(ValidationError, match="reason"):
            create_contradiction_link(
                db, e1.id, e2.id, created_by='op', reason='', link_confidence=3,
            )

    def test_validates_confidence_range_low(self, db):
        e1 = _add(db, title='E1')
        e2 = _add(db, title='E2')
        with pytest.raises(ValidationError, match="confidence"):
            create_contradiction_link(
                db, e1.id, e2.id, created_by='op', reason='r', link_confidence=0,
            )

    def test_validates_confidence_range_high(self, db):
        e1 = _add(db, title='E1')
        e2 = _add(db, title='E2')
        with pytest.raises(ValidationError, match="confidence"):
            create_contradiction_link(
                db, e1.id, e2.id, created_by='op', reason='r', link_confidence=6,
            )

    def test_rejects_duplicate_active_contradiction_link(self, db):
        e1 = _add(db, title='E1')
        e2 = _add(db, title='E2')
        create_contradiction_link(
            db, e1.id, e2.id, created_by='op', reason='first', link_confidence=3,
        )
        with pytest.raises(ValidationError, match="already exists"):
            create_contradiction_link(
                db, e1.id, e2.id, created_by='op', reason='second', link_confidence=3,
            )

    def test_raises_if_source_event_not_active(self, db):
        e1 = _add(db, title='E1', status='proposed')
        e2 = _add(db, title='E2')
        with pytest.raises(ValidationError, match="active or accepted"):
            create_contradiction_link(
                db, e1.id, e2.id, created_by='op', reason='r', link_confidence=3,
            )

    def test_raises_if_target_event_not_active(self, db):
        e1 = _add(db, title='E1')
        e2 = _add(db, title='E2', status='deprecated')
        with pytest.raises(ValidationError, match="active or accepted"):
            create_contradiction_link(
                db, e1.id, e2.id, created_by='op', reason='r', link_confidence=3,
            )

    def test_accepted_status_events_are_allowed(self, db):
        e1 = _add(db, title='E1', status='accepted')
        e2 = _add(db, title='E2', status='accepted')
        link = create_contradiction_link(
            db, e1.id, e2.id, created_by='op', reason='r', link_confidence=3,
        )
        assert link.status == 'active'

    def test_raises_if_source_not_found(self, db):
        e2 = _add(db, title='E2')
        with pytest.raises(NotFoundError):
            create_contradiction_link(
                db, 9999, e2.id, created_by='op', reason='r', link_confidence=3,
            )

    def test_raises_if_target_not_found(self, db):
        e1 = _add(db, title='E1')
        with pytest.raises(NotFoundError):
            create_contradiction_link(
                db, e1.id, 9999, created_by='op', reason='r', link_confidence=3,
            )

    def test_no_memory_event_mutation_on_create(self, db):
        e1 = _add(db, title='E1')
        e2 = _add(db, title='E2')
        before1 = _raw(db, 'SELECT version, status FROM memory_events WHERE id=?', (e1.id,))
        before2 = _raw(db, 'SELECT version, status FROM memory_events WHERE id=?', (e2.id,))
        create_contradiction_link(
            db, e1.id, e2.id, created_by='op', reason='r', link_confidence=3,
        )
        after1 = _raw(db, 'SELECT version, status FROM memory_events WHERE id=?', (e1.id,))
        after2 = _raw(db, 'SELECT version, status FROM memory_events WHERE id=?', (e2.id,))
        assert before1['version'] == after1['version']
        assert before1['status'] == after1['status']
        assert before2['version'] == after2['version']
        assert before2['status'] == after2['status']


# ---------------------------------------------------------------------------
# retract_contradiction_link
# ---------------------------------------------------------------------------

class TestRetractContradictionLink:
    def _make_link(self, db):
        e1 = _add(db, title='E1')
        e2 = _add(db, title='E2')
        return create_contradiction_link(
            db, e1.id, e2.id, created_by='op', reason='r', link_confidence=3,
        )

    def test_sets_status_retracted(self, db):
        link = self._make_link(db)
        retracted = retract_contradiction_link(db, link.id, retracted_by='reviewer', reason='resolved')
        assert retracted.status == 'retracted'

    def test_sets_retraction_fields(self, db):
        link = self._make_link(db)
        retracted = retract_contradiction_link(db, link.id, retracted_by='reviewer', reason='no longer valid')
        assert retracted.retracted_by == 'reviewer'
        assert retracted.retracted_reason == 'no longer valid'
        assert retracted.retracted_at is not None
        assert 'T' in retracted.retracted_at
        assert retracted.retracted_at.endswith('Z')

    def test_rejects_already_retracted(self, db):
        link = self._make_link(db)
        retract_contradiction_link(db, link.id, retracted_by='op', reason='first')
        with pytest.raises(ValidationError, match="already retracted"):
            retract_contradiction_link(db, link.id, retracted_by='op', reason='second')

    def test_rejects_non_contradiction_links(self, db):
        e1 = _add(db, title='E1')
        e2 = _add(db, title='E2')
        supports_link = link_memory_events(db, e1.id, e2.id, 'supports')
        with pytest.raises(ValidationError, match="contradicts"):
            retract_contradiction_link(db, supports_link.id, retracted_by='op', reason='r')

    def test_requires_retracted_by_non_empty(self, db):
        link = self._make_link(db)
        with pytest.raises(ValidationError, match="retracted_by"):
            retract_contradiction_link(db, link.id, retracted_by='', reason='r')

    def test_requires_reason_non_empty(self, db):
        link = self._make_link(db)
        with pytest.raises(ValidationError, match="reason"):
            retract_contradiction_link(db, link.id, retracted_by='op', reason='')

    def test_raises_if_link_not_found(self, db):
        with pytest.raises(NotFoundError):
            retract_contradiction_link(db, 9999, retracted_by='op', reason='r')

    def test_link_row_not_deleted(self, db):
        link = self._make_link(db)
        retract_contradiction_link(db, link.id, retracted_by='op', reason='r')
        row = _raw(db, 'SELECT id FROM memory_links WHERE id=?', (link.id,))
        assert row is not None

    def test_no_memory_event_mutation_on_retract(self, db):
        e1 = _add(db, title='E1')
        e2 = _add(db, title='E2')
        link = create_contradiction_link(
            db, e1.id, e2.id, created_by='op', reason='r', link_confidence=3,
        )
        before1 = _raw(db, 'SELECT version, status FROM memory_events WHERE id=?', (e1.id,))
        before2 = _raw(db, 'SELECT version, status FROM memory_events WHERE id=?', (e2.id,))
        retract_contradiction_link(db, link.id, retracted_by='op', reason='r')
        after1 = _raw(db, 'SELECT version, status FROM memory_events WHERE id=?', (e1.id,))
        after2 = _raw(db, 'SELECT version, status FROM memory_events WHERE id=?', (e2.id,))
        assert before1['version'] == after1['version']
        assert before1['status'] == after1['status']
        assert before2['version'] == after2['version']
        assert before2['status'] == after2['status']


# ---------------------------------------------------------------------------
# detect_conflicts — provenance and retraction filtering
# ---------------------------------------------------------------------------

class TestDetectConflictsV8:
    def _make_conflict(self, db, **kw):
        e1 = _add(db, title='Claim A')
        e2 = _add(db, title='Claim B')
        defaults = dict(created_by='governance-bot', reason='direct contradiction', link_confidence=4)
        defaults.update(kw)
        link = create_contradiction_link(db, e1.id, e2.id, **defaults)
        return e1, e2, link

    def test_surfaces_active_contradiction_link(self, db):
        e1, e2, _ = self._make_conflict(db)
        issues = detect_conflicts(db)
        memory_ids = [i.memory_id for i in issues]
        assert e1.id in memory_ids

    def test_excludes_retracted_contradiction_links(self, db):
        e1, e2, link = self._make_conflict(db)
        retract_contradiction_link(db, link.id, retracted_by='op', reason='resolved')
        issues = detect_conflicts(db)
        assert issues == []

    def test_returns_provenance_in_metadata(self, db):
        e1, e2, link = self._make_conflict(db)
        issues = detect_conflicts(db)
        assert len(issues) == 1
        meta = issues[0].metadata
        assert meta is not None
        assert meta['link_id'] == link.id
        assert meta['source_id'] == e1.id
        assert meta['target_id'] == e2.id
        assert meta['created_by'] == 'governance-bot'
        assert meta['reason'] == 'direct contradiction'
        assert meta['link_confidence'] == 4
        assert meta['created_at'] is not None

    def test_metadata_includes_link_metadata_json_when_present(self, db):
        e1 = _add(db, title='E1')
        e2 = _add(db, title='E2')
        meta_payload = {'method': 'semantic-scan', 'score': 0.91}
        create_contradiction_link(
            db, e1.id, e2.id,
            created_by='scanner', reason='high similarity', link_confidence=3,
            link_metadata=meta_payload,
        )
        issues = detect_conflicts(db)
        assert 'link_metadata_json' in issues[0].metadata

    def test_does_not_surface_non_active_link_statuses(self, db):
        """A retracted link must not appear in detect_conflicts output."""
        e1, e2, link = self._make_conflict(db)
        retract_contradiction_link(db, link.id, retracted_by='op', reason='r')
        issues = detect_conflicts(db)
        assert len(issues) == 0

    def test_surfaces_only_both_active_event_pairs(self, db):
        """Contradiction link between deprecated + active event must not surface."""
        e1 = _add(db, title='Deprecated claim', status='active')
        e2 = _add(db, title='Live claim')
        link = create_contradiction_link(
            db, e1.id, e2.id, created_by='op', reason='r', link_confidence=2,
        )
        # Deprecate e1 after link creation
        from memory.service import update_status
        update_status(db, e1.id, 'deprecated', reason='obsolete', created_by='op')
        issues = detect_conflicts(db)
        assert len(issues) == 0


# ---------------------------------------------------------------------------
# Generic link hardening — link_memory_events rejects contradicts
# ---------------------------------------------------------------------------

class TestLinkMemoryEventsHardening:
    def test_contradicts_raises_validation_error(self, db):
        e1 = _add(db, title='E1')
        e2 = _add(db, title='E2')
        with pytest.raises(ValidationError):
            link_memory_events(db, e1.id, e2.id, 'contradicts')

    def test_error_message_references_create_contradiction_link(self, db):
        e1 = _add(db, title='E1')
        e2 = _add(db, title='E2')
        with pytest.raises(ValidationError, match="create_contradiction_link"):
            link_memory_events(db, e1.id, e2.id, 'contradicts')

    def test_no_row_inserted_on_rejected_contradicts(self, db):
        e1 = _add(db, title='E1')
        e2 = _add(db, title='E2')
        before = _raw(db, 'SELECT COUNT(*) AS n FROM memory_links')['n']
        try:
            link_memory_events(db, e1.id, e2.id, 'contradicts')
        except ValidationError:
            pass
        after = _raw(db, 'SELECT COUNT(*) AS n FROM memory_links')['n']
        assert after == before

    def test_no_memory_event_mutation_on_rejected_contradicts(self, db):
        e1 = _add(db, title='E1')
        e2 = _add(db, title='E2')
        before1 = _raw(db, 'SELECT version, status FROM memory_events WHERE id=?', (e1.id,))
        before2 = _raw(db, 'SELECT version, status FROM memory_events WHERE id=?', (e2.id,))
        try:
            link_memory_events(db, e1.id, e2.id, 'contradicts')
        except ValidationError:
            pass
        after1 = _raw(db, 'SELECT version, status FROM memory_events WHERE id=?', (e1.id,))
        after2 = _raw(db, 'SELECT version, status FROM memory_events WHERE id=?', (e2.id,))
        assert before1['version'] == after1['version']
        assert before2['version'] == after2['version']

    def test_non_contradicts_relationships_still_work(self, db):
        e1 = _add(db, title='E1')
        e2 = _add(db, title='E2')
        for rel in ('supports', 'supersedes', 'refines', 'derived_from',
                    'related_to', 'blocks', 'depends_on'):
            e_a = _add(db, title=f'A-{rel}')
            e_b = _add(db, title=f'B-{rel}')
            lnk = link_memory_events(db, e_a.id, e_b.id, rel)
            assert lnk.relationship == rel

    def test_create_contradiction_link_still_works_after_hardening(self, db):
        e1 = _add(db, title='E1')
        e2 = _add(db, title='E2')
        link = create_contradiction_link(
            db, e1.id, e2.id, created_by='op', reason='conflict', link_confidence=3,
        )
        assert link.relationship == 'contradicts'
        assert link.status == 'active'

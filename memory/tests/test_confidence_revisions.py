"""
Tests for Phase 4B: confidence revision lineage substrate (schema v9).

Covers:
- Schema migration v8→v9
- revise_confidence() — operator and candidate
- reject_candidate_revision()
- get_effective_confidence() and get_effective_confidence_batch()
- list_confidence_revisions()
- Retrieval integration: effective_confidence in scoring and ranking
- Governance: detect_unreviewed_confidence_candidates()
"""
import json
import sqlite3
import time

import pytest

from memory import service
from memory.service import (
    NotFoundError,
    ValidationError,
    add_memory_event,
    get_effective_confidence,
    get_effective_confidence_batch,
    init_db,
    list_confidence_revisions,
    reject_candidate_revision,
    revise_confidence,
)
from memory.governance import detect_unreviewed_confidence_candidates
from memory.retrieval import RETRIEVAL_SCORING_VERSION, retrieve, RetrievalQuery


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------

def _db(tmp_path) -> str:
    path = str(tmp_path / 'cr_test.db')
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

class TestSchemaV9Migration:
    def test_fresh_db_schema_version_9(self, tmp_path):
        db = _db(tmp_path)
        conn = sqlite3.connect(db)
        version = conn.execute('SELECT version FROM memory_schema_version').fetchone()[0]
        conn.close()
        assert version == 9

    def test_confidence_revisions_table_exists(self, tmp_path):
        db = _db(tmp_path)
        conn = sqlite3.connect(db)
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        conn.close()
        assert 'confidence_revisions' in tables

    def test_confidence_revisions_columns(self, tmp_path):
        db = _db(tmp_path)
        conn = sqlite3.connect(db)
        cols = {r[1] for r in conn.execute('PRAGMA table_info(confidence_revisions)')}
        conn.close()
        required = {
            'id', 'memory_event_id', 'confidence_before', 'confidence_after',
            'revised_by', 'reason', 'revision_type', 'status',
            'contradiction_link_ids_json', 'evidence', 'provenance_json',
            'created_at', 'superseded_at', 'rejected_at', 'rejected_by', 'rejected_reason',
        }
        assert required <= cols

    def test_v9_indices_exist(self, tmp_path):
        db = _db(tmp_path)
        conn = sqlite3.connect(db)
        indices = {r[1] for r in conn.execute('PRAGMA index_list(confidence_revisions)')}
        conn.close()
        assert 'idx_conf_rev_event' in indices
        assert 'idx_conf_rev_type_status' in indices
        assert 'idx_conf_rev_created_at' in indices

    def test_v8_db_migrates_to_v9(self, tmp_path):
        """A DB at v8 should be upgraded to v9 by init_db()."""
        from memory.service import _connect
        db_path = str(tmp_path / 'v8.db')
        conn = _connect(db_path)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS memory_schema_version (version INTEGER NOT NULL);
            INSERT INTO memory_schema_version (version) VALUES (8);
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
                created_by TEXT, reason TEXT, link_confidence INTEGER,
                link_metadata_json TEXT, status TEXT NOT NULL DEFAULT 'active',
                retracted_at TEXT, retracted_reason TEXT, retracted_by TEXT,
                UNIQUE (source_id, target_id, relationship)
            );
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
        version = conn.execute('SELECT version FROM memory_schema_version').fetchone()[0]
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        conn.close()

        assert version == 9
        assert 'confidence_revisions' in tables

    def test_migration_idempotent(self, tmp_path):
        db = _db(tmp_path)
        init_db(db)
        conn = sqlite3.connect(db)
        version = conn.execute('SELECT version FROM memory_schema_version').fetchone()[0]
        conn.close()
        assert version == 9


# ---------------------------------------------------------------------------
# revise_confidence
# ---------------------------------------------------------------------------

class TestReviseConfidence:
    def test_operator_revision_stores_all_fields(self, tmp_path):
        db = _db(tmp_path)
        ev = _add(db, confidence=3)
        rev = revise_confidence(
            db, ev.id, 4, 'analyst', 'validated by backtest',
            revision_type='operator',
            evidence='backtest result 2026-05-01',
            provenance={'source': 'backtest'},
        )
        assert rev.memory_event_id == ev.id
        assert rev.confidence_before == 3
        assert rev.confidence_after == 4
        assert rev.revised_by == 'analyst'
        assert rev.reason == 'validated by backtest'
        assert rev.revision_type == 'operator'
        assert rev.status == 'active'
        assert rev.evidence == 'backtest result 2026-05-01'
        assert rev.provenance_json == json.dumps({'source': 'backtest'}, sort_keys=True)
        assert rev.superseded_at is None
        assert rev.rejected_at is None
        assert rev.rejected_by is None
        assert rev.rejected_reason is None

    def test_candidate_revision_stores_proposed_status(self, tmp_path):
        db = _db(tmp_path)
        ev = _add(db, confidence=3)
        rev = revise_confidence(
            db, ev.id, 2, 'model', 'confidence degraded',
            revision_type='candidate',
        )
        assert rev.revision_type == 'candidate'
        assert rev.status == 'proposed'
        assert rev.superseded_at is None

    def test_confidence_before_reflects_original(self, tmp_path):
        db = _db(tmp_path)
        ev = _add(db, confidence=3)
        rev = revise_confidence(db, ev.id, 5, 'analyst', 'reason')
        assert rev.confidence_before == 3

    def test_confidence_before_reflects_effective_after_prior_operator(self, tmp_path):
        db = _db(tmp_path)
        ev = _add(db, confidence=3)
        revise_confidence(db, ev.id, 4, 'analyst', 'first revision')
        rev2 = revise_confidence(db, ev.id, 5, 'analyst', 'second revision')
        assert rev2.confidence_before == 4

    def test_operator_revision_supersedes_prior_active(self, tmp_path):
        db = _db(tmp_path)
        ev = _add(db, confidence=3)
        rev1 = revise_confidence(db, ev.id, 4, 'analyst', 'first')
        assert rev1.status == 'active'

        rev2 = revise_confidence(db, ev.id, 5, 'analyst', 'second')
        assert rev2.status == 'active'

        row1 = _raw(db, 'SELECT * FROM confidence_revisions WHERE id = ?', (rev1.id,))
        assert row1['status'] == 'superseded'
        assert row1['superseded_at'] is not None

    def test_candidate_does_not_supersede_prior_operator(self, tmp_path):
        db = _db(tmp_path)
        ev = _add(db, confidence=3)
        op_rev = revise_confidence(db, ev.id, 4, 'analyst', 'first', revision_type='operator')
        revise_confidence(db, ev.id, 2, 'model', 'candidate', revision_type='candidate')

        row = _raw(db, 'SELECT * FROM confidence_revisions WHERE id = ?', (op_rev.id,))
        assert row['status'] == 'active'

    def test_contradiction_link_ids_sorted_deterministically(self, tmp_path):
        db = _db(tmp_path)
        ev = _add(db, confidence=3)
        rev = revise_confidence(
            db, ev.id, 4, 'analyst', 'reason',
            contradiction_link_ids=[5, 2, 8],
        )
        assert rev.contradiction_link_ids_json == json.dumps([2, 5, 8])

    def test_provenance_json_canonical(self, tmp_path):
        db = _db(tmp_path)
        ev = _add(db, confidence=3)
        prov = {'z_key': 'val', 'a_key': 'other'}
        rev = revise_confidence(db, ev.id, 4, 'analyst', 'reason', provenance=prov)
        assert rev.provenance_json == json.dumps(prov, sort_keys=True)

    def test_no_memory_events_mutation(self, tmp_path):
        db = _db(tmp_path)
        ev = _add(db, confidence=3)
        revise_confidence(db, ev.id, 5, 'analyst', 'reason')
        row = _raw(db, 'SELECT confidence FROM memory_events WHERE id = ?', (ev.id,))
        assert row['confidence'] == 3

    def test_raises_on_invalid_confidence(self, tmp_path):
        db = _db(tmp_path)
        ev = _add(db, confidence=3)
        with pytest.raises(ValidationError):
            revise_confidence(db, ev.id, 6, 'analyst', 'too high')
        with pytest.raises(ValidationError):
            revise_confidence(db, ev.id, 0, 'analyst', 'too low')

    def test_raises_on_empty_revised_by(self, tmp_path):
        db = _db(tmp_path)
        ev = _add(db, confidence=3)
        with pytest.raises(ValidationError):
            revise_confidence(db, ev.id, 4, '', 'reason')

    def test_raises_on_empty_reason(self, tmp_path):
        db = _db(tmp_path)
        ev = _add(db, confidence=3)
        with pytest.raises(ValidationError):
            revise_confidence(db, ev.id, 4, 'analyst', '')

    def test_raises_on_invalid_revision_type(self, tmp_path):
        db = _db(tmp_path)
        ev = _add(db, confidence=3)
        with pytest.raises(ValidationError):
            revise_confidence(db, ev.id, 4, 'analyst', 'reason', revision_type='invalid')

    def test_raises_on_unknown_event(self, tmp_path):
        db = _db(tmp_path)
        with pytest.raises(NotFoundError):
            revise_confidence(db, 9999, 4, 'analyst', 'reason')


# ---------------------------------------------------------------------------
# reject_candidate_revision
# ---------------------------------------------------------------------------

class TestRejectCandidateRevision:
    def test_rejection_sets_rejected_fields(self, tmp_path):
        db = _db(tmp_path)
        ev = _add(db, confidence=3)
        rev = revise_confidence(db, ev.id, 2, 'model', 'candidate', revision_type='candidate')
        rejected = reject_candidate_revision(db, rev.id, 'analyst', 'not validated')
        assert rejected.status == 'rejected'
        assert rejected.rejected_by == 'analyst'
        assert rejected.rejected_reason == 'not validated'
        assert rejected.rejected_at is not None

    def test_rejection_does_not_set_superseded_at(self, tmp_path):
        db = _db(tmp_path)
        ev = _add(db, confidence=3)
        rev = revise_confidence(db, ev.id, 2, 'model', 'candidate', revision_type='candidate')
        rejected = reject_candidate_revision(db, rev.id, 'analyst', 'not validated')
        assert rejected.superseded_at is None

    def test_rejection_does_not_mutate_memory_events(self, tmp_path):
        db = _db(tmp_path)
        ev = _add(db, confidence=3)
        rev = revise_confidence(db, ev.id, 2, 'model', 'candidate', revision_type='candidate')
        reject_candidate_revision(db, rev.id, 'analyst', 'reason')
        row = _raw(db, 'SELECT confidence FROM memory_events WHERE id = ?', (ev.id,))
        assert row['confidence'] == 3

    def test_raises_on_operator_revision(self, tmp_path):
        db = _db(tmp_path)
        ev = _add(db, confidence=3)
        rev = revise_confidence(db, ev.id, 4, 'analyst', 'reason', revision_type='operator')
        with pytest.raises(ValidationError):
            reject_candidate_revision(db, rev.id, 'analyst', 'wrong type')

    def test_raises_on_already_rejected(self, tmp_path):
        db = _db(tmp_path)
        ev = _add(db, confidence=3)
        rev = revise_confidence(db, ev.id, 2, 'model', 'candidate', revision_type='candidate')
        reject_candidate_revision(db, rev.id, 'analyst', 'once')
        with pytest.raises(ValidationError):
            reject_candidate_revision(db, rev.id, 'analyst', 'twice')

    def test_raises_on_empty_rejected_by(self, tmp_path):
        db = _db(tmp_path)
        ev = _add(db, confidence=3)
        rev = revise_confidence(db, ev.id, 2, 'model', 'candidate', revision_type='candidate')
        with pytest.raises(ValidationError):
            reject_candidate_revision(db, rev.id, '', 'reason')

    def test_raises_on_empty_reason(self, tmp_path):
        db = _db(tmp_path)
        ev = _add(db, confidence=3)
        rev = revise_confidence(db, ev.id, 2, 'model', 'candidate', revision_type='candidate')
        with pytest.raises(ValidationError):
            reject_candidate_revision(db, rev.id, 'analyst', '')

    def test_raises_on_unknown_revision(self, tmp_path):
        db = _db(tmp_path)
        with pytest.raises(NotFoundError):
            reject_candidate_revision(db, 9999, 'analyst', 'reason')


# ---------------------------------------------------------------------------
# get_effective_confidence
# ---------------------------------------------------------------------------

class TestGetEffectiveConfidence:
    def test_returns_original_when_no_revisions(self, tmp_path):
        db = _db(tmp_path)
        ev = _add(db, confidence=3)
        assert get_effective_confidence(db, ev.id) == 3

    def test_returns_active_operator_confidence(self, tmp_path):
        db = _db(tmp_path)
        ev = _add(db, confidence=3)
        revise_confidence(db, ev.id, 4, 'analyst', 'reason', revision_type='operator')
        assert get_effective_confidence(db, ev.id) == 4

    def test_follows_supersession_chain(self, tmp_path):
        db = _db(tmp_path)
        ev = _add(db, confidence=3)
        revise_confidence(db, ev.id, 4, 'analyst', 'first', revision_type='operator')
        revise_confidence(db, ev.id, 5, 'analyst', 'second', revision_type='operator')
        assert get_effective_confidence(db, ev.id) == 5

    def test_ignores_candidate_revisions(self, tmp_path):
        db = _db(tmp_path)
        ev = _add(db, confidence=3)
        revise_confidence(db, ev.id, 2, 'model', 'candidate', revision_type='candidate')
        assert get_effective_confidence(db, ev.id) == 3

    def test_ignores_rejected_candidates(self, tmp_path):
        db = _db(tmp_path)
        ev = _add(db, confidence=3)
        rev = revise_confidence(db, ev.id, 2, 'model', 'candidate', revision_type='candidate')
        reject_candidate_revision(db, rev.id, 'analyst', 'reason')
        assert get_effective_confidence(db, ev.id) == 3

    def test_ignores_superseded_operator(self, tmp_path):
        db = _db(tmp_path)
        ev = _add(db, confidence=3)
        revise_confidence(db, ev.id, 4, 'analyst', 'first', revision_type='operator')
        revise_confidence(db, ev.id, 5, 'analyst', 'second', revision_type='operator')
        assert get_effective_confidence(db, ev.id) == 5

    def test_raises_on_unknown_event(self, tmp_path):
        db = _db(tmp_path)
        with pytest.raises(NotFoundError):
            get_effective_confidence(db, 9999)


# ---------------------------------------------------------------------------
# get_effective_confidence_batch
# ---------------------------------------------------------------------------

class TestGetEffectiveConfidenceBatch:
    def test_returns_empty_for_empty_input(self, tmp_path):
        db = _db(tmp_path)
        assert get_effective_confidence_batch(db, []) == {}

    def test_returns_only_events_with_active_operator_revisions(self, tmp_path):
        db = _db(tmp_path)
        ev1 = _add(db, confidence=3)
        ev2 = _add(db, confidence=3)
        revise_confidence(db, ev1.id, 4, 'analyst', 'reason', revision_type='operator')
        result = get_effective_confidence_batch(db, [ev1.id, ev2.id])
        assert ev1.id in result
        assert ev2.id not in result
        assert result[ev1.id] == 4

    def test_ignores_candidates_in_batch(self, tmp_path):
        db = _db(tmp_path)
        ev = _add(db, confidence=3)
        revise_confidence(db, ev.id, 2, 'model', 'candidate', revision_type='candidate')
        result = get_effective_confidence_batch(db, [ev.id])
        assert ev.id not in result

    def test_returns_latest_operator_per_event(self, tmp_path):
        db = _db(tmp_path)
        ev = _add(db, confidence=3)
        revise_confidence(db, ev.id, 4, 'analyst', 'first', revision_type='operator')
        revise_confidence(db, ev.id, 5, 'analyst', 'second', revision_type='operator')
        result = get_effective_confidence_batch(db, [ev.id])
        assert result[ev.id] == 5


# ---------------------------------------------------------------------------
# list_confidence_revisions
# ---------------------------------------------------------------------------

class TestListConfidenceRevisions:
    def test_returns_all_revisions_ordered_by_id(self, tmp_path):
        db = _db(tmp_path)
        ev = _add(db, confidence=3)
        rev1 = revise_confidence(db, ev.id, 4, 'analyst', 'first', revision_type='operator')
        rev2 = revise_confidence(db, ev.id, 5, 'analyst', 'second', revision_type='operator')
        revs = list_confidence_revisions(db)
        ids = [r.id for r in revs]
        assert ids == sorted(ids)
        assert rev1.id in ids
        assert rev2.id in ids

    def test_filters_by_memory_event_id(self, tmp_path):
        db = _db(tmp_path)
        ev1 = _add(db, confidence=3)
        ev2 = _add(db, confidence=3)
        revise_confidence(db, ev1.id, 4, 'analyst', 'for ev1', revision_type='operator')
        revise_confidence(db, ev2.id, 5, 'analyst', 'for ev2', revision_type='operator')
        revs = list_confidence_revisions(db, memory_event_id=ev1.id)
        assert all(r.memory_event_id == ev1.id for r in revs)
        assert len(revs) == 1

    def test_filters_by_revision_type(self, tmp_path):
        db = _db(tmp_path)
        ev = _add(db, confidence=3)
        revise_confidence(db, ev.id, 4, 'analyst', 'op', revision_type='operator')
        revise_confidence(db, ev.id, 2, 'model', 'cand', revision_type='candidate')
        revs = list_confidence_revisions(db, revision_type='candidate')
        assert all(r.revision_type == 'candidate' for r in revs)
        assert len(revs) == 1

    def test_filters_by_status(self, tmp_path):
        db = _db(tmp_path)
        ev = _add(db, confidence=3)
        rev1 = revise_confidence(db, ev.id, 4, 'analyst', 'first', revision_type='operator')
        revise_confidence(db, ev.id, 5, 'analyst', 'second', revision_type='operator')
        revs = list_confidence_revisions(db, status='superseded')
        assert all(r.status == 'superseded' for r in revs)
        assert rev1.id in [r.id for r in revs]

    def test_empty_db_returns_empty(self, tmp_path):
        db = _db(tmp_path)
        assert list_confidence_revisions(db) == []


# ---------------------------------------------------------------------------
# Retrieval integration
# ---------------------------------------------------------------------------

class TestRetrievalEffectiveConfidence:
    def test_scoring_version_is_3(self, tmp_path):
        assert RETRIEVAL_SCORING_VERSION == '3.0.0'

    def test_effective_confidence_affects_ranking(self, tmp_path):
        db = _db(tmp_path)
        # ev_low: original confidence=1, no revision
        # ev_high: original confidence=1, operator revision to 5
        ev_low = _add(db, title='Low', confidence=1, event_type='hypothesis', status='active')
        ev_high = _add(db, title='High', confidence=1, event_type='hypothesis', status='active')
        revise_confidence(db, ev_high.id, 5, 'analyst', 'upgraded', revision_type='operator')

        query = RetrievalQuery(limit=10, expand_related=False)
        results = retrieve(db, query)
        ids = [s.event.id for s in results]
        # ev_high should rank ahead of ev_low due to effective confidence
        assert ids.index(ev_high.id) < ids.index(ev_low.id)

    def test_scored_event_effective_confidence_set(self, tmp_path):
        db = _db(tmp_path)
        ev = _add(db, confidence=3)
        revise_confidence(db, ev.id, 5, 'analyst', 'reason', revision_type='operator')
        query = RetrievalQuery(limit=10, expand_related=False)
        results = retrieve(db, query)
        scored = next(s for s in results if s.event.id == ev.id)
        assert scored.effective_confidence == 5

    def test_no_revision_uses_event_confidence(self, tmp_path):
        db = _db(tmp_path)
        ev = _add(db, confidence=3)
        query = RetrievalQuery(limit=10, expand_related=False)
        results = retrieve(db, query)
        scored = next(s for s in results if s.event.id == ev.id)
        assert scored.effective_confidence == 3

    def test_scoring_params_has_effective_confidence_enabled(self, tmp_path):
        import json
        from memory.retrieval import _scoring_params_json
        params = json.loads(_scoring_params_json())
        assert params.get('effective_confidence_enabled') is True


# ---------------------------------------------------------------------------
# detect_unreviewed_confidence_candidates
# ---------------------------------------------------------------------------

class TestDetectUnreviewedCandidates:
    def test_empty_when_no_candidates(self, tmp_path):
        db = _db(tmp_path)
        _add(db, confidence=3)
        issues = detect_unreviewed_confidence_candidates(db, warning_days=-1, critical_days=-1)
        assert issues == []

    def test_empty_when_no_old_enough_candidates(self, tmp_path):
        db = _db(tmp_path)
        ev = _add(db, confidence=3)
        revise_confidence(db, ev.id, 2, 'model', 'candidate', revision_type='candidate')
        # threshold far in future: all candidates are "fresh"
        issues = detect_unreviewed_confidence_candidates(db, warning_days=36500, critical_days=36500)
        assert issues == []

    def test_returns_warning_for_old_candidates(self, tmp_path):
        db = _db(tmp_path)
        ev = _add(db, confidence=3)
        revise_confidence(db, ev.id, 2, 'model', 'candidate', revision_type='candidate')
        # negative threshold: all candidates are "old"
        issues = detect_unreviewed_confidence_candidates(db, warning_days=-1, critical_days=36500)
        assert len(issues) == 1
        assert issues[0].issue_type == 'unreviewed_confidence_candidate'
        assert issues[0].severity == 'warning'
        assert issues[0].memory_id == ev.id

    def test_returns_critical_for_very_old_candidates(self, tmp_path):
        db = _db(tmp_path)
        ev = _add(db, confidence=3)
        revise_confidence(db, ev.id, 2, 'model', 'candidate', revision_type='candidate')
        issues = detect_unreviewed_confidence_candidates(db, warning_days=-1, critical_days=-1)
        assert len(issues) == 1
        assert issues[0].severity == 'critical'

    def test_metadata_contains_expected_fields(self, tmp_path):
        db = _db(tmp_path)
        ev = _add(db, confidence=3)
        rev = revise_confidence(
            db, ev.id, 2, 'model', 'auto candidate',
            revision_type='candidate',
            contradiction_link_ids=[7, 3],
        )
        issues = detect_unreviewed_confidence_candidates(db, warning_days=-1, critical_days=36500)
        assert len(issues) == 1
        meta = issues[0].metadata
        assert meta['revision_id'] == rev.id
        assert meta['memory_event_id'] == ev.id
        assert meta['confidence_before'] == 3
        assert meta['confidence_after'] == 2
        assert meta['revised_by'] == 'model'
        assert meta['reason'] == 'auto candidate'

    def test_excludes_rejected_candidates(self, tmp_path):
        db = _db(tmp_path)
        ev = _add(db, confidence=3)
        rev = revise_confidence(db, ev.id, 2, 'model', 'candidate', revision_type='candidate')
        reject_candidate_revision(db, rev.id, 'analyst', 'not valid')
        issues = detect_unreviewed_confidence_candidates(db, warning_days=-1, critical_days=36500)
        assert issues == []

    def test_excludes_operator_revisions(self, tmp_path):
        db = _db(tmp_path)
        ev = _add(db, confidence=3)
        revise_confidence(db, ev.id, 5, 'analyst', 'operator', revision_type='operator')
        issues = detect_unreviewed_confidence_candidates(db, warning_days=-1, critical_days=-1)
        assert issues == []

    def test_issue_ordered_by_id_ascending(self, tmp_path):
        db = _db(tmp_path)
        ev1 = _add(db, title='First', confidence=3)
        ev2 = _add(db, title='Second', confidence=3)
        revise_confidence(db, ev2.id, 2, 'model', 'candidate', revision_type='candidate')
        revise_confidence(db, ev1.id, 2, 'model', 'candidate', revision_type='candidate')
        issues = detect_unreviewed_confidence_candidates(db, warning_days=-1, critical_days=36500)
        ids = [i.memory_id for i in issues]
        assert ids == sorted(ids)


# ---------------------------------------------------------------------------
# Activation integration: ActivatedMemory.confidence reflects effective_confidence
# ---------------------------------------------------------------------------

class TestActivationEffectiveConfidence:
    def test_activated_memory_confidence_reflects_revision(self, tmp_path):
        from session.activation import activate_memory
        from session.models import ContextActivationPolicy

        db = _db(tmp_path)
        ev = _add(db, confidence=2, status='active')
        revise_confidence(db, ev.id, 5, 'analyst', 'validated', revision_type='operator')

        policy = ContextActivationPolicy()
        activated = activate_memory(db, policy)
        mem = next((m for m in activated if m.memory_id == ev.id), None)
        assert mem is not None
        assert mem.confidence == 5

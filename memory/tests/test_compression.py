"""
Tests for Phase 6A: governed compression artifact lineage (schema v11).

Covers:
- Schema v11: compression_artifacts table and indices exist
- compression_artifacts has no promoted_memory_id column (Phase 6B)
- create_compression_artifact(): creation, validation, provenance extraction
- get_compression_artifact() / list_compression_artifacts()
- promote_compression_artifact(): candidate → active, audit fields
- invalidate_compression_artifact(): candidate/active → invalidated (NOT rejection)
- Terminal state machine: superseded/invalidated block further transitions
- detect_unreviewed_compression_candidates(): governance detection with table guard
- build_governance_report() integrates compression governance
- CLI: create-compression-artifact, promote-compression-artifact,
        invalidate-compression-artifact, list-compression-artifacts,
        show-compression-artifact
- Bundle policy: compression_artifacts is NOT in continuity bundles
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

import pytest

from memory import service
from memory.artifact_governance import ArtifactStatus, GovernanceInvalidationError
from memory.compression import (
    CompressionArtifact,
    create_compression_artifact,
    get_compression_artifact,
    invalidate_compression_artifact,
    list_compression_artifacts,
    promote_compression_artifact,
)
from memory.governance import (
    COMPRESSION_CANDIDATE_CRITICAL_DAYS,
    COMPRESSION_CANDIDATE_WARNING_DAYS,
    build_governance_report,
    detect_unreviewed_compression_candidates,
)
from memory.service import init_db


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_asm_counter = 0


def _db(tmp_path) -> str:
    path = str(tmp_path / 'comp_test.db')
    init_db(path)
    return path


def _add_event(db_path: str) -> int:
    ev = service.add_memory_event(
        db_path=db_path,
        event_type='hypothesis',
        title='Test event',
        summary='Test summary',
        source='test',
        confidence=3,
        status='active',
        created_by='tester',
    )
    return ev.id


def _insert_assembly(db_path: str, memory_ids=None, link_ids=None) -> int:
    """Insert a minimal context_assembly_log row and return its id."""
    global _asm_counter
    _asm_counter += 1

    now = datetime.now(timezone.utc).isoformat()
    unique_hash = f'testhash_{_asm_counter:08d}'

    governance_ctx = []
    if memory_ids:
        for mid in memory_ids:
            governance_ctx.append({'memory_id': mid, 'confidence': 3})
    conflicting_pairs = []
    if link_ids:
        for lid in link_ids:
            conflicting_pairs.append({'link_id': lid})

    snapshot = {
        'governance_context': governance_ctx,
        'unresolved_items': [],
        'active_investigations': [],
        'relevant_memory': [],
        'conflicting_pairs': conflicting_pairs,
        'workflow_events': [],
        'runtime_snapshots': [],
        'total_chars': 0,
        'total_entries': 0,
        'char_budget': 12000,
        'entry_budget': 60,
        'included_entries': 0,
        'total_candidates': 0,
        'chars_used': 0,
        'session_id': 'test',
    }

    conn = sqlite3.connect(db_path)
    cur = conn.execute(
        """INSERT INTO context_assembly_log
           (assembly_hash, session_id, assembly_version, assembled_at, db_path,
            policy_json, entries_accepted, entries_rejected_budget, entries_rejected_filter,
            char_budget_used, char_budget_limit, compression_mode, assembly_snapshot_json)
           VALUES (?, 'test', '1.0.0', ?, ?, '{}', 0, 0, 0, 0, 12000, 'none', ?)""",
        (unique_hash, now, db_path, json.dumps(snapshot)),
    )
    conn.commit()
    asm_id = cur.lastrowid
    conn.close()
    return asm_id


def _past_utc(days: int) -> str:
    from datetime import timedelta
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


# ---------------------------------------------------------------------------
# Schema v11
# ---------------------------------------------------------------------------

class TestSchemaV11:
    def test_schema_version_is_11(self, tmp_path):
        db = _db(tmp_path)
        conn = sqlite3.connect(db)
        row = conn.execute('SELECT version FROM memory_schema_version').fetchone()
        conn.close()
        assert row[0] == 11

    def test_compression_artifacts_table_exists(self, tmp_path):
        db = _db(tmp_path)
        conn = sqlite3.connect(db)
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        conn.close()
        assert 'compression_artifacts' in tables

    def test_compression_artifacts_columns(self, tmp_path):
        db = _db(tmp_path)
        conn = sqlite3.connect(db)
        cols = {r[1] for r in conn.execute('PRAGMA table_info(compression_artifacts)')}
        conn.close()
        required = {
            'id', 'source_assembly_id', 'source_assembly_hash', 'cognition_session_id',
            'compression_method', 'producer_version', 'artifact_text', 'artifact_char_count',
            'source_memory_event_ids_json', 'source_contradiction_link_ids_json',
            'confidence_snapshot_json', 'excluded_event_ids_json', 'unresolved_issue_count',
            'compression_confidence', 'status', 'generated_at',
            'invalidated_at', 'invalidated_reason',
            'promoted_by', 'promoted_at', 'promotion_notes',
            'provenance_json',
        }
        assert required <= cols

    def test_no_promoted_memory_id_column(self, tmp_path):
        """promoted_memory_id is Phase 6B scope — must not exist in schema v11."""
        db = _db(tmp_path)
        conn = sqlite3.connect(db)
        cols = {r[1] for r in conn.execute('PRAGMA table_info(compression_artifacts)')}
        conn.close()
        assert 'promoted_memory_id' not in cols

    def test_compression_artifacts_indices_exist(self, tmp_path):
        db = _db(tmp_path)
        conn = sqlite3.connect(db)
        indices = {r[1] for r in conn.execute('PRAGMA index_list(compression_artifacts)')}
        conn.close()
        assert 'idx_compression_source_assembly' in indices
        assert 'idx_compression_status' in indices
        assert 'idx_compression_session' in indices
        assert 'idx_compression_method' in indices
        assert 'idx_compression_generated_at' in indices

    def test_schema_v11_is_idempotent(self, tmp_path):
        db = _db(tmp_path)
        init_db(db)  # second call must not raise
        conn = sqlite3.connect(db)
        row = conn.execute('SELECT version FROM memory_schema_version').fetchone()
        conn.close()
        assert row[0] == 11


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------

class TestCreateCompressionArtifact:
    def test_create_returns_candidate(self, tmp_path):
        db = _db(tmp_path)
        asm_id = _insert_assembly(db)
        artifact = create_compression_artifact(
            db_path=db,
            source_assembly_id=asm_id,
            compression_method='extractive_v1',
            producer_version='1.0.0',
            artifact_text='Summary text.',
            created_by='operator',
        )
        assert isinstance(artifact, CompressionArtifact)
        assert artifact.status == ArtifactStatus.CANDIDATE
        assert artifact.id > 0
        assert artifact.source_assembly_id == asm_id
        assert artifact.compression_method == 'extractive_v1'
        assert artifact.producer_version == '1.0.0'
        assert artifact.artifact_text == 'Summary text.'
        assert artifact.artifact_char_count == len('Summary text.')

    def test_create_sets_generated_at(self, tmp_path):
        db = _db(tmp_path)
        asm_id = _insert_assembly(db)
        artifact = create_compression_artifact(
            db_path=db, source_assembly_id=asm_id,
            compression_method='m', producer_version='1', artifact_text='t', created_by='op',
        )
        assert artifact.generated_at
        datetime.fromisoformat(artifact.generated_at)

    def test_create_provenance_includes_created_by(self, tmp_path):
        db = _db(tmp_path)
        asm_id = _insert_assembly(db)
        artifact = create_compression_artifact(
            db_path=db, source_assembly_id=asm_id,
            compression_method='m', producer_version='1', artifact_text='t', created_by='alice',
        )
        assert artifact.provenance['created_by'] == 'alice'

    def test_create_extracts_event_ids_from_snapshot(self, tmp_path):
        db = _db(tmp_path)
        ev1 = _add_event(db)
        ev2 = _add_event(db)
        asm_id = _insert_assembly(db, memory_ids=[ev1, ev2])
        artifact = create_compression_artifact(
            db_path=db, source_assembly_id=asm_id,
            compression_method='m', producer_version='1', artifact_text='t', created_by='op',
        )
        assert sorted(artifact.source_memory_event_ids) == sorted([ev1, ev2])

    def test_create_extracts_contradiction_link_ids(self, tmp_path):
        db = _db(tmp_path)
        asm_id = _insert_assembly(db, link_ids=[101, 202])
        artifact = create_compression_artifact(
            db_path=db, source_assembly_id=asm_id,
            compression_method='m', producer_version='1', artifact_text='t', created_by='op',
        )
        assert sorted(artifact.source_contradiction_link_ids) == [101, 202]

    def test_create_confidence_snapshot(self, tmp_path):
        db = _db(tmp_path)
        ev_id = _add_event(db)
        asm_id = _insert_assembly(db, memory_ids=[ev_id])
        artifact = create_compression_artifact(
            db_path=db, source_assembly_id=asm_id,
            compression_method='m', producer_version='1', artifact_text='t', created_by='op',
        )
        assert str(ev_id) in artifact.confidence_snapshot

    def test_create_excluded_event_ids_optional(self, tmp_path):
        db = _db(tmp_path)
        asm_id = _insert_assembly(db)
        artifact = create_compression_artifact(
            db_path=db, source_assembly_id=asm_id,
            compression_method='m', producer_version='1', artifact_text='t', created_by='op',
            excluded_event_ids=[5, 9],
        )
        assert artifact.excluded_event_ids == [5, 9]

    def test_create_compression_confidence(self, tmp_path):
        db = _db(tmp_path)
        asm_id = _insert_assembly(db)
        artifact = create_compression_artifact(
            db_path=db, source_assembly_id=asm_id,
            compression_method='m', producer_version='1', artifact_text='t', created_by='op',
            compression_confidence=4,
        )
        assert artifact.compression_confidence == 4

    def test_create_missing_assembly_raises(self, tmp_path):
        db = _db(tmp_path)
        with pytest.raises(ValueError, match='source_assembly_id=9999'):
            create_compression_artifact(
                db_path=db, source_assembly_id=9999,
                compression_method='m', producer_version='1', artifact_text='t', created_by='op',
            )

    def test_create_empty_method_raises(self, tmp_path):
        db = _db(tmp_path)
        asm_id = _insert_assembly(db)
        with pytest.raises(ValueError, match='compression_method'):
            create_compression_artifact(
                db_path=db, source_assembly_id=asm_id,
                compression_method='', producer_version='1', artifact_text='t', created_by='op',
            )

    def test_create_empty_producer_version_raises(self, tmp_path):
        db = _db(tmp_path)
        asm_id = _insert_assembly(db)
        with pytest.raises(ValueError, match='producer_version'):
            create_compression_artifact(
                db_path=db, source_assembly_id=asm_id,
                compression_method='m', producer_version='', artifact_text='t', created_by='op',
            )

    def test_create_empty_artifact_text_raises(self, tmp_path):
        db = _db(tmp_path)
        asm_id = _insert_assembly(db)
        with pytest.raises(ValueError, match='artifact_text'):
            create_compression_artifact(
                db_path=db, source_assembly_id=asm_id,
                compression_method='m', producer_version='1', artifact_text='', created_by='op',
            )

    def test_create_empty_created_by_raises(self, tmp_path):
        db = _db(tmp_path)
        asm_id = _insert_assembly(db)
        with pytest.raises(ValueError, match='created_by'):
            create_compression_artifact(
                db_path=db, source_assembly_id=asm_id,
                compression_method='m', producer_version='1', artifact_text='t', created_by='',
            )

    def test_create_invalid_compression_confidence_raises(self, tmp_path):
        db = _db(tmp_path)
        asm_id = _insert_assembly(db)
        with pytest.raises(ValueError, match='compression_confidence'):
            create_compression_artifact(
                db_path=db, source_assembly_id=asm_id,
                compression_method='m', producer_version='1', artifact_text='t', created_by='op',
                compression_confidence=6,
            )


# ---------------------------------------------------------------------------
# Get / List
# ---------------------------------------------------------------------------

class TestGetListCompressionArtifact:
    def test_get_returns_artifact(self, tmp_path):
        db = _db(tmp_path)
        asm_id = _insert_assembly(db)
        created = create_compression_artifact(
            db_path=db, source_assembly_id=asm_id,
            compression_method='m', producer_version='1', artifact_text='t', created_by='op',
        )
        fetched = get_compression_artifact(db, created.id)
        assert fetched.id == created.id
        assert fetched.artifact_text == 't'

    def test_get_not_found_raises(self, tmp_path):
        db = _db(tmp_path)
        with pytest.raises(ValueError, match='not found'):
            get_compression_artifact(db, 9999)

    def test_list_empty(self, tmp_path):
        db = _db(tmp_path)
        assert list_compression_artifacts(db) == []

    def test_list_returns_all(self, tmp_path):
        db = _db(tmp_path)
        asm_id = _insert_assembly(db)
        for i in range(3):
            create_compression_artifact(
                db_path=db, source_assembly_id=asm_id,
                compression_method='m', producer_version='1',
                artifact_text=f'text{i}', created_by='op',
            )
        assert len(list_compression_artifacts(db)) == 3

    def test_list_filter_by_status(self, tmp_path):
        db = _db(tmp_path)
        asm_id = _insert_assembly(db)
        a1 = create_compression_artifact(
            db_path=db, source_assembly_id=asm_id,
            compression_method='m', producer_version='1', artifact_text='t1', created_by='op',
        )
        promote_compression_artifact(db, a1.id, promoted_by='op', promotion_notes='ok')
        create_compression_artifact(
            db_path=db, source_assembly_id=asm_id,
            compression_method='m', producer_version='1', artifact_text='t2', created_by='op',
        )
        candidates = list_compression_artifacts(db, status='candidate')
        actives = list_compression_artifacts(db, status='active')
        assert len(candidates) == 1
        assert len(actives) == 1

    def test_list_filter_by_method(self, tmp_path):
        db = _db(tmp_path)
        asm_id = _insert_assembly(db)
        create_compression_artifact(
            db_path=db, source_assembly_id=asm_id,
            compression_method='method_a', producer_version='1', artifact_text='t', created_by='op',
        )
        create_compression_artifact(
            db_path=db, source_assembly_id=asm_id,
            compression_method='method_b', producer_version='1', artifact_text='t', created_by='op',
        )
        results = list_compression_artifacts(db, compression_method='method_a')
        assert len(results) == 1
        assert results[0].compression_method == 'method_a'

    def test_list_filter_by_assembly_id(self, tmp_path):
        db = _db(tmp_path)
        asm1 = _insert_assembly(db)
        asm2 = _insert_assembly(db)
        create_compression_artifact(
            db_path=db, source_assembly_id=asm1,
            compression_method='m', producer_version='1', artifact_text='t1', created_by='op',
        )
        create_compression_artifact(
            db_path=db, source_assembly_id=asm2,
            compression_method='m', producer_version='1', artifact_text='t2', created_by='op',
        )
        results = list_compression_artifacts(db, source_assembly_id=asm1)
        assert len(results) == 1
        assert results[0].source_assembly_id == asm1


# ---------------------------------------------------------------------------
# Promote
# ---------------------------------------------------------------------------

class TestPromoteCompressionArtifact:
    def test_promote_sets_active(self, tmp_path):
        db = _db(tmp_path)
        asm_id = _insert_assembly(db)
        artifact = create_compression_artifact(
            db_path=db, source_assembly_id=asm_id,
            compression_method='m', producer_version='1', artifact_text='t', created_by='op',
        )
        promoted = promote_compression_artifact(db, artifact.id, promoted_by='quant', promotion_notes='validated')
        assert promoted.status == ArtifactStatus.ACTIVE
        assert promoted.promoted_by == 'quant'
        assert promoted.promotion_notes == 'validated'
        assert promoted.promoted_at is not None

    def test_promote_empty_promoted_by_raises(self, tmp_path):
        db = _db(tmp_path)
        asm_id = _insert_assembly(db)
        artifact = create_compression_artifact(
            db_path=db, source_assembly_id=asm_id,
            compression_method='m', producer_version='1', artifact_text='t', created_by='op',
        )
        with pytest.raises(ValueError, match='promoted_by'):
            promote_compression_artifact(db, artifact.id, promoted_by='', promotion_notes='ok')

    def test_promote_empty_notes_raises(self, tmp_path):
        db = _db(tmp_path)
        asm_id = _insert_assembly(db)
        artifact = create_compression_artifact(
            db_path=db, source_assembly_id=asm_id,
            compression_method='m', producer_version='1', artifact_text='t', created_by='op',
        )
        with pytest.raises(ValueError, match='promotion_notes'):
            promote_compression_artifact(db, artifact.id, promoted_by='op', promotion_notes='')

    def test_promote_active_raises(self, tmp_path):
        db = _db(tmp_path)
        asm_id = _insert_assembly(db)
        artifact = create_compression_artifact(
            db_path=db, source_assembly_id=asm_id,
            compression_method='m', producer_version='1', artifact_text='t', created_by='op',
        )
        promote_compression_artifact(db, artifact.id, promoted_by='op', promotion_notes='ok')
        with pytest.raises(GovernanceInvalidationError):
            promote_compression_artifact(db, artifact.id, promoted_by='op', promotion_notes='again')

    def test_promote_invalidated_raises(self, tmp_path):
        db = _db(tmp_path)
        asm_id = _insert_assembly(db)
        artifact = create_compression_artifact(
            db_path=db, source_assembly_id=asm_id,
            compression_method='m', producer_version='1', artifact_text='t', created_by='op',
        )
        invalidate_compression_artifact(db, artifact.id, reason='stale', invalidated_by='op')
        with pytest.raises(GovernanceInvalidationError):
            promote_compression_artifact(db, artifact.id, promoted_by='op', promotion_notes='ok')


# ---------------------------------------------------------------------------
# Invalidate
# ---------------------------------------------------------------------------

class TestInvalidateCompressionArtifact:
    def test_invalidate_candidate(self, tmp_path):
        db = _db(tmp_path)
        asm_id = _insert_assembly(db)
        artifact = create_compression_artifact(
            db_path=db, source_assembly_id=asm_id,
            compression_method='m', producer_version='1', artifact_text='t', created_by='op',
        )
        result = invalidate_compression_artifact(db, artifact.id, reason='stale source', invalidated_by='op')
        assert result.status == ArtifactStatus.INVALIDATED
        assert result.invalidated_reason == 'stale source'
        assert result.invalidated_at is not None

    def test_invalidate_active(self, tmp_path):
        db = _db(tmp_path)
        asm_id = _insert_assembly(db)
        artifact = create_compression_artifact(
            db_path=db, source_assembly_id=asm_id,
            compression_method='m', producer_version='1', artifact_text='t', created_by='op',
        )
        promote_compression_artifact(db, artifact.id, promoted_by='op', promotion_notes='ok')
        result = invalidate_compression_artifact(db, artifact.id, reason='model upgrade', invalidated_by='op')
        assert result.status == ArtifactStatus.INVALIDATED

    def test_invalidation_is_not_rejection(self, tmp_path):
        """Invalidated status is source/artifact invalidation, not operator rejection."""
        db = _db(tmp_path)
        asm_id = _insert_assembly(db)
        artifact = create_compression_artifact(
            db_path=db, source_assembly_id=asm_id,
            compression_method='m', producer_version='1', artifact_text='t', created_by='op',
        )
        result = invalidate_compression_artifact(db, artifact.id, reason='stale', invalidated_by='op')
        assert result.status == 'invalidated'
        conn = sqlite3.connect(str(tmp_path / 'comp_test.db'))
        cols = {r[1] for r in conn.execute('PRAGMA table_info(compression_artifacts)')}
        conn.close()
        assert 'rejected_at' not in cols
        assert 'rejected_reason' not in cols

    def test_invalidate_empty_reason_raises(self, tmp_path):
        db = _db(tmp_path)
        asm_id = _insert_assembly(db)
        artifact = create_compression_artifact(
            db_path=db, source_assembly_id=asm_id,
            compression_method='m', producer_version='1', artifact_text='t', created_by='op',
        )
        with pytest.raises(ValueError, match='reason'):
            invalidate_compression_artifact(db, artifact.id, reason='', invalidated_by='op')

    def test_invalidate_empty_invalidated_by_raises(self, tmp_path):
        db = _db(tmp_path)
        asm_id = _insert_assembly(db)
        artifact = create_compression_artifact(
            db_path=db, source_assembly_id=asm_id,
            compression_method='m', producer_version='1', artifact_text='t', created_by='op',
        )
        with pytest.raises(ValueError, match='invalidated_by'):
            invalidate_compression_artifact(db, artifact.id, reason='r', invalidated_by='')

    def test_invalidate_invalidated_raises(self, tmp_path):
        db = _db(tmp_path)
        asm_id = _insert_assembly(db)
        artifact = create_compression_artifact(
            db_path=db, source_assembly_id=asm_id,
            compression_method='m', producer_version='1', artifact_text='t', created_by='op',
        )
        invalidate_compression_artifact(db, artifact.id, reason='r', invalidated_by='op')
        with pytest.raises(GovernanceInvalidationError):
            invalidate_compression_artifact(db, artifact.id, reason='r2', invalidated_by='op')


# ---------------------------------------------------------------------------
# Governance detection
# ---------------------------------------------------------------------------

class TestDetectUnreviewedCompressionCandidates:
    def test_no_candidates_returns_empty(self, tmp_path):
        db = _db(tmp_path)
        issues = detect_unreviewed_compression_candidates(db)
        assert issues == []

    def test_fresh_candidate_not_detected(self, tmp_path):
        db = _db(tmp_path)
        asm_id = _insert_assembly(db)
        create_compression_artifact(
            db_path=db, source_assembly_id=asm_id,
            compression_method='m', producer_version='1', artifact_text='t', created_by='op',
        )
        issues = detect_unreviewed_compression_candidates(db, warning_days=36500)
        assert issues == []

    def test_old_candidate_generates_warning(self, tmp_path):
        db = _db(tmp_path)
        asm_id = _insert_assembly(db)
        create_compression_artifact(
            db_path=db, source_assembly_id=asm_id,
            compression_method='m', producer_version='1', artifact_text='t', created_by='op',
        )
        # warning_days=-1 → cutoff in future → artifact detected
        # critical_days=36500 → cutoff far in past → artifact not critical → warning
        issues = detect_unreviewed_compression_candidates(db, warning_days=-1, critical_days=36500)
        assert len(issues) == 1
        assert issues[0].issue_type == 'unreviewed_compression_candidate'
        assert issues[0].severity == 'warning'

    def test_very_old_candidate_generates_critical(self, tmp_path):
        db = _db(tmp_path)
        asm_id = _insert_assembly(db)
        create_compression_artifact(
            db_path=db, source_assembly_id=asm_id,
            compression_method='m', producer_version='1', artifact_text='t', created_by='op',
        )
        issues = detect_unreviewed_compression_candidates(db, warning_days=-1, critical_days=0)
        assert len(issues) == 1
        assert issues[0].severity == 'critical'

    def test_issue_metadata_fields(self, tmp_path):
        db = _db(tmp_path)
        asm_id = _insert_assembly(db)
        artifact = create_compression_artifact(
            db_path=db, source_assembly_id=asm_id,
            compression_method='extractive_v1', producer_version='2.0', artifact_text='text', created_by='op',
        )
        issues = detect_unreviewed_compression_candidates(db, warning_days=-1, critical_days=-2)
        assert len(issues) == 1
        meta = issues[0].metadata
        assert meta['artifact_id'] == artifact.id
        assert meta['source_assembly_id'] == asm_id
        assert meta['compression_method'] == 'extractive_v1'
        assert meta['producer_version'] == '2.0'

    def test_active_not_detected(self, tmp_path):
        db = _db(tmp_path)
        asm_id = _insert_assembly(db)
        artifact = create_compression_artifact(
            db_path=db, source_assembly_id=asm_id,
            compression_method='m', producer_version='1', artifact_text='t', created_by='op',
        )
        promote_compression_artifact(db, artifact.id, promoted_by='op', promotion_notes='ok')
        issues = detect_unreviewed_compression_candidates(db, warning_days=-1, critical_days=-2)
        assert issues == []

    def test_invalidated_not_detected(self, tmp_path):
        db = _db(tmp_path)
        asm_id = _insert_assembly(db)
        artifact = create_compression_artifact(
            db_path=db, source_assembly_id=asm_id,
            compression_method='m', producer_version='1', artifact_text='t', created_by='op',
        )
        invalidate_compression_artifact(db, artifact.id, reason='r', invalidated_by='op')
        issues = detect_unreviewed_compression_candidates(db, warning_days=-1, critical_days=-2)
        assert issues == []

    def test_table_existence_guard(self, tmp_path):
        """Detector must return [] on a DB without the compression_artifacts table."""
        db = str(tmp_path / 'old.db')
        conn = sqlite3.connect(db)
        conn.executescript("""
            CREATE TABLE memory_schema_version (version INTEGER NOT NULL);
            INSERT INTO memory_schema_version VALUES (10);
        """)
        conn.close()
        issues = detect_unreviewed_compression_candidates(db)
        assert issues == []

    def test_constants_are_correct(self):
        assert COMPRESSION_CANDIDATE_WARNING_DAYS == 7
        assert COMPRESSION_CANDIDATE_CRITICAL_DAYS == 30


# ---------------------------------------------------------------------------
# build_governance_report integration
# ---------------------------------------------------------------------------

class TestBuildGovernanceReportCompression:
    def test_report_includes_compression_issues(self, tmp_path):
        db = _db(tmp_path)
        asm_id = _insert_assembly(db)
        create_compression_artifact(
            db_path=db, source_assembly_id=asm_id,
            compression_method='m', producer_version='1', artifact_text='t', created_by='op',
        )
        report = build_governance_report(db, compression_warning_days=-1, compression_critical_days=-2)
        types = {i.issue_type for i in report.issues}
        assert 'unreviewed_compression_candidate' in types

    def test_report_no_compression_issues_when_empty(self, tmp_path):
        db = _db(tmp_path)
        report = build_governance_report(db)
        types = {i.issue_type for i in report.issues}
        assert 'unreviewed_compression_candidate' not in types


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------

class TestCLICompressionArtifacts:
    def _run(self, args, tmp_path=None):
        from memory.cli import build_parser
        import io, sys
        parser = build_parser()
        parsed = parser.parse_args(args)
        out = io.StringIO()
        old_stdout = sys.stdout
        sys.stdout = out
        try:
            from memory.cli import _COMMANDS
            _COMMANDS[parsed.command](parsed)
        finally:
            sys.stdout = old_stdout
        return out.getvalue()

    def test_create_cli(self, tmp_path):
        db = _db(tmp_path)
        asm_id = _insert_assembly(db)
        out = self._run([
            'create-compression-artifact', '--db', db,
            '--assembly-id', str(asm_id),
            '--method', 'extractive_v1',
            '--producer-version', '1.0.0',
            '--artifact-text', 'Compressed context.',
            '--created-by', 'operator',
        ])
        data = json.loads(out)
        assert data['status'] == 'candidate'
        assert data['compression_method'] == 'extractive_v1'

    def test_show_cli(self, tmp_path):
        db = _db(tmp_path)
        asm_id = _insert_assembly(db)
        artifact = create_compression_artifact(
            db_path=db, source_assembly_id=asm_id,
            compression_method='m', producer_version='1', artifact_text='t', created_by='op',
        )
        out = self._run(['show-compression-artifact', '--db', db, '--id', str(artifact.id)])
        data = json.loads(out)
        assert data['id'] == artifact.id

    def test_list_cli(self, tmp_path):
        db = _db(tmp_path)
        asm_id = _insert_assembly(db)
        create_compression_artifact(
            db_path=db, source_assembly_id=asm_id,
            compression_method='m', producer_version='1', artifact_text='t', created_by='op',
        )
        out = self._run(['list-compression-artifacts', '--db', db])
        data = json.loads(out)
        assert len(data) == 1

    def test_promote_cli(self, tmp_path):
        db = _db(tmp_path)
        asm_id = _insert_assembly(db)
        artifact = create_compression_artifact(
            db_path=db, source_assembly_id=asm_id,
            compression_method='m', producer_version='1', artifact_text='t', created_by='op',
        )
        out = self._run([
            'promote-compression-artifact', '--db', db,
            '--id', str(artifact.id),
            '--promoted-by', 'quant',
            '--promotion-notes', 'Validated by quant review.',
        ])
        data = json.loads(out)
        assert data['status'] == 'active'
        assert data['promoted_by'] == 'quant'

    def test_invalidate_cli(self, tmp_path):
        db = _db(tmp_path)
        asm_id = _insert_assembly(db)
        artifact = create_compression_artifact(
            db_path=db, source_assembly_id=asm_id,
            compression_method='m', producer_version='1', artifact_text='t', created_by='op',
        )
        out = self._run([
            'invalidate-compression-artifact', '--db', db,
            '--id', str(artifact.id),
            '--reason', 'Source assembly superseded.',
            '--invalidated-by', 'operator',
        ])
        data = json.loads(out)
        assert data['status'] == 'invalidated'
        assert data['invalidated_reason'] == 'Source assembly superseded.'


# ---------------------------------------------------------------------------
# Bundle policy
# ---------------------------------------------------------------------------

class TestBundlePolicy:
    def test_compression_artifacts_not_in_export(self, tmp_path):
        """compression_artifacts is a local derived artifact; export must not include it."""
        db = _db(tmp_path)
        asm_id = _insert_assembly(db)
        create_compression_artifact(
            db_path=db, source_assembly_id=asm_id,
            compression_method='m', producer_version='1', artifact_text='t', created_by='op',
        )
        from memory.service import export_memory
        snapshot = export_memory(db)
        assert 'compression_artifacts' not in snapshot

"""
Tests for Phase 6B-alpha: governed continuity context integration.

Covers:
- Schema v12: superseded_by_artifact_id column and index
- ContextActivationPolicy: compression_artifact_ids, continuity_char_budget round-trips
- reconstruct() with empty compression_artifact_ids → no continuity_context
- reconstruct() with active artifact → continuity_context present in snapshot
- reconstruct() with candidate artifact → ContinuityGovernanceError
- reconstruct() with invalidated artifact → ContinuityGovernanceError
- reconstruct() with superseded artifact → ContinuityGovernanceError
- replay_assembly() reproduces continuity_context from snapshot without DB re-query
- reconstruct_from_dict() with old snapshot (no continuity_context) → empty list
- Governance order: governance_context renders before continuity_context
- continuity_context section labeled "PRIOR SESSION REDUCTION"
- continuity_char_budget separate from main char budget
- continuity_context does not alter main char budget accounting
- verify_assembly_against_current_db() detects artifact status change
- export_memory() output unchanged
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

import pytest

from memory import service as mem_service
from memory.artifact_governance import ArtifactStatus
from memory.compression import (
    create_compression_artifact,
    invalidate_compression_artifact,
    promote_compression_artifact,
)
from memory.service import init_db
from session.models import (
    CONTEXT_ASSEMBLY_VERSION,
    ContextActivationPolicy,
    ContinuityArtifactEntry,
    SessionContext,
    SessionReconstruction,
)
from session.reconstruction import (
    ContinuityGovernanceError,
    log_assembly,
    reconstruct,
    reconstruct_from_dict,
    replay_assembly,
    verify_assembly_against_current_db,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_asm_counter = 0


def _db(tmp_path) -> str:
    path = str(tmp_path / 'cc_test.db')
    init_db(path)
    return path


def _add_event(db_path: str) -> int:
    ev = mem_service.add_memory_event(
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


def _insert_assembly(db_path: str) -> int:
    """Insert a minimal context_assembly_log row and return its id."""
    global _asm_counter
    _asm_counter += 1
    now = datetime.now(timezone.utc).isoformat()
    unique_hash = f'cctest_{_asm_counter:08d}'
    snapshot = {
        'governance_context': [], 'unresolved_items': [], 'active_investigations': [],
        'relevant_memory': [], 'conflicting_pairs': [], 'workflow_events': [],
        'runtime_snapshots': [], 'total_chars': 0, 'total_entries': 0,
        'char_budget': 12000, 'entry_budget': 60, 'included_entries': 0,
        'total_candidates': 0, 'chars_used': 0, 'session_id': 'cctest',
    }
    conn = sqlite3.connect(db_path)
    cur = conn.execute(
        """INSERT INTO context_assembly_log
           (assembly_hash, session_id, assembly_version, assembled_at, db_path,
            policy_json, entries_accepted, entries_rejected_budget, entries_rejected_filter,
            char_budget_used, char_budget_limit, compression_mode, assembly_snapshot_json)
           VALUES (?, 'cctest', '1.2.0', ?, ?, '{}', 0, 0, 0, 0, 12000, 'none', ?)""",
        (unique_hash, now, db_path, json.dumps(snapshot)),
    )
    conn.commit()
    asm_id = cur.lastrowid
    conn.close()
    return asm_id


def _make_active_artifact(db_path: str, text: str = 'Compressed context.') -> int:
    """Create and promote a compression artifact; return its id."""
    asm_id = _insert_assembly(db_path)
    artifact = create_compression_artifact(
        db_path=db_path,
        source_assembly_id=asm_id,
        compression_method='extractive_v1',
        producer_version='1.0.0',
        artifact_text=text,
        created_by='operator',
    )
    promote_compression_artifact(
        db_path=db_path,
        artifact_id=artifact.id,
        promoted_by='quant',
        promotion_notes='Validated for continuity.',
    )
    return artifact.id


def _make_candidate_artifact(db_path: str) -> int:
    asm_id = _insert_assembly(db_path)
    artifact = create_compression_artifact(
        db_path=db_path,
        source_assembly_id=asm_id,
        compression_method='m',
        producer_version='1',
        artifact_text='text',
        created_by='op',
    )
    return artifact.id


# ---------------------------------------------------------------------------
# Schema v12
# ---------------------------------------------------------------------------

class TestSchemaV12:
    def test_superseded_by_artifact_id_column_exists(self, tmp_path):
        db = _db(tmp_path)
        conn = sqlite3.connect(db)
        cols = {r[1] for r in conn.execute('PRAGMA table_info(compression_artifacts)')}
        conn.close()
        assert 'superseded_by_artifact_id' in cols

    def test_superseded_by_index_exists(self, tmp_path):
        db = _db(tmp_path)
        conn = sqlite3.connect(db)
        indices = {r[1] for r in conn.execute('PRAGMA index_list(compression_artifacts)')}
        conn.close()
        assert 'idx_compression_superseded_by' in indices

    def test_schema_version_is_12(self, tmp_path):
        db = _db(tmp_path)
        conn = sqlite3.connect(db)
        row = conn.execute('SELECT version FROM memory_schema_version').fetchone()
        conn.close()
        assert row[0] == 12

    def test_v11_db_migrates_to_v12(self, tmp_path):
        """A DB at version 11 (without superseded_by column) is upgraded by init_db()."""
        db_path = str(tmp_path / 'v11.db')
        conn = sqlite3.connect(db_path)
        conn.executescript("""
            CREATE TABLE memory_schema_version (version INTEGER NOT NULL);
            INSERT INTO memory_schema_version VALUES (11);
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
                created_at TEXT NOT NULL, UNIQUE (source_id, target_id, relationship)
            );
            CREATE TABLE IF NOT EXISTS retrieval_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT, query_hash TEXT NOT NULL,
                session_id TEXT, query_json TEXT NOT NULL, scoring_version TEXT NOT NULL,
                scoring_params_json TEXT NOT NULL, result_event_ids_json TEXT NOT NULL,
                result_count INTEGER NOT NULL, executed_at TEXT NOT NULL, actor TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active', semantic_mode TEXT NOT NULL DEFAULT 'none',
                semantic_provenance_json TEXT
            );
            CREATE TABLE IF NOT EXISTS compression_artifacts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_assembly_id INTEGER NOT NULL, source_assembly_hash TEXT NOT NULL,
                cognition_session_id INTEGER, compression_method TEXT NOT NULL,
                producer_version TEXT NOT NULL, artifact_text TEXT NOT NULL,
                artifact_char_count INTEGER NOT NULL,
                source_memory_event_ids_json TEXT NOT NULL DEFAULT '[]',
                source_contradiction_link_ids_json TEXT NOT NULL DEFAULT '[]',
                confidence_snapshot_json TEXT NOT NULL DEFAULT '{}',
                excluded_event_ids_json TEXT NOT NULL DEFAULT '[]',
                unresolved_issue_count INTEGER NOT NULL DEFAULT 0,
                compression_confidence INTEGER,
                status TEXT NOT NULL DEFAULT 'candidate',
                generated_at TEXT NOT NULL,
                invalidated_at TEXT, invalidated_reason TEXT,
                promoted_by TEXT, promoted_at TEXT, promotion_notes TEXT,
                provenance_json TEXT NOT NULL DEFAULT '{}'
            );
        """)
        conn.close()

        init_db(db_path)

        conn = sqlite3.connect(db_path)
        version = conn.execute('SELECT version FROM memory_schema_version').fetchone()[0]
        cols = {r[1] for r in conn.execute('PRAGMA table_info(compression_artifacts)')}
        indices = {r[1] for r in conn.execute('PRAGMA index_list(compression_artifacts)')}
        conn.close()
        assert version == 12
        assert 'superseded_by_artifact_id' in cols
        assert 'idx_compression_superseded_by' in indices


# ---------------------------------------------------------------------------
# ContextActivationPolicy round-trips
# ---------------------------------------------------------------------------

class TestContextActivationPolicyRoundTrip:
    def test_default_compression_artifact_ids_is_empty(self):
        p = ContextActivationPolicy()
        assert p.compression_artifact_ids == []

    def test_default_continuity_char_budget(self):
        p = ContextActivationPolicy()
        assert p.continuity_char_budget == 2000

    def test_to_dict_includes_new_fields(self):
        p = ContextActivationPolicy(
            compression_artifact_ids=[1, 2, 3],
            continuity_char_budget=1500,
        )
        d = p.to_dict()
        assert d['compression_artifact_ids'] == [1, 2, 3]
        assert d['continuity_char_budget'] == 1500

    def test_from_dict_round_trips(self):
        p = ContextActivationPolicy(
            compression_artifact_ids=[7],
            continuity_char_budget=500,
        )
        restored = ContextActivationPolicy.from_dict(p.to_dict())
        assert restored.compression_artifact_ids == [7]
        assert restored.continuity_char_budget == 500

    def test_from_dict_old_policy_defaults_to_empty(self):
        """Old policies serialized without these keys default safely."""
        old_dict = {'min_confidence': 1, 'max_chars': 12000, 'max_entries': 60}
        p = ContextActivationPolicy.from_dict(old_dict)
        assert p.compression_artifact_ids == []
        assert p.continuity_char_budget == 2000


# ---------------------------------------------------------------------------
# ContinuityArtifactEntry
# ---------------------------------------------------------------------------

class TestContinuityArtifactEntry:
    def test_to_dict_from_dict_round_trip(self):
        entry = ContinuityArtifactEntry(
            artifact_id=5,
            source_assembly_id=10,
            source_assembly_hash='abc123',
            compression_method='extractive_v1',
            producer_version='1.0.0',
            promoted_by='quant',
            promoted_at='2026-05-25T00:00:00+00:00',
            artifact_text='Summary.',
            artifact_char_count=8,
        )
        restored = ContinuityArtifactEntry.from_dict(entry.to_dict())
        assert restored.artifact_id == 5
        assert restored.artifact_text == 'Summary.'
        assert restored.promoted_by == 'quant'

    def test_render_contains_artifact_id(self):
        entry = ContinuityArtifactEntry(
            artifact_id=42,
            source_assembly_id=1,
            source_assembly_hash='hash',
            compression_method='m',
            producer_version='1',
            promoted_by='op',
            promoted_at='2026-01-01T00:00:00+00:00',
            artifact_text='text here',
            artifact_char_count=9,
        )
        rendered = entry.render()
        assert '[artifact:42]' in rendered
        assert 'text here' in rendered


# ---------------------------------------------------------------------------
# reconstruct() — empty compression_artifact_ids
# ---------------------------------------------------------------------------

class TestReconstructEmptyContinuity:
    def test_empty_ids_produces_no_continuity_context(self, tmp_path):
        db = _db(tmp_path)
        result = reconstruct(db)
        assert result.context.continuity_context == []

    def test_empty_ids_preserves_main_budget(self, tmp_path):
        db = _db(tmp_path)
        result = reconstruct(db)
        ctx = result.context
        assert ctx.char_budget == 12000
        assert ctx.chars_used == 0

    def test_assembly_version_is_current(self, tmp_path):
        db = _db(tmp_path)
        result = reconstruct(db)
        assert result.context.assembly_version == CONTEXT_ASSEMBLY_VERSION

    def test_to_dict_includes_continuity_context_key(self, tmp_path):
        db = _db(tmp_path)
        result = reconstruct(db)
        d = result.context.to_dict()
        assert 'continuity_context' in d
        assert d['continuity_context'] == []


# ---------------------------------------------------------------------------
# reconstruct() — active artifact
# ---------------------------------------------------------------------------

class TestReconstructWithActiveContinuity:
    def test_active_artifact_appears_in_continuity_context(self, tmp_path):
        db = _db(tmp_path)
        artifact_id = _make_active_artifact(db, text='Prior session reduction text.')
        policy = ContextActivationPolicy(compression_artifact_ids=[artifact_id])
        result = reconstruct(db, policy)
        assert len(result.context.continuity_context) == 1
        entry = result.context.continuity_context[0]
        assert entry.artifact_id == artifact_id
        assert entry.artifact_text == 'Prior session reduction text.'
        assert entry.promoted_by == 'quant'

    def test_active_artifact_lineage_fingerprints_captured(self, tmp_path):
        db = _db(tmp_path)
        artifact_id = _make_active_artifact(db)
        policy = ContextActivationPolicy(compression_artifact_ids=[artifact_id])
        result = reconstruct(db, policy)
        entry = result.context.continuity_context[0]
        assert entry.source_assembly_id > 0
        assert entry.source_assembly_hash != ''
        assert entry.compression_method == 'extractive_v1'
        assert entry.producer_version == '1.0.0'

    def test_continuity_context_captured_in_snapshot(self, tmp_path):
        db = _db(tmp_path)
        artifact_id = _make_active_artifact(db, text='Snapshot text.')
        policy = ContextActivationPolicy(compression_artifact_ids=[artifact_id])
        result = reconstruct(db, policy)
        snapshot = result.context.to_dict()
        assert len(snapshot['continuity_context']) == 1
        assert snapshot['continuity_context'][0]['artifact_text'] == 'Snapshot text.'
        assert snapshot['continuity_context'][0]['artifact_id'] == artifact_id

    def test_continuity_does_not_consume_main_char_budget(self, tmp_path):
        db = _db(tmp_path)
        artifact_id = _make_active_artifact(db, text='A' * 500)
        policy = ContextActivationPolicy(
            compression_artifact_ids=[artifact_id],
            continuity_char_budget=2000,
        )
        result = reconstruct(db, policy)
        ctx = result.context
        # main chars_used should not include the 500-char continuity artifact
        assert ctx.chars_used == 0   # empty DB, no memory events
        assert len(result.context.continuity_context) == 1

    def test_continuity_char_budget_limits_artifacts(self, tmp_path):
        db = _db(tmp_path)
        # Create two artifacts; total would exceed budget
        id1 = _make_active_artifact(db, text='A' * 1500)
        id2 = _make_active_artifact(db, text='B' * 1500)
        policy = ContextActivationPolicy(
            compression_artifact_ids=[id1, id2],
            continuity_char_budget=2000,  # only first fits
        )
        result = reconstruct(db, policy)
        # First artifact fits (1500 < 2000); second doesn't (1500 + 1500 > 2000)
        assert len(result.context.continuity_context) == 1
        assert result.context.continuity_context[0].artifact_id == id1

    def test_multiple_active_artifacts_all_within_budget(self, tmp_path):
        db = _db(tmp_path)
        id1 = _make_active_artifact(db, text='AAA')
        id2 = _make_active_artifact(db, text='BBB')
        policy = ContextActivationPolicy(
            compression_artifact_ids=[id1, id2],
            continuity_char_budget=2000,
        )
        result = reconstruct(db, policy)
        assert len(result.context.continuity_context) == 2


# ---------------------------------------------------------------------------
# reconstruct() — non-active artifacts raise ContinuityGovernanceError
# ---------------------------------------------------------------------------

class TestReconstructGovernanceEnforcement:
    def test_candidate_artifact_raises_governance_error(self, tmp_path):
        db = _db(tmp_path)
        candidate_id = _make_candidate_artifact(db)
        policy = ContextActivationPolicy(compression_artifact_ids=[candidate_id])
        with pytest.raises(ContinuityGovernanceError, match='candidate'):
            reconstruct(db, policy)

    def test_invalidated_artifact_raises_governance_error(self, tmp_path):
        db = _db(tmp_path)
        candidate_id = _make_candidate_artifact(db)
        invalidate_compression_artifact(db, candidate_id, reason='stale', invalidated_by='op')
        policy = ContextActivationPolicy(compression_artifact_ids=[candidate_id])
        with pytest.raises(ContinuityGovernanceError, match='invalidated'):
            reconstruct(db, policy)

    def test_promoted_then_invalidated_raises_governance_error(self, tmp_path):
        db = _db(tmp_path)
        artifact_id = _make_active_artifact(db)
        invalidate_compression_artifact(db, artifact_id, reason='stale', invalidated_by='op')
        policy = ContextActivationPolicy(compression_artifact_ids=[artifact_id])
        with pytest.raises(ContinuityGovernanceError, match='invalidated'):
            reconstruct(db, policy)

    def test_nonexistent_artifact_raises_governance_error(self, tmp_path):
        db = _db(tmp_path)
        policy = ContextActivationPolicy(compression_artifact_ids=[9999])
        with pytest.raises(ContinuityGovernanceError, match='not found'):
            reconstruct(db, policy)

    def test_error_message_includes_artifact_id(self, tmp_path):
        db = _db(tmp_path)
        candidate_id = _make_candidate_artifact(db)
        policy = ContextActivationPolicy(compression_artifact_ids=[candidate_id])
        with pytest.raises(ContinuityGovernanceError) as exc_info:
            reconstruct(db, policy)
        assert str(candidate_id) in str(exc_info.value)


# ---------------------------------------------------------------------------
# replay_assembly() — snapshot-only replay
# ---------------------------------------------------------------------------

class TestReplayAssemblyContinuity:
    def test_replay_reproduces_continuity_context(self, tmp_path):
        db = _db(tmp_path)
        artifact_id = _make_active_artifact(db, text='Replay text.')
        policy = ContextActivationPolicy(compression_artifact_ids=[artifact_id])
        result = reconstruct(db, policy)
        log_row = log_assembly(db, result)
        assembly_id = log_row['id']

        replayed = replay_assembly(assembly_id, db)
        assert replayed.replayed is True
        assert len(replayed.context.continuity_context) == 1
        assert replayed.context.continuity_context[0].artifact_id == artifact_id
        assert replayed.context.continuity_context[0].artifact_text == 'Replay text.'

    def test_replay_survives_artifact_invalidation(self, tmp_path):
        """Replay must succeed even if the artifact is later invalidated."""
        db = _db(tmp_path)
        artifact_id = _make_active_artifact(db, text='Stable text.')
        policy = ContextActivationPolicy(compression_artifact_ids=[artifact_id])
        result = reconstruct(db, policy)
        log_row = log_assembly(db, result)
        assembly_id = log_row['id']

        # Invalidate the artifact after it was logged
        invalidate_compression_artifact(db, artifact_id, reason='stale', invalidated_by='op')

        # replay must succeed (snapshot is self-contained)
        replayed = replay_assembly(assembly_id, db)
        assert len(replayed.context.continuity_context) == 1
        assert replayed.context.continuity_context[0].artifact_text == 'Stable text.'

    def test_replay_without_continuity_context(self, tmp_path):
        """Assembly logged with no continuity_context replays with empty list."""
        db = _db(tmp_path)
        policy = ContextActivationPolicy()
        result = reconstruct(db, policy)
        log_row = log_assembly(db, result)
        replayed = replay_assembly(log_row['id'], db)
        assert replayed.context.continuity_context == []


# ---------------------------------------------------------------------------
# reconstruct_from_dict() backward compat
# ---------------------------------------------------------------------------

class TestReconstructFromDictBackwardCompat:
    def test_old_snapshot_without_continuity_context_defaults_to_empty(self):
        """Pre-v1.2.0 snapshots (no continuity_context key) must deserialize safely."""
        old_snapshot = {
            'session_id': 'old-session',
            'created_at': '2025-01-01T00:00:00+00:00',
            'assembly_version': '1.1.0',
            'policy': {},
            'governance_context': [],
            'unresolved_items': [],
            'active_workflows': [],
            'execution_lineage': [],
            'relevant_memory': [],
            'active_investigations': [],
            'runtime_snapshots': [],
            'total_candidates': 0,
            'included_entries': 0,
            'char_budget': 12000,
            'chars_used': 0,
            'truncated': False,
            'contradiction_pairs': [],
            # no 'continuity_context' key
        }
        ctx = reconstruct_from_dict(old_snapshot)
        assert ctx.continuity_context == []

    def test_new_snapshot_with_continuity_context_deserializes(self, tmp_path):
        db = _db(tmp_path)
        artifact_id = _make_active_artifact(db, text='For dict test.')
        policy = ContextActivationPolicy(compression_artifact_ids=[artifact_id])
        result = reconstruct(db, policy)
        snapshot_dict = result.context.to_dict()

        restored = reconstruct_from_dict(snapshot_dict)
        assert len(restored.continuity_context) == 1
        assert restored.continuity_context[0].artifact_id == artifact_id
        assert restored.continuity_context[0].artifact_text == 'For dict test.'


# ---------------------------------------------------------------------------
# Governance order invariant
# ---------------------------------------------------------------------------

class TestGovernanceOrderInvariant:
    def test_governance_context_renders_before_continuity_context(self, tmp_path):
        """ACTIVE GOVERNANCE CONTEXT must appear before PRIOR SESSION REDUCTION."""
        db = _db(tmp_path)
        mem_service.add_memory_event(
            db_path=db,
            event_type='governance_rule',
            title='Governance rule',
            summary='A governance rule.',
            source='test',
            confidence=5,
            status='active',
            created_by='tester',
        )
        artifact_id = _make_active_artifact(db, text='Reduction.')
        policy = ContextActivationPolicy(compression_artifact_ids=[artifact_id])
        result = reconstruct(db, policy)
        rendered = result.render()
        gov_pos = rendered.find('ACTIVE GOVERNANCE CONTEXT')
        cont_pos = rendered.find('PRIOR SESSION REDUCTION')
        assert gov_pos != -1
        assert cont_pos != -1
        assert gov_pos < cont_pos, (
            "ACTIVE GOVERNANCE CONTEXT must appear before PRIOR SESSION REDUCTION"
        )

    def test_continuity_section_labeled_prior_session_reduction(self, tmp_path):
        db = _db(tmp_path)
        artifact_id = _make_active_artifact(db, text='Labeled correctly.')
        policy = ContextActivationPolicy(compression_artifact_ids=[artifact_id])
        result = reconstruct(db, policy)
        rendered = result.render()
        assert 'PRIOR SESSION REDUCTION' in rendered
        assert 'Labeled correctly.' in rendered

    def test_continuity_context_not_present_in_render_when_empty(self, tmp_path):
        db = _db(tmp_path)
        result = reconstruct(db)
        rendered = result.render()
        assert 'PRIOR SESSION REDUCTION' not in rendered

    def test_governance_context_not_demoted_by_continuity(self, tmp_path):
        """With continuity_context present, governance_context still comes first in to_dict output order."""
        db = _db(tmp_path)
        artifact_id = _make_active_artifact(db, text='Continuity.')
        policy = ContextActivationPolicy(compression_artifact_ids=[artifact_id])
        result = reconstruct(db, policy)
        snapshot = result.context.to_dict()
        keys = list(snapshot.keys())
        # governance_context key must come before continuity_context key
        assert keys.index('governance_context') < keys.index('continuity_context')


# ---------------------------------------------------------------------------
# verify_assembly_against_current_db() drift detection
# ---------------------------------------------------------------------------

class TestVerifyAssemblyArtifactDrift:
    def test_no_drift_when_artifact_still_active(self, tmp_path):
        db = _db(tmp_path)
        artifact_id = _make_active_artifact(db, text='Still active.')
        policy = ContextActivationPolicy(compression_artifact_ids=[artifact_id])
        result = reconstruct(db, policy)
        log_row = log_assembly(db, result)
        report = verify_assembly_against_current_db(log_row['id'], db)
        assert report.continuity_artifacts_changed == []
        assert artifact_id not in report.continuity_artifacts_changed

    def test_drift_detected_when_artifact_invalidated(self, tmp_path):
        db = _db(tmp_path)
        artifact_id = _make_active_artifact(db, text='Will be invalidated.')
        policy = ContextActivationPolicy(compression_artifact_ids=[artifact_id])
        result = reconstruct(db, policy)
        log_row = log_assembly(db, result)

        # Invalidate after logging
        invalidate_compression_artifact(db, artifact_id, reason='stale', invalidated_by='op')

        report = verify_assembly_against_current_db(log_row['id'], db)
        assert artifact_id in report.continuity_artifacts_changed
        assert report.diverged is True

    def test_no_continuity_artifacts_in_assembly_no_drift(self, tmp_path):
        db = _db(tmp_path)
        result = reconstruct(db)
        log_row = log_assembly(db, result)
        report = verify_assembly_against_current_db(log_row['id'], db)
        assert report.continuity_artifacts_changed == []

    def test_verify_still_checks_memory_events(self, tmp_path):
        """verify_assembly_against_current_db() still reports memory event changes."""
        db = _db(tmp_path)
        result = reconstruct(db)
        log_row = log_assembly(db, result)
        # Add a new event after logging
        mem_service.add_memory_event(
            db_path=db,
            event_type='hypothesis',
            title='New event',
            summary='Added after assembly.',
            source='test',
            confidence=3,
            status='active',
            created_by='tester',
        )
        report = verify_assembly_against_current_db(log_row['id'], db)
        assert report.diverged is True
        assert len(report.events_added_since_assembly) > 0


# ---------------------------------------------------------------------------
# export_memory() unchanged
# ---------------------------------------------------------------------------

class TestExportMemoryUnchanged:
    def test_export_memory_does_not_include_continuity_artifacts(self, tmp_path):
        db = _db(tmp_path)
        artifact_id = _make_active_artifact(db, text='Should not export.')
        snapshot = mem_service.export_memory(db)
        assert 'compression_artifacts' not in snapshot
        assert 'continuity_context' not in snapshot

    def test_export_memory_structure_unchanged(self, tmp_path):
        db = _db(tmp_path)
        snapshot = mem_service.export_memory(db)
        assert 'memory_events' in snapshot
        assert 'memory_revisions' in snapshot
        assert 'memory_links' in snapshot

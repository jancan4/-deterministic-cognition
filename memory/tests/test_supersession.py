"""
Tests for Phase 6B-beta: compression artifact supersession semantics.

Covers:
- Schema v13: superseded_at, superseded_reason, superseded_by_operator columns + index
- supersede_compression_artifact(): state machine, validations, hard column invariants
- Hard invariants: superseded_at IS NULL for invalidated; invalidated_at IS NULL for superseded
- mark_superseded() and mark_invalidated() remain behaviorally distinct
- SupersessionChain dataclass
- get_supersession_chain(): traversal, broken chain, cycle, depth limit
- Replay validity: replay_assembly() survives artifact supersession
- Governance: detect_orphan_supersessions(), detect_pending_replacement_supersessions(),
              detect_supersession_cycles(), build_governance_report() integration
- CLI: supersede-compression-artifact, list-supersession-chain round-trips
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

import pytest

from memory import service
from memory.artifact_governance import (
    ArtifactStatus,
    GovernanceInvalidationError,
    mark_invalidated,
    mark_superseded,
)
from memory.compression import (
    CompressionArtifact,
    SupersessionChain,
    create_compression_artifact,
    get_compression_artifact,
    get_supersession_chain,
    invalidate_compression_artifact,
    promote_compression_artifact,
    supersede_compression_artifact,
)
from memory.governance import (
    build_governance_report,
    detect_orphan_supersessions,
    detect_pending_replacement_supersessions,
    detect_supersession_cycles,
)
from memory.service import init_db


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------

_asm_counter = 0


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / 'sup_test.db')
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


def _insert_assembly(db_path: str) -> int:
    global _asm_counter
    _asm_counter += 1
    now = datetime.now(timezone.utc).isoformat()
    unique_hash = f'testhash_{_asm_counter:08d}'
    snapshot = {
        'governance_context': [],
        'unresolved_items': [],
        'active_investigations': [],
        'relevant_memory': [],
        'conflicting_pairs': [],
    }
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    cur = conn.execute(
        """INSERT INTO context_assembly_log
           (assembly_hash, session_id, assembly_version, assembled_at, db_path,
            policy_json, entries_accepted, char_budget_used, char_budget_limit,
            assembly_snapshot_json, status)
           VALUES (?, 'sess1', '1.0.0', ?, ?, '{}', 0, 0, 8000, ?, 'active')""",
        (unique_hash, now, db_path, json.dumps(snapshot)),
    )
    asm_id = cur.lastrowid
    conn.commit()
    conn.close()
    return asm_id


def _make_active_artifact(db_path: str, method: str = 'summary') -> CompressionArtifact:
    """Create and promote a compression artifact; returns active artifact."""
    asm_id = _insert_assembly(db_path)
    artifact = create_compression_artifact(
        db_path=db_path,
        source_assembly_id=asm_id,
        compression_method=method,
        producer_version='1.0.0',
        artifact_text='Test compression text.',
        created_by='tester',
    )
    return promote_compression_artifact(
        db_path=db_path,
        artifact_id=artifact.id,
        promoted_by='operator',
        promotion_notes='Testing',
    )


def _make_candidate_artifact(db_path: str) -> CompressionArtifact:
    asm_id = _insert_assembly(db_path)
    return create_compression_artifact(
        db_path=db_path,
        source_assembly_id=asm_id,
        compression_method='summary',
        producer_version='1.0.0',
        artifact_text='Candidate artifact text.',
        created_by='tester',
    )


# ---------------------------------------------------------------------------
# Schema v13
# ---------------------------------------------------------------------------

class TestSchemaV13:
    def test_schema_version_is_16(self, db):
        conn = sqlite3.connect(db)
        row = conn.execute('SELECT version FROM memory_schema_version').fetchone()
        conn.close()
        assert row[0] == 16

    def test_v13_columns_exist(self, db):
        conn = sqlite3.connect(db)
        cols = {r[1] for r in conn.execute('PRAGMA table_info(compression_artifacts)')}
        conn.close()
        assert 'superseded_at' in cols
        assert 'superseded_reason' in cols
        assert 'superseded_by_operator' in cols

    def test_v13_index_exists(self, db):
        conn = sqlite3.connect(db)
        indices = {r[1] for r in conn.execute('PRAGMA index_list(compression_artifacts)')}
        conn.close()
        assert 'idx_compression_superseded_at' in indices

    def test_v12_db_migrates_to_v16(self, tmp_path):
        """A DB at version 12 gains v13 columns when init_db() is called."""
        from memory.service import _connect
        db_path = str(tmp_path / 'v12.db')
        conn = _connect(db_path)
        # Build a minimal v12 DB (has superseded_by_artifact_id but not v13 columns).
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS memory_schema_version (version INTEGER NOT NULL);
            INSERT INTO memory_schema_version (version) VALUES (12);
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
            CREATE TABLE IF NOT EXISTS context_assembly_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                assembly_hash TEXT NOT NULL UNIQUE, session_id TEXT NOT NULL,
                assembly_version TEXT NOT NULL, assembled_at TEXT NOT NULL,
                db_path TEXT NOT NULL, policy_json TEXT NOT NULL,
                entries_accepted INTEGER NOT NULL, char_budget_used INTEGER NOT NULL,
                char_budget_limit INTEGER NOT NULL,
                assembly_snapshot_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active'
            );
            CREATE TABLE IF NOT EXISTS compression_artifacts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_assembly_id INTEGER NOT NULL,
                source_assembly_hash TEXT NOT NULL,
                cognition_session_id INTEGER,
                compression_method TEXT NOT NULL,
                producer_version TEXT NOT NULL,
                artifact_text TEXT NOT NULL,
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
                superseded_by_artifact_id INTEGER,
                provenance_json TEXT NOT NULL DEFAULT '{}'
            );
        """)
        conn.commit()
        conn.close()

        init_db(db_path)

        conn = sqlite3.connect(db_path)
        version = conn.execute('SELECT version FROM memory_schema_version').fetchone()[0]
        cols = {r[1] for r in conn.execute('PRAGMA table_info(compression_artifacts)')}
        indices = {r[1] for r in conn.execute('PRAGMA index_list(compression_artifacts)')}
        conn.close()
        assert version == 16
        assert 'superseded_at' in cols
        assert 'superseded_reason' in cols
        assert 'superseded_by_operator' in cols
        assert 'idx_compression_superseded_at' in indices


# ---------------------------------------------------------------------------
# supersede_compression_artifact()
# ---------------------------------------------------------------------------

class TestSupersede:
    def test_supersedes_active_artifact(self, db):
        old = _make_active_artifact(db)
        new = _make_active_artifact(db)
        superseded = supersede_compression_artifact(
            db_path=db,
            artifact_id=old.id,
            superseded_by_id=new.id,
            reason='new model version',
            superseded_by_operator='operator-1',
        )
        assert superseded.status == 'superseded'
        assert superseded.superseded_at is not None
        assert superseded.superseded_reason == 'new model version'
        assert superseded.superseded_by_operator == 'operator-1'
        assert superseded.superseded_by_artifact_id == new.id

    def test_returns_updated_artifact(self, db):
        old = _make_active_artifact(db)
        new = _make_active_artifact(db)
        superseded = supersede_compression_artifact(db, old.id, new.id, 'reason', 'op')
        re_read = get_compression_artifact(db, old.id)
        assert re_read.status == 'superseded'
        assert re_read.superseded_reason == 'reason'
        assert re_read.superseded_by_artifact_id == new.id

    def test_raises_on_nonexistent_artifact(self, db):
        new = _make_active_artifact(db)
        with pytest.raises(ValueError, match="not found"):
            supersede_compression_artifact(db, 9999, new.id, 'reason', 'op')

    def test_raises_on_nonexistent_replacement(self, db):
        old = _make_active_artifact(db)
        with pytest.raises(ValueError, match="not found"):
            supersede_compression_artifact(db, old.id, 9999, 'reason', 'op')

    def test_raises_on_self_supersession(self, db):
        artifact = _make_active_artifact(db)
        with pytest.raises(ValueError, match="must differ"):
            supersede_compression_artifact(db, artifact.id, artifact.id, 'reason', 'op')

    def test_raises_on_candidate_status(self, db):
        candidate = _make_candidate_artifact(db)
        new = _make_active_artifact(db)
        with pytest.raises(ValueError, match="Only 'active'"):
            supersede_compression_artifact(db, candidate.id, new.id, 'reason', 'op')

    def test_raises_on_already_superseded(self, db):
        old = _make_active_artifact(db)
        mid = _make_active_artifact(db)
        new = _make_active_artifact(db)
        supersede_compression_artifact(db, old.id, mid.id, 'first supersession', 'op')
        with pytest.raises(ValueError, match="Only 'active'"):
            supersede_compression_artifact(db, old.id, new.id, 'second attempt', 'op')

    def test_raises_on_invalidated_artifact(self, db):
        artifact = _make_active_artifact(db)
        invalidate_compression_artifact(db, artifact.id, 'stale', 'op')
        new = _make_active_artifact(db)
        with pytest.raises(ValueError, match="Only 'active'"):
            supersede_compression_artifact(db, artifact.id, new.id, 'reason', 'op')

    def test_raises_on_empty_reason(self, db):
        old = _make_active_artifact(db)
        new = _make_active_artifact(db)
        with pytest.raises(ValueError, match="reason"):
            supersede_compression_artifact(db, old.id, new.id, '', 'op')

    def test_raises_on_empty_operator(self, db):
        old = _make_active_artifact(db)
        new = _make_active_artifact(db)
        with pytest.raises(ValueError, match="operator"):
            supersede_compression_artifact(db, old.id, new.id, 'reason', '')


# ---------------------------------------------------------------------------
# Hard column invariants
# ---------------------------------------------------------------------------

class TestHardColumnInvariants:
    def test_superseded_artifact_has_null_invalidated_at(self, db):
        """status='superseded' must never populate invalidated_at."""
        old = _make_active_artifact(db)
        new = _make_active_artifact(db)
        superseded = supersede_compression_artifact(db, old.id, new.id, 'reason', 'op')
        assert superseded.invalidated_at is None
        assert superseded.invalidated_reason is None

    def test_superseded_at_null_in_db_for_invalidated(self, db):
        """status='invalidated' must never populate superseded_at in the DB."""
        artifact = _make_active_artifact(db)
        invalidated = invalidate_compression_artifact(db, artifact.id, 'stale data', 'op')
        assert invalidated.invalidated_at is not None
        conn = sqlite3.connect(db)
        row = conn.execute(
            "SELECT superseded_at, superseded_reason, superseded_by_operator FROM compression_artifacts WHERE id=?",
            (artifact.id,),
        ).fetchone()
        conn.close()
        assert row[0] is None
        assert row[1] is None
        assert row[2] is None

    def test_invalidated_at_null_in_db_for_superseded(self, db):
        """status='superseded' must never populate invalidated_at in the DB."""
        old = _make_active_artifact(db)
        new = _make_active_artifact(db)
        supersede_compression_artifact(db, old.id, new.id, 'reason', 'op')
        conn = sqlite3.connect(db)
        row = conn.execute(
            "SELECT invalidated_at, invalidated_reason FROM compression_artifacts WHERE id=?",
            (old.id,),
        ).fetchone()
        conn.close()
        assert row[0] is None
        assert row[1] is None

    def test_mark_superseded_writes_superseded_at_not_invalidated_at(self, db):
        """Phase 6C: mark_superseded() writes superseded_at — invalidated_at remains NULL.

        This is the normalized behavior after Phase 6C. Status is authoritative;
        timestamps are informational lineage metadata only.
        """
        old = _make_active_artifact(db)
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        now = datetime.now(timezone.utc).isoformat()
        mark_superseded(conn, 'compression_artifacts', old.id, 'via shared helper', now)
        conn.commit()
        row = conn.execute(
            "SELECT status, invalidated_at, superseded_at FROM compression_artifacts WHERE id=?",
            (old.id,),
        ).fetchone()
        conn.close()
        # Normalized behavior: mark_superseded() writes superseded_at, not invalidated_at.
        assert row['status'] == 'superseded'
        assert row['superseded_at'] is not None   # Phase 6C: dedicated column written
        assert row['invalidated_at'] is None       # invalidation column stays NULL

    def test_mark_invalidated_writes_invalidated_at_not_superseded_at(self, db):
        """mark_invalidated() writes invalidated_at — superseded_at stays NULL."""
        artifact = _make_active_artifact(db)
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        now = datetime.now(timezone.utc).isoformat()
        mark_invalidated(conn, 'compression_artifacts', artifact.id, 'stale', now)
        conn.commit()
        row = conn.execute(
            "SELECT status, invalidated_at, superseded_at FROM compression_artifacts WHERE id=?",
            (artifact.id,),
        ).fetchone()
        conn.close()
        assert row['status'] == 'invalidated'
        assert row['invalidated_at'] is not None
        assert row['superseded_at'] is None

    def test_candidate_columns_all_null(self, db):
        """A fresh candidate artifact has all supersession and invalidation columns NULL."""
        artifact = _make_candidate_artifact(db)
        assert artifact.invalidated_at is None
        assert artifact.invalidated_reason is None
        assert artifact.superseded_at is None
        assert artifact.superseded_reason is None
        assert artifact.superseded_by_operator is None
        assert artifact.superseded_by_artifact_id is None


# ---------------------------------------------------------------------------
# SupersessionChain
# ---------------------------------------------------------------------------

class TestSupersessionChain:
    def test_single_artifact_chain(self, db):
        artifact = _make_active_artifact(db)
        chain = get_supersession_chain(artifact.id, db)
        assert chain.root_artifact_id == artifact.id
        assert len(chain.artifacts) == 1
        assert chain.artifacts[0].id == artifact.id
        assert not chain.chain_broken
        assert not chain.truncated
        assert not chain.cycle_detected

    def test_two_artifact_chain(self, db):
        a1 = _make_active_artifact(db)
        a2 = _make_active_artifact(db)
        supersede_compression_artifact(db, a1.id, a2.id, 'reason', 'op')
        chain = get_supersession_chain(a1.id, db)
        assert len(chain.artifacts) == 2
        assert chain.artifacts[0].id == a1.id
        assert chain.artifacts[1].id == a2.id
        assert not chain.chain_broken
        assert not chain.truncated
        assert not chain.cycle_detected

    def test_three_artifact_chain(self, db):
        a1 = _make_active_artifact(db)
        a2 = _make_active_artifact(db)
        a3 = _make_active_artifact(db)
        supersede_compression_artifact(db, a1.id, a2.id, 'r1', 'op')
        supersede_compression_artifact(db, a2.id, a3.id, 'r2', 'op')
        chain = get_supersession_chain(a1.id, db)
        assert len(chain.artifacts) == 3
        assert [a.id for a in chain.artifacts] == [a1.id, a2.id, a3.id]

    def test_broken_chain(self, db):
        """Broken chain: superseded_by_artifact_id points to non-existent artifact."""
        a1 = _make_active_artifact(db)
        # Directly insert a dangling FK to simulate a broken chain.
        conn = sqlite3.connect(db)
        conn.execute(
            "UPDATE compression_artifacts SET superseded_by_artifact_id=9999 WHERE id=?",
            (a1.id,),
        )
        conn.commit()
        conn.close()
        chain = get_supersession_chain(a1.id, db)
        assert chain.chain_broken is True
        assert not chain.cycle_detected
        assert len(chain.artifacts) == 1  # root only; broken before any successor

    def test_cycle_detected(self, db):
        """Cycle: a1 -> a2 -> a1 forms a cycle."""
        a1 = _make_active_artifact(db)
        a2 = _make_active_artifact(db)
        # Manually create the cycle without going through validation.
        conn = sqlite3.connect(db)
        conn.execute(
            "UPDATE compression_artifacts SET superseded_by_artifact_id=? WHERE id=?",
            (a2.id, a1.id),
        )
        conn.execute(
            "UPDATE compression_artifacts SET superseded_by_artifact_id=? WHERE id=?",
            (a1.id, a2.id),
        )
        conn.commit()
        conn.close()
        chain = get_supersession_chain(a1.id, db)
        assert chain.cycle_detected is True
        assert not chain.chain_broken

    def test_raises_on_nonexistent_root(self, db):
        with pytest.raises(ValueError, match="not found"):
            get_supersession_chain(9999, db)

    def test_to_dict_structure(self, db):
        a1 = _make_active_artifact(db)
        a2 = _make_active_artifact(db)
        supersede_compression_artifact(db, a1.id, a2.id, 'reason', 'op')
        chain = get_supersession_chain(a1.id, db)
        d = chain.to_dict()
        assert d['root_artifact_id'] == a1.id
        assert d['chain_length'] == 2
        assert d['chain_broken'] is False
        assert d['truncated'] is False
        assert d['cycle_detected'] is False
        assert len(d['artifacts']) == 2

    def test_depth_limit_truncation(self, db):
        """Chain of 52 artifacts: chain is truncated at 50."""
        from memory.compression import _SUPERSESSION_CHAIN_DEPTH_LIMIT
        # Build a chain of length DEPTH_LIMIT + 2.
        prev = _make_active_artifact(db)
        all_ids = [prev.id]
        for _ in range(_SUPERSESSION_CHAIN_DEPTH_LIMIT + 1):
            curr = _make_active_artifact(db)
            supersede_compression_artifact(db, prev.id, curr.id, 'chain', 'op')
            all_ids.append(curr.id)
            prev = curr
        chain = get_supersession_chain(all_ids[0], db)
        assert chain.truncated is True
        assert len(chain.artifacts) == _SUPERSESSION_CHAIN_DEPTH_LIMIT


# ---------------------------------------------------------------------------
# Replay validity after supersession
# ---------------------------------------------------------------------------

class TestReplayValidityAfterSupersession:
    def test_replay_assembly_survives_supersession(self, db):
        """replay_assembly() is replay-safe: supersession does not affect snapshot replay."""
        from session.reconstruction import reconstruct
        from session.models import ContextActivationPolicy

        asm_id = _insert_assembly(db)
        artifact = create_compression_artifact(
            db_path=db,
            source_assembly_id=asm_id,
            compression_method='summary',
            producer_version='1.0.0',
            artifact_text='Prior session reduction text.',
            created_by='tester',
        )
        promoted = promote_compression_artifact(
            db_path=db,
            artifact_id=artifact.id,
            promoted_by='operator',
            promotion_notes='For replay test',
        )
        # Supersede the artifact.
        replacement = _make_active_artifact(db)
        supersede_compression_artifact(
            db, promoted.id, replacement.id, 'upgraded model', 'op'
        )
        # Verify the superseded artifact is no longer active.
        superseded = get_compression_artifact(db, promoted.id)
        assert superseded.status == 'superseded'

        # Replay via reconstruct() without compression_artifact_ids should not raise.
        policy = ContextActivationPolicy(
            compression_artifact_ids=[],  # not loading the superseded artifact
        )
        ctx = reconstruct(memory_db_path=db, policy=policy)
        assert ctx is not None


# ---------------------------------------------------------------------------
# Governance: detect_orphan_supersessions
# ---------------------------------------------------------------------------

class TestDetectOrphanSupersessions:
    def test_empty_when_no_superseded(self, db):
        issues = detect_orphan_supersessions(db)
        assert issues == []

    def test_detects_orphan(self, db):
        artifact = _make_active_artifact(db)
        # Create orphan supersession directly (superseded but no pointer to replacement).
        conn = sqlite3.connect(db)
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """UPDATE compression_artifacts
               SET status='superseded', superseded_at=?, superseded_reason='test',
                   superseded_by_operator='op'
               WHERE id=?""",
            (now, artifact.id),
        )
        conn.commit()
        conn.close()
        issues = detect_orphan_supersessions(db)
        assert len(issues) == 1
        assert issues[0].issue_type == 'orphan_supersession'
        assert issues[0].severity == 'warning'
        assert issues[0].metadata['artifact_id'] == artifact.id

    def test_clean_supersession_not_detected(self, db):
        old = _make_active_artifact(db)
        new = _make_active_artifact(db)
        supersede_compression_artifact(db, old.id, new.id, 'reason', 'op')
        issues = detect_orphan_supersessions(db)
        assert issues == []

    def test_table_existence_guard(self, tmp_path):
        """No crash when compression_artifacts table does not exist."""
        db_path = str(tmp_path / 'bare.db')
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE memory_events (id INTEGER PRIMARY KEY)")
        conn.commit()
        conn.close()
        issues = detect_orphan_supersessions(db_path)
        assert issues == []


# ---------------------------------------------------------------------------
# Governance: detect_pending_replacement_supersessions
# ---------------------------------------------------------------------------

class TestDetectPendingReplacementSupersessions:
    def test_empty_when_no_superseded(self, db):
        issues = detect_pending_replacement_supersessions(db)
        assert issues == []

    def test_detects_candidate_replacement(self, db):
        old = _make_active_artifact(db)
        replacement = _make_candidate_artifact(db)
        # Bypass validation — directly record supersession pointing to candidate.
        conn = sqlite3.connect(db)
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """UPDATE compression_artifacts
               SET status='superseded', superseded_at=?, superseded_reason='test',
                   superseded_by_operator='op', superseded_by_artifact_id=?
               WHERE id=?""",
            (now, replacement.id, old.id),
        )
        conn.commit()
        conn.close()
        issues = detect_pending_replacement_supersessions(db)
        assert len(issues) == 1
        assert issues[0].issue_type == 'pending_replacement_supersession'
        assert issues[0].severity == 'warning'
        assert issues[0].metadata['artifact_id'] == old.id
        assert issues[0].metadata['superseded_by_artifact_id'] == replacement.id
        assert issues[0].metadata['replacement_status'] == 'candidate'

    def test_no_issue_when_replacement_active(self, db):
        old = _make_active_artifact(db)
        new = _make_active_artifact(db)
        supersede_compression_artifact(db, old.id, new.id, 'reason', 'op')
        issues = detect_pending_replacement_supersessions(db)
        assert issues == []

    def test_table_existence_guard(self, tmp_path):
        db_path = str(tmp_path / 'bare.db')
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE memory_events (id INTEGER PRIMARY KEY)")
        conn.commit()
        conn.close()
        issues = detect_pending_replacement_supersessions(db_path)
        assert issues == []


# ---------------------------------------------------------------------------
# Governance: detect_supersession_cycles
# ---------------------------------------------------------------------------

class TestDetectSupersessionCycles:
    def test_empty_when_no_cycles(self, db):
        a1 = _make_active_artifact(db)
        a2 = _make_active_artifact(db)
        supersede_compression_artifact(db, a1.id, a2.id, 'reason', 'op')
        issues = detect_supersession_cycles(db)
        assert issues == []

    def test_detects_two_artifact_cycle(self, db):
        a1 = _make_active_artifact(db)
        a2 = _make_active_artifact(db)
        conn = sqlite3.connect(db)
        conn.execute(
            "UPDATE compression_artifacts SET superseded_by_artifact_id=? WHERE id=?",
            (a2.id, a1.id),
        )
        conn.execute(
            "UPDATE compression_artifacts SET superseded_by_artifact_id=? WHERE id=?",
            (a1.id, a2.id),
        )
        conn.commit()
        conn.close()
        issues = detect_supersession_cycles(db)
        assert len(issues) == 1
        assert issues[0].issue_type == 'compression_supersession_cycle'
        assert issues[0].severity == 'critical'
        assert issues[0].memory_id == 0
        cycle_ids = issues[0].metadata['cycle_artifact_ids']
        assert sorted(cycle_ids) == sorted([a1.id, a2.id])

    def test_cycle_ids_deterministically_sorted(self, db):
        a1 = _make_active_artifact(db)
        a2 = _make_active_artifact(db)
        a3 = _make_active_artifact(db)
        conn = sqlite3.connect(db)
        conn.execute("UPDATE compression_artifacts SET superseded_by_artifact_id=? WHERE id=?", (a2.id, a1.id))
        conn.execute("UPDATE compression_artifacts SET superseded_by_artifact_id=? WHERE id=?", (a3.id, a2.id))
        conn.execute("UPDATE compression_artifacts SET superseded_by_artifact_id=? WHERE id=?", (a1.id, a3.id))
        conn.commit()
        conn.close()
        issues = detect_supersession_cycles(db)
        assert len(issues) == 1
        cycle_ids = issues[0].metadata['cycle_artifact_ids']
        assert cycle_ids == sorted(cycle_ids)

    def test_each_cycle_reported_once(self, db):
        """A cycle of 3 should produce exactly one issue, not 3."""
        a1 = _make_active_artifact(db)
        a2 = _make_active_artifact(db)
        a3 = _make_active_artifact(db)
        conn = sqlite3.connect(db)
        conn.execute("UPDATE compression_artifacts SET superseded_by_artifact_id=? WHERE id=?", (a2.id, a1.id))
        conn.execute("UPDATE compression_artifacts SET superseded_by_artifact_id=? WHERE id=?", (a3.id, a2.id))
        conn.execute("UPDATE compression_artifacts SET superseded_by_artifact_id=? WHERE id=?", (a1.id, a3.id))
        conn.commit()
        conn.close()
        issues = detect_supersession_cycles(db)
        assert len(issues) == 1

    def test_table_existence_guard(self, tmp_path):
        db_path = str(tmp_path / 'bare.db')
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE memory_events (id INTEGER PRIMARY KEY)")
        conn.commit()
        conn.close()
        issues = detect_supersession_cycles(db_path)
        assert issues == []

    def test_no_cycles_without_supersession_links(self, db):
        _make_active_artifact(db)
        _make_active_artifact(db)
        issues = detect_supersession_cycles(db)
        assert issues == []


# ---------------------------------------------------------------------------
# build_governance_report() integration
# ---------------------------------------------------------------------------

class TestGovernanceReportIntegration:
    def test_report_includes_supersession_issues(self, db):
        old = _make_active_artifact(db)
        # Create an orphan supersession.
        conn = sqlite3.connect(db)
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "UPDATE compression_artifacts SET status='superseded', superseded_at=?, "
            "superseded_reason='test', superseded_by_operator='op' WHERE id=?",
            (now, old.id),
        )
        conn.commit()
        conn.close()
        report = build_governance_report(db)
        issue_types = {i.issue_type for i in report.issues}
        assert 'orphan_supersession' in issue_types

    def test_report_includes_cycle_issues(self, db):
        a1 = _make_active_artifact(db)
        a2 = _make_active_artifact(db)
        conn = sqlite3.connect(db)
        conn.execute("UPDATE compression_artifacts SET superseded_by_artifact_id=? WHERE id=?", (a2.id, a1.id))
        conn.execute("UPDATE compression_artifacts SET superseded_by_artifact_id=? WHERE id=?", (a1.id, a2.id))
        conn.commit()
        conn.close()
        report = build_governance_report(db)
        cycle_issues = [i for i in report.issues if i.issue_type == 'compression_supersession_cycle']
        assert len(cycle_issues) == 1
        assert cycle_issues[0].severity == 'critical'

    def test_report_no_supersession_issues_when_flag_disabled(self, db):
        old = _make_active_artifact(db)
        conn = sqlite3.connect(db)
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "UPDATE compression_artifacts SET status='superseded', superseded_at=?, "
            "superseded_reason='test', superseded_by_operator='op' WHERE id=?",
            (now, old.id),
        )
        conn.commit()
        conn.close()
        report = build_governance_report(db, detect_compression_supersession_issues=False)
        issue_types = {i.issue_type for i in report.issues}
        assert 'orphan_supersession' not in issue_types

    def test_clean_db_no_supersession_issues(self, db):
        old = _make_active_artifact(db)
        new = _make_active_artifact(db)
        supersede_compression_artifact(db, old.id, new.id, 'reason', 'op')
        report = build_governance_report(db)
        supersession_issue_types = {
            'orphan_supersession',
            'pending_replacement_supersession',
            'compression_supersession_cycle',
        }
        detected = {i.issue_type for i in report.issues} & supersession_issue_types
        assert detected == set()


# ---------------------------------------------------------------------------
# CLI round-trips
# ---------------------------------------------------------------------------

class TestCLI:
    def _run(self, args: list) -> str:
        from memory.cli import main
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            main(args)
        return buf.getvalue()

    def test_supersede_compression_artifact_cli(self, db, tmp_path):
        old = _make_active_artifact(db)
        new = _make_active_artifact(db)
        out = self._run([
            'supersede-compression-artifact', '--db', db,
            '--id', str(old.id),
            '--superseded-by', str(new.id),
            '--reason', 'CLI test',
            '--operator', 'cli-op',
        ])
        data = json.loads(out)
        assert data['status'] == 'superseded'
        assert data['superseded_reason'] == 'CLI test'
        assert data['superseded_by_operator'] == 'cli-op'
        assert data['superseded_by_artifact_id'] == new.id

    def test_list_supersession_chain_cli(self, db):
        a1 = _make_active_artifact(db)
        a2 = _make_active_artifact(db)
        supersede_compression_artifact(db, a1.id, a2.id, 'chain test', 'op')
        out = self._run([
            'list-supersession-chain', '--db', db,
            '--id', str(a1.id),
        ])
        data = json.loads(out)
        assert data['root_artifact_id'] == a1.id
        assert data['chain_length'] == 2
        assert data['chain_broken'] is False
        assert data['cycle_detected'] is False

    def test_supersede_cli_error_on_invalid_artifact(self, db):
        from memory.cli import main
        new = _make_active_artifact(db)
        with pytest.raises(SystemExit):
            main([
                'supersede-compression-artifact', '--db', db,
                '--id', '9999',
                '--superseded-by', str(new.id),
                '--reason', 'fail',
                '--operator', 'op',
            ])

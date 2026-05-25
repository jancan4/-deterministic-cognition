"""
Phase 3B acceptance tests — context_assembly_log, replay, verify.

Tests map directly to the 20 acceptance criteria in the Phase 3B design document.
"""
import json
import sqlite3
from unittest.mock import patch

import pytest

from memory import service as mem_service
from session.models import (
    AssemblyDivergenceReport,
    CHAR_BUDGET_DEFAULT,
    CONTEXT_ASSEMBLY_VERSION,
    ENTRY_BUDGET_DEFAULT,
    ContextActivationPolicy,
    SessionReconstruction,
)
from session.reconstruction import (
    log_assembly,
    reconstruct,
    reconstruct_from_dict,
    replay_assembly,
    verify_assembly_against_current_db,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mem_db(tmp_path) -> str:
    path = str(tmp_path / 'mem.db')
    mem_service.init_db(path)
    return path


def _add(db, **kw):
    defaults = dict(
        event_type='hypothesis',
        title='Test',
        summary='Test summary',
        source='test',
        confidence=3,
        status='proposed',
        created_by='tester',
    )
    defaults.update(kw)
    return mem_service.add_memory_event(db, **defaults)


def _row_count(db_path: str, table: str = 'context_assembly_log') -> int:
    conn = sqlite3.connect(db_path)
    n = conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
    conn.close()
    return n


def _active_rows(db_path: str) -> list:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM context_assembly_log WHERE status='active'"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Schema v7 migration — tests 1 & 2
# ---------------------------------------------------------------------------

class TestSchemaV7Migration:
    def test_fresh_db_schema_version_7(self, tmp_path):
        db = _mem_db(tmp_path)
        conn = sqlite3.connect(db)
        version = conn.execute('SELECT version FROM memory_schema_version').fetchone()[0]
        conn.close()
        assert version == 13

    def test_fresh_db_context_assembly_log_exists(self, tmp_path):
        db = _mem_db(tmp_path)
        conn = sqlite3.connect(db)
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        conn.close()
        assert 'context_assembly_log' in tables

    def test_fresh_db_all_4_indices_exist(self, tmp_path):
        db = _mem_db(tmp_path)
        conn = sqlite3.connect(db)
        indices = {r[1] for r in conn.execute('PRAGMA index_list(context_assembly_log)')}
        conn.close()
        assert 'idx_assembly_hash' in indices
        assert 'idx_assembly_session' in indices
        assert 'idx_assembly_status' in indices
        assert 'idx_assembly_at' in indices

    def test_v6_db_migrates_to_v7(self, tmp_path):
        """A DB at version 6 should be upgraded to 7 by init_db()."""
        from memory.service import _connect
        db_path = str(tmp_path / 'v6.db')
        conn = _connect(db_path)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS memory_schema_version (version INTEGER NOT NULL);
            INSERT INTO memory_schema_version (version) VALUES (6);
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
                id INTEGER PRIMARY KEY AUTOINCREMENT, pin_scope TEXT NOT NULL DEFAULT 'global',
                adapter_name TEXT NOT NULL, adapter_version TEXT NOT NULL,
                model_name TEXT NOT NULL, model_digest TEXT, dimensions INTEGER NOT NULL,
                embedding_visible_fields_version TEXT NOT NULL DEFAULT '1',
                pin_identity TEXT NOT NULL, provider_name TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active', pinned_at TEXT NOT NULL,
                pinned_by TEXT NOT NULL, superseded_at TEXT, superseded_reason TEXT, notes TEXT
            );
        """)
        conn.commit()
        conn.close()

        mem_service.init_db(db_path)

        conn = sqlite3.connect(db_path)
        version = conn.execute('SELECT version FROM memory_schema_version').fetchone()[0]
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        conn.close()
        assert version == 13
        assert 'context_assembly_log' in tables


# ---------------------------------------------------------------------------
# Assembly identity — tests 3, 4, 5, 6
# ---------------------------------------------------------------------------

class TestAssemblyIdentity:
    def test_log_twice_same_reconstruction_idempotent(self, tmp_path):
        """Same reconstruction logged twice → second call returns existing row, table has 1 row."""
        db = _mem_db(tmp_path)
        r = reconstruct(db)
        row1 = log_assembly(db, r)
        row2 = log_assembly(db, r)
        assert row1['id'] == row2['id']
        assert row1['assembly_hash'] == row2['assembly_hash']
        assert _row_count(db) == 1

    def test_same_session_different_db_state_supersedes(self, tmp_path):
        """Same session_id with changed DB state → new active row, old row superseded."""
        db = _mem_db(tmp_path)
        r1 = reconstruct(db)
        log_assembly(db, r1)

        _add(db, event_type='hypothesis', title='NewEvent')
        r2 = reconstruct(db)
        log_assembly(db, r2)

        assert _row_count(db) == 2
        active = _active_rows(db)
        assert len(active) == 1

        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        first = conn.execute(
            "SELECT * FROM context_assembly_log WHERE id = ?", (row1_id := r1.context.session_id,)
        )
        # Verify the first row is superseded by querying directly
        rows = conn.execute("SELECT status FROM context_assembly_log ORDER BY id").fetchall()
        conn.close()
        assert rows[0]['status'] == 'superseded'
        assert rows[1]['status'] == 'active'

    def test_different_policy_tags_different_session_id(self, tmp_path):
        """Different policy tags → different session_id, no supersession between them."""
        db = _mem_db(tmp_path)
        r1 = reconstruct(db, ContextActivationPolicy(tags=['fx']))
        r2 = reconstruct(db, ContextActivationPolicy(tags=['macro']))

        log_assembly(db, r1)
        log_assembly(db, r2)

        assert r1.context.session_id != r2.context.session_id
        assert _row_count(db) == 2
        active = _active_rows(db)
        assert len(active) == 2

    def test_assembly_version_bump_changes_assembly_hash(self, tmp_path):
        """Bumping CONTEXT_ASSEMBLY_VERSION changes assembly_hash for otherwise identical inputs."""
        db = _mem_db(tmp_path)
        r = reconstruct(db)

        with patch('session.reconstruction.CONTEXT_ASSEMBLY_VERSION', '1.0.0'):
            row_v1 = log_assembly(db, r)

        # Manually clear so we can log a "different" assembly for the same reconstruction
        conn = sqlite3.connect(db)
        conn.execute('DELETE FROM context_assembly_log')
        conn.commit()
        conn.close()

        with patch('session.reconstruction.CONTEXT_ASSEMBLY_VERSION', '9.9.9'):
            row_v2 = log_assembly(db, r)

        assert row_v1['assembly_hash'] != row_v2['assembly_hash']

    def test_assembly_hash_field_present_in_log_row(self, tmp_path):
        db = _mem_db(tmp_path)
        r = reconstruct(db)
        row = log_assembly(db, r)
        assert 'assembly_hash' in row
        assert len(row['assembly_hash']) == 32

    def test_log_assembly_status_is_active(self, tmp_path):
        db = _mem_db(tmp_path)
        r = reconstruct(db)
        row = log_assembly(db, r)
        assert row['status'] == 'active'

    def test_log_assembly_entries_accepted_matches_context(self, tmp_path):
        db = _mem_db(tmp_path)
        _add(db, event_type='hypothesis', title='H1')
        r = reconstruct(db)
        row = log_assembly(db, r)
        assert row['entries_accepted'] == r.context.included_entries


# ---------------------------------------------------------------------------
# Replay — tests 7, 8, 9, 10
# ---------------------------------------------------------------------------

class TestReplayAssembly:
    def test_replay_returns_session_reconstruction_with_replayed_flag(self, tmp_path):
        db = _mem_db(tmp_path)
        r = reconstruct(db)
        row = log_assembly(db, r)
        replayed = replay_assembly(row['id'], db)
        assert isinstance(replayed, SessionReconstruction)
        assert replayed.replayed is True

    def test_replay_session_id_matches_logged_row(self, tmp_path):
        db = _mem_db(tmp_path)
        _add(db, event_type='hypothesis', title='H1')
        r = reconstruct(db)
        row = log_assembly(db, r)
        replayed = replay_assembly(row['id'], db)
        assert replayed.context.session_id == row['session_id']

    def test_replay_does_not_read_memory_events(self, tmp_path):
        """replay_assembly() must not call retrieve() or query memory_events."""
        db = _mem_db(tmp_path)
        _add(db, event_type='hypothesis', title='H1')
        r = reconstruct(db)
        row = log_assembly(db, r)

        # Patch retrieve to raise — if replay calls it, the test fails
        from memory import retrieval as _retrieval
        with patch.object(_retrieval, 'retrieve', side_effect=RuntimeError('retrieve called during replay')):
            replayed = replay_assembly(row['id'], db)
        assert replayed.replayed is True

    def test_replay_raises_on_nonexistent_id(self, tmp_path):
        db = _mem_db(tmp_path)
        with pytest.raises(ValueError, match="not found"):
            replay_assembly(9999, db)

    def test_replay_preserves_sections(self, tmp_path):
        db = _mem_db(tmp_path)
        _add(db, event_type='governance_rule', title='G1', status='active')
        _add(db, event_type='hypothesis', title='H1', status='proposed')
        r = reconstruct(db)
        row = log_assembly(db, r)
        replayed = replay_assembly(row['id'], db)
        orig_gov_ids = {m.memory_id for m in r.context.governance_context}
        replay_gov_ids = {m.memory_id for m in replayed.context.governance_context}
        assert orig_gov_ids == replay_gov_ids


# ---------------------------------------------------------------------------
# Verification — tests 11, 12, 13
# ---------------------------------------------------------------------------

class TestVerifyAssemblyAgainstCurrentDb:
    def test_verify_unchanged_db_returns_no_divergence(self, tmp_path):
        db = _mem_db(tmp_path)
        _add(db, event_type='hypothesis', title='H1')
        r = reconstruct(db)
        row = log_assembly(db, r)
        report = verify_assembly_against_current_db(row['id'], db)
        assert isinstance(report, AssemblyDivergenceReport)
        assert report.diverged is False
        assert report.events_added_since_assembly == []
        assert report.events_removed_since_assembly == []

    def test_verify_new_event_detected_as_added(self, tmp_path):
        db = _mem_db(tmp_path)
        r = reconstruct(db)
        row = log_assembly(db, r)

        _add(db, event_type='hypothesis', title='NewEvent', status='proposed')

        report = verify_assembly_against_current_db(row['id'], db)
        assert report.diverged is True
        assert len(report.events_added_since_assembly) >= 1

    def test_verify_does_not_write_to_log(self, tmp_path):
        db = _mem_db(tmp_path)
        r = reconstruct(db)
        row = log_assembly(db, r)
        count_before = _row_count(db)

        verify_assembly_against_current_db(row['id'], db)

        count_after = _row_count(db)
        assert count_after == count_before

    def test_verify_raises_on_nonexistent_id(self, tmp_path):
        db = _mem_db(tmp_path)
        with pytest.raises(ValueError, match="not found"):
            verify_assembly_against_current_db(9999, db)

    def test_verify_report_has_correct_assembly_hash(self, tmp_path):
        db = _mem_db(tmp_path)
        r = reconstruct(db)
        row = log_assembly(db, r)
        report = verify_assembly_against_current_db(row['id'], db)
        assert report.assembly_hash == row['assembly_hash']


# ---------------------------------------------------------------------------
# Governance filter closure — tests 14, 15, 16
# ---------------------------------------------------------------------------

class TestGovernanceFilterClosure:
    def test_context_activation_policy_has_no_include_governance(self):
        policy = ContextActivationPolicy()
        assert not hasattr(policy, 'include_governance')

    def test_activate_memory_always_returns_governance_events(self, tmp_path):
        db = _mem_db(tmp_path)
        _add(db, event_type='governance_rule', title='G1', status='active')
        from session.activation import activate_memory
        result = activate_memory(db, ContextActivationPolicy())
        types = [m.event_type for m in result]
        assert 'governance_rule' in types

    def test_score_and_rank_pins_governance_tier_0(self):
        from memory.retrieval import ScoredEvent
        from memory.models import MemoryEvent
        from session.activation import score_and_rank

        def _ev(id_, event_type):
            return MemoryEvent(
                id=id_, event_type=event_type, title=f'T{id_}',
                summary='s', evidence=None, source='test', confidence=3,
                status='active', tags=[], related_ids=[], created_by='t',
                created_at='2025-01-01T00:00:00Z', updated_at='2025-01-01T00:00:00Z',
                version=1,
            )

        scored = [
            ScoredEvent(event=_ev(1, 'hypothesis'), tag_overlap=0, recency_rank=0, is_expanded=False),
            ScoredEvent(event=_ev(2, 'governance_rule'), tag_overlap=0, recency_rank=1, is_expanded=False),
        ]
        result = score_and_rank(scored, pin_governance=True, pin_unresolved=False)
        assert result[0].memory_id == 2  # governance pinned to tier 0


# ---------------------------------------------------------------------------
# Budget constants — tests 17 & 18
# ---------------------------------------------------------------------------

class TestBudgetConstants:
    def test_char_budget_default_is_12000(self):
        assert CHAR_BUDGET_DEFAULT == 12000

    def test_entry_budget_default_is_60(self):
        assert ENTRY_BUDGET_DEFAULT == 60

    def test_default_policy_uses_named_constants(self):
        policy = ContextActivationPolicy()
        assert policy.max_chars == CHAR_BUDGET_DEFAULT
        assert policy.max_entries == ENTRY_BUDGET_DEFAULT

    def test_constants_importable_from_session_models(self):
        from session.models import CHAR_BUDGET_DEFAULT as CBD, ENTRY_BUDGET_DEFAULT as EBD
        assert CBD == 12000
        assert EBD == 60


# ---------------------------------------------------------------------------
# Backward compat — tests 19 & 20
# ---------------------------------------------------------------------------

class TestBackwardCompat:
    def test_reconstruct_from_dict_without_assembly_version_does_not_raise(self, tmp_path):
        db = _mem_db(tmp_path)
        r = reconstruct(db)
        d = r.context.to_dict()
        d.pop('assembly_version', None)
        restored = reconstruct_from_dict(d)
        assert restored.assembly_version == 'unknown'

    def test_compression_mode_defaults_to_none_in_policy(self):
        policy = ContextActivationPolicy()
        assert policy.compression_mode == 'none'

    def test_compression_mode_defaults_to_none_in_log_row(self, tmp_path):
        db = _mem_db(tmp_path)
        r = reconstruct(db)
        row = log_assembly(db, r)
        assert row['compression_mode'] == 'none'

    def test_from_dict_ignores_include_governance(self):
        """Old policy_json with include_governance key should deserialize without error."""
        old_policy_dict = {
            'tags': [],
            'min_confidence': 1,
            'include_unresolved': True,
            'include_governance': True,   # old field — must be silently ignored
            'include_adaptations': True,
            'expand_related': True,
            'compression_mode': 'none',
            'include_active_workflows': True,
            'workflow_db_path': None,
            'max_workflows': 10,
            'include_runtime_state': True,
            'runtime_db_path': None,
            'max_runtime_events': 5,
            'max_memory_candidates': 50,
            'max_chars': 12000,
            'max_entries': 60,
        }
        policy = ContextActivationPolicy.from_dict(old_policy_dict)
        assert policy.include_unresolved is True
        assert not hasattr(policy, 'include_governance')

    def test_context_assembly_version_is_1_1_0(self):
        assert CONTEXT_ASSEMBLY_VERSION == '1.2.0'

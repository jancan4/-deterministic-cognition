"""
Phase 5B acceptance tests — cognition session lifecycle, assembly transition log,
replay, verification, and session governance detectors.
"""
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from memory import service as mem_service
from session.models import (
    AssemblyDivergenceReport,
    AssemblyTransition,
    CognitionSession,
    ContextActivationPolicy,
    SessionTimelineDivergenceReport,
    VALID_SESSION_STATUSES,
    VALID_TRANSITION_TYPES,
)
from session.reconstruction import (
    close_cognition_session,
    get_cognition_session,
    get_session_assemblies,
    list_cognition_sessions,
    log_assembly,
    log_assembly_transition,
    open_cognition_session,
    reconstruct,
    replay_session_timeline,
    verify_session_timeline,
)
from memory.governance import (
    detect_abandoned_sessions,
    detect_duplicate_active_sessions,
    detect_stale_sessions,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mem_db(tmp_path) -> str:
    path = str(tmp_path / 'mem.db')
    mem_service.init_db(path)
    return path


def _default_policy(**kw) -> ContextActivationPolicy:
    return ContextActivationPolicy(**kw)


_asm_counter = 0


def _insert_assembly(db_path: str, session_key: str = 'default') -> int:
    """Insert a minimal context_assembly_log row and return its id."""
    global _asm_counter
    _asm_counter += 1
    now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    unique_hash = f'testhash_{_asm_counter:08d}'
    snapshot = {
        'governance_context': [],
        'unresolved_items': [],
        'active_investigations': [],
        'relevant_memory': [],
        'conflicting_pairs': [],
        'workflow_events': [],
        'runtime_snapshots': [],
        'total_chars': 0,
        'total_entries': 0,
        'char_budget': 12000,
        'entry_budget': 60,
        'included_entries': 0,
        'total_candidates': 0,
        'chars_used': 0,
        'session_id': session_key,
    }
    conn = sqlite3.connect(db_path)
    cur = conn.execute(
        """INSERT INTO context_assembly_log
           (assembly_hash, session_id, assembly_version, assembled_at, db_path,
            policy_json, entries_accepted, entries_rejected_budget, entries_rejected_filter,
            char_budget_used, char_budget_limit, compression_mode,
            assembly_snapshot_json)
           VALUES (?, ?, '1.1.0', ?, ?, '{}', 0, 0, 0, 0, 12000, 'none', ?)""",
        (unique_hash, session_key, now, db_path, json.dumps(snapshot)),
    )
    conn.commit()
    row_id = cur.lastrowid
    conn.close()
    return row_id


def _past_utc(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime('%Y-%m-%dT%H:%M:%SZ')


def _v9_db(tmp_path, name: str = 'v9.db') -> str:
    """Create a minimal DB without cognition_session table (simulates v9)."""
    db = str(tmp_path / name)
    conn = sqlite3.connect(db)
    conn.close()
    return db


# ---------------------------------------------------------------------------
# Schema v10 — tables and indices
# ---------------------------------------------------------------------------

class TestSchemaV10:
    def test_fresh_db_schema_version_12(self, tmp_path):
        db = _mem_db(tmp_path)
        conn = sqlite3.connect(db)
        version = conn.execute('SELECT version FROM memory_schema_version').fetchone()[0]
        conn.close()
        assert version == 12

    def test_cognition_session_table_exists(self, tmp_path):
        db = _mem_db(tmp_path)
        conn = sqlite3.connect(db)
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        conn.close()
        assert 'cognition_session' in tables

    def test_assembly_transition_log_table_exists(self, tmp_path):
        db = _mem_db(tmp_path)
        conn = sqlite3.connect(db)
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        conn.close()
        assert 'assembly_transition_log' in tables

    def test_cognition_session_indices_exist(self, tmp_path):
        db = _mem_db(tmp_path)
        conn = sqlite3.connect(db)
        all_idx = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        )}
        conn.close()
        assert 'idx_cog_session_key' in all_idx
        assert 'idx_cog_session_status' in all_idx
        assert 'idx_cog_session_started_at' in all_idx
        assert 'idx_atl_session' in all_idx
        assert 'idx_atl_to_assembly' in all_idx
        assert 'idx_atl_type' in all_idx


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

class TestConstants:
    def test_valid_transition_types(self):
        assert VALID_TRANSITION_TYPES == frozenset({
            'session_start', 'memory_drift', 'confidence_revision',
            'contradiction_change', 'operator_rebuild', 'policy_update', 'session_close',
        })

    def test_valid_session_statuses(self):
        assert VALID_SESSION_STATUSES == frozenset({'active', 'closed', 'abandoned'})


# ---------------------------------------------------------------------------
# Dataclass roundtrips
# ---------------------------------------------------------------------------

class TestCognitionSessionDataclass:
    def test_to_dict_from_dict_roundtrip(self):
        sess = CognitionSession(
            id=1, session_key='abc', status='active',
            started_at='2026-01-01T00:00:00Z', closed_at=None, closed_reason=None,
            initial_assembly_id=None, latest_assembly_id=None, assembly_count=0,
            db_path='/tmp/x.db', policy_fingerprint_json='{}', metadata_json=None,
        )
        rebuilt = CognitionSession.from_dict(sess.to_dict())
        assert rebuilt == sess

    def test_to_dict_has_all_fields(self):
        sess = CognitionSession(
            id=2, session_key='key', status='closed',
            started_at='2026-01-01T00:00:00Z', closed_at='2026-01-02T00:00:00Z',
            closed_reason='done', initial_assembly_id=1, latest_assembly_id=3,
            assembly_count=3, db_path='/x.db', policy_fingerprint_json='{"a":1}',
            metadata_json='{"meta": true}',
        )
        d = sess.to_dict()
        assert d['status'] == 'closed'
        assert d['closed_reason'] == 'done'
        assert d['assembly_count'] == 3
        assert d['initial_assembly_id'] == 1

    def test_from_row_via_open_and_get(self, tmp_path):
        db = _mem_db(tmp_path)
        sess = open_cognition_session(db, _default_policy(), 'tester')
        fetched = get_cognition_session(db, sess.id)
        assert fetched.id == sess.id
        assert fetched.status == 'active'
        assert fetched.assembly_count == 0


class TestAssemblyTransitionDataclass:
    def test_to_dict_from_dict_roundtrip(self):
        t = AssemblyTransition(
            id=1, cognition_session_id=1, sequence_index=0,
            from_assembly_id=None, to_assembly_id=5,
            transition_type='session_start', transition_reason='init',
            triggered_by='tester', transitioned_at='2026-01-01T00:00:00Z',
            triggering_retrieval_ids_json=None,
            triggering_confidence_revision_ids_json=None,
            triggering_contradiction_link_ids_json=None,
            provenance_json=None,
        )
        rebuilt = AssemblyTransition.from_dict(t.to_dict())
        assert rebuilt == t

    def test_to_dict_has_all_fields(self):
        t = AssemblyTransition(
            id=2, cognition_session_id=1, sequence_index=1,
            from_assembly_id=5, to_assembly_id=6,
            transition_type='memory_drift', transition_reason='new events',
            triggered_by='system', transitioned_at='2026-01-02T00:00:00Z',
            triggering_retrieval_ids_json='[1,2]',
            triggering_confidence_revision_ids_json=None,
            triggering_contradiction_link_ids_json='[7]',
            provenance_json='{"k":"v"}',
        )
        d = t.to_dict()
        assert d['sequence_index'] == 1
        assert d['transition_type'] == 'memory_drift'
        assert d['triggering_retrieval_ids_json'] == '[1,2]'


class TestSessionTimelineDivergenceReportDataclass:
    def test_to_dict_empty(self):
        report = SessionTimelineDivergenceReport(
            cognition_session_id=1, diverged=False, assembly_reports=[],
        )
        d = report.to_dict()
        assert d['cognition_session_id'] == 1
        assert d['diverged'] is False
        assert d['assembly_reports'] == []

    def test_to_dict_with_reports(self):
        r = AssemblyDivergenceReport(
            assembly_id=1, assembly_hash='h', diverged=False,
            events_added_since_assembly=[], events_removed_since_assembly=[],
            events_rescored_since_assembly=[],
            contradictions_added_since_assembly=[],
            contradictions_retracted_since_assembly=[],
        )
        report = SessionTimelineDivergenceReport(
            cognition_session_id=2, diverged=False, assembly_reports=[r],
        )
        d = report.to_dict()
        assert len(d['assembly_reports']) == 1
        assert d['assembly_reports'][0]['assembly_id'] == 1


# ---------------------------------------------------------------------------
# open_cognition_session
# ---------------------------------------------------------------------------

class TestOpenCognitionSession:
    def test_creates_active_session(self, tmp_path):
        db = _mem_db(tmp_path)
        sess = open_cognition_session(db, _default_policy(), 'tester')
        assert sess.status == 'active'
        assert sess.assembly_count == 0
        assert sess.initial_assembly_id is None
        assert sess.latest_assembly_id is None

    def test_session_key_matches_policy_fingerprint(self, tmp_path):
        import hashlib
        db = _mem_db(tmp_path)
        policy = _default_policy(tags=['a', 'b'], min_confidence=2)
        sess = open_cognition_session(db, policy, 'tester')
        expected = hashlib.sha256(
            f"{db}|{sorted(policy.tags)}|{policy.min_confidence}".encode()
        ).hexdigest()[:32]
        assert sess.session_key == expected

    def test_raises_on_empty_triggered_by(self, tmp_path):
        db = _mem_db(tmp_path)
        with pytest.raises(ValueError, match='triggered_by'):
            open_cognition_session(db, _default_policy(), '')

    def test_multiple_opens_same_policy_allowed(self, tmp_path):
        db = _mem_db(tmp_path)
        policy = _default_policy()
        s1 = open_cognition_session(db, policy, 'tester')
        s2 = open_cognition_session(db, policy, 'tester')
        assert s1.id != s2.id
        assert s1.session_key == s2.session_key
        assert s2.status == 'active'

    def test_metadata_stored(self, tmp_path):
        db = _mem_db(tmp_path)
        sess = open_cognition_session(db, _default_policy(), 'tester', metadata={'k': 'v'})
        fetched = get_cognition_session(db, sess.id)
        assert fetched.metadata_json is not None
        assert json.loads(fetched.metadata_json) == {'k': 'v'}


# ---------------------------------------------------------------------------
# log_assembly_transition
# ---------------------------------------------------------------------------

class TestLogAssemblyTransition:
    def test_appends_first_transition(self, tmp_path):
        db = _mem_db(tmp_path)
        sess = open_cognition_session(db, _default_policy(), 'tester')
        asm_id = _insert_assembly(db, sess.session_key)
        t = log_assembly_transition(db, sess.id, asm_id, 'session_start', 'tester', 'init')
        assert t.sequence_index == 0
        assert t.from_assembly_id is None
        assert t.to_assembly_id == asm_id

    def test_first_transition_updates_session_pointers(self, tmp_path):
        db = _mem_db(tmp_path)
        sess = open_cognition_session(db, _default_policy(), 'tester')
        asm_id = _insert_assembly(db, sess.session_key)
        log_assembly_transition(db, sess.id, asm_id, 'session_start', 'tester', 'init')
        fetched = get_cognition_session(db, sess.id)
        assert fetched.assembly_count == 1
        assert fetched.initial_assembly_id == asm_id
        assert fetched.latest_assembly_id == asm_id

    def test_second_transition_increments_sequence_and_infers_from(self, tmp_path):
        db = _mem_db(tmp_path)
        sess = open_cognition_session(db, _default_policy(), 'tester')
        asm1 = _insert_assembly(db, 'k1')
        asm2 = _insert_assembly(db, 'k2')
        log_assembly_transition(db, sess.id, asm1, 'session_start', 'tester', 'init')
        t2 = log_assembly_transition(db, sess.id, asm2, 'memory_drift', 'tester', 'drift')
        assert t2.sequence_index == 1
        assert t2.from_assembly_id == asm1

    def test_initial_assembly_id_not_overwritten(self, tmp_path):
        db = _mem_db(tmp_path)
        sess = open_cognition_session(db, _default_policy(), 'tester')
        asm1 = _insert_assembly(db, 'k1')
        asm2 = _insert_assembly(db, 'k2')
        log_assembly_transition(db, sess.id, asm1, 'session_start', 'tester', 'init')
        log_assembly_transition(db, sess.id, asm2, 'memory_drift', 'tester', 'drift')
        fetched = get_cognition_session(db, sess.id)
        assert fetched.initial_assembly_id == asm1
        assert fetched.latest_assembly_id == asm2

    def test_raises_on_invalid_session(self, tmp_path):
        db = _mem_db(tmp_path)
        asm_id = _insert_assembly(db, 'key')
        with pytest.raises(ValueError, match='not found'):
            log_assembly_transition(db, 9999, asm_id, 'session_start', 'tester', 'r')

    def test_raises_on_closed_session(self, tmp_path):
        db = _mem_db(tmp_path)
        sess = open_cognition_session(db, _default_policy(), 'tester')
        close_cognition_session(db, sess.id, 'done', 'tester')
        asm_id = _insert_assembly(db, 'key')
        with pytest.raises(ValueError, match='closed'):
            log_assembly_transition(db, sess.id, asm_id, 'session_start', 'tester', 'r')

    def test_raises_on_missing_assembly(self, tmp_path):
        db = _mem_db(tmp_path)
        sess = open_cognition_session(db, _default_policy(), 'tester')
        with pytest.raises(ValueError, match='Assembly'):
            log_assembly_transition(db, sess.id, 9999, 'session_start', 'tester', 'r')

    def test_raises_on_invalid_transition_type(self, tmp_path):
        db = _mem_db(tmp_path)
        sess = open_cognition_session(db, _default_policy(), 'tester')
        asm_id = _insert_assembly(db, 'key')
        with pytest.raises(ValueError, match='Invalid transition_type'):
            log_assembly_transition(db, sess.id, asm_id, 'bad_type', 'tester', 'r')

    def test_raises_on_empty_triggered_by(self, tmp_path):
        db = _mem_db(tmp_path)
        sess = open_cognition_session(db, _default_policy(), 'tester')
        asm_id = _insert_assembly(db, 'key')
        with pytest.raises(ValueError, match='triggered_by'):
            log_assembly_transition(db, sess.id, asm_id, 'session_start', '', 'r')

    def test_empty_provenance_ids_stored_as_null(self, tmp_path):
        db = _mem_db(tmp_path)
        sess = open_cognition_session(db, _default_policy(), 'tester')
        asm_id = _insert_assembly(db, 'key')
        t = log_assembly_transition(
            db, sess.id, asm_id, 'session_start', 'tester', 'init',
            triggering_retrieval_ids=[],
            triggering_confidence_revision_ids=[],
        )
        assert t.triggering_retrieval_ids_json is None
        assert t.triggering_confidence_revision_ids_json is None


# ---------------------------------------------------------------------------
# close_cognition_session
# ---------------------------------------------------------------------------

class TestCloseCognitionSession:
    def test_closes_active_session(self, tmp_path):
        db = _mem_db(tmp_path)
        sess = open_cognition_session(db, _default_policy(), 'tester')
        closed = close_cognition_session(db, sess.id, 'done', 'tester')
        assert closed.status == 'closed'
        assert closed.closed_at is not None
        assert closed.closed_reason == 'done'

    def test_raises_on_not_found(self, tmp_path):
        db = _mem_db(tmp_path)
        with pytest.raises(ValueError, match='not found'):
            close_cognition_session(db, 9999, 'done', 'tester')

    def test_raises_on_already_closed(self, tmp_path):
        db = _mem_db(tmp_path)
        sess = open_cognition_session(db, _default_policy(), 'tester')
        close_cognition_session(db, sess.id, 'first', 'tester')
        with pytest.raises(ValueError, match='closed'):
            close_cognition_session(db, sess.id, 'second', 'tester')

    def test_raises_on_empty_reason(self, tmp_path):
        db = _mem_db(tmp_path)
        sess = open_cognition_session(db, _default_policy(), 'tester')
        with pytest.raises(ValueError, match='reason'):
            close_cognition_session(db, sess.id, '', 'tester')


# ---------------------------------------------------------------------------
# list_cognition_sessions / get_cognition_session
# ---------------------------------------------------------------------------

class TestListAndGetSessions:
    def test_list_returns_all_sessions(self, tmp_path):
        db = _mem_db(tmp_path)
        open_cognition_session(db, _default_policy(), 'tester')
        open_cognition_session(db, _default_policy(tags=['x']), 'tester')
        sessions = list_cognition_sessions(db)
        assert len(sessions) == 2

    def test_list_filter_by_active_status(self, tmp_path):
        db = _mem_db(tmp_path)
        s1 = open_cognition_session(db, _default_policy(), 'tester')
        open_cognition_session(db, _default_policy(tags=['x']), 'tester')
        close_cognition_session(db, s1.id, 'done', 'tester')
        active = list_cognition_sessions(db, status='active')
        assert len(active) == 1
        assert active[0].status == 'active'

    def test_list_filter_by_closed_status(self, tmp_path):
        db = _mem_db(tmp_path)
        s1 = open_cognition_session(db, _default_policy(), 'tester')
        open_cognition_session(db, _default_policy(tags=['x']), 'tester')
        close_cognition_session(db, s1.id, 'done', 'tester')
        closed = list_cognition_sessions(db, status='closed')
        assert len(closed) == 1
        assert closed[0].status == 'closed'

    def test_get_session_raises_on_not_found(self, tmp_path):
        db = _mem_db(tmp_path)
        with pytest.raises(ValueError, match='not found'):
            get_cognition_session(db, 9999)


# ---------------------------------------------------------------------------
# replay_session_timeline
# ---------------------------------------------------------------------------

class TestReplaySessionTimeline:
    def test_empty_for_session_with_no_transitions(self, tmp_path):
        db = _mem_db(tmp_path)
        sess = open_cognition_session(db, _default_policy(), 'tester')
        result = replay_session_timeline(sess.id, db)
        assert result == []

    def test_returns_one_reconstruction_per_transition(self, tmp_path):
        db = _mem_db(tmp_path)
        policy = _default_policy()
        sess = open_cognition_session(db, policy, 'tester')
        recon = reconstruct(db, policy)
        asm_row = log_assembly(db, recon)
        log_assembly_transition(db, sess.id, asm_row['id'], 'session_start', 'tester', 'init')
        result = replay_session_timeline(sess.id, db)
        assert len(result) == 1
        assert result[0].replayed is True

    def test_does_not_call_reconstruct(self, tmp_path):
        db = _mem_db(tmp_path)
        policy = _default_policy()
        sess = open_cognition_session(db, policy, 'tester')
        recon = reconstruct(db, policy)
        asm_row = log_assembly(db, recon)
        log_assembly_transition(db, sess.id, asm_row['id'], 'session_start', 'tester', 'init')
        with patch('session.reconstruction.reconstruct') as mock_recon:
            replay_session_timeline(sess.id, db)
            mock_recon.assert_not_called()

    def test_raises_on_not_found(self, tmp_path):
        db = _mem_db(tmp_path)
        with pytest.raises(ValueError, match='not found'):
            replay_session_timeline(9999, db)


# ---------------------------------------------------------------------------
# get_session_assemblies
# ---------------------------------------------------------------------------

class TestGetSessionAssemblies:
    def test_empty_for_session_with_no_transitions(self, tmp_path):
        db = _mem_db(tmp_path)
        sess = open_cognition_session(db, _default_policy(), 'tester')
        assert get_session_assemblies(sess.id, db) == []

    def test_returns_enriched_row_with_assembly_fields(self, tmp_path):
        db = _mem_db(tmp_path)
        policy = _default_policy()
        sess = open_cognition_session(db, policy, 'tester')
        recon = reconstruct(db, policy)
        asm_row = log_assembly(db, recon)
        log_assembly_transition(db, sess.id, asm_row['id'], 'session_start', 'tester', 'init')
        result = get_session_assemblies(sess.id, db)
        assert len(result) == 1
        assert 'assembly_hash' in result[0]
        assert result[0]['sequence_index'] == 0

    def test_raises_on_not_found(self, tmp_path):
        db = _mem_db(tmp_path)
        with pytest.raises(ValueError, match='not found'):
            get_session_assemblies(9999, db)


# ---------------------------------------------------------------------------
# verify_session_timeline
# ---------------------------------------------------------------------------

class TestVerifySessionTimeline:
    def test_empty_session_not_diverged(self, tmp_path):
        db = _mem_db(tmp_path)
        sess = open_cognition_session(db, _default_policy(), 'tester')
        report = verify_session_timeline(sess.id, db)
        assert report.diverged is False
        assert report.assembly_reports == []

    def test_fresh_assembly_not_diverged(self, tmp_path):
        db = _mem_db(tmp_path)
        policy = _default_policy()
        sess = open_cognition_session(db, policy, 'tester')
        recon = reconstruct(db, policy)
        asm_row = log_assembly(db, recon)
        log_assembly_transition(db, sess.id, asm_row['id'], 'session_start', 'tester', 'init')
        report = verify_session_timeline(sess.id, db)
        assert report.diverged is False
        assert len(report.assembly_reports) == 1

    def test_raises_on_not_found(self, tmp_path):
        db = _mem_db(tmp_path)
        with pytest.raises(ValueError, match='not found'):
            verify_session_timeline(9999, db)


# ---------------------------------------------------------------------------
# detect_stale_sessions
# ---------------------------------------------------------------------------

class TestDetectStaleSessions:
    def test_returns_empty_on_db_without_table(self, tmp_path):
        db = _v9_db(tmp_path)
        assert detect_stale_sessions(db) == []

    def test_no_issues_for_fresh_session(self, tmp_path):
        db = _mem_db(tmp_path)
        open_cognition_session(db, _default_policy(), 'tester')
        assert detect_stale_sessions(db, warning_days=30) == []

    def test_detects_stale_active_session(self, tmp_path):
        db = _mem_db(tmp_path)
        sess = open_cognition_session(db, _default_policy(), 'tester')
        conn = sqlite3.connect(db)
        conn.execute(
            "UPDATE cognition_session SET started_at = ? WHERE id = ?",
            (_past_utc(40), sess.id),
        )
        conn.commit()
        conn.close()
        issues = detect_stale_sessions(db, warning_days=30, critical_days=90)
        assert len(issues) == 1
        assert issues[0].issue_type == 'stale_cognition_session'
        assert issues[0].severity == 'warning'
        assert issues[0].metadata['session_id'] == sess.id

    def test_critical_severity_past_critical_days(self, tmp_path):
        db = _mem_db(tmp_path)
        sess = open_cognition_session(db, _default_policy(), 'tester')
        conn = sqlite3.connect(db)
        conn.execute(
            "UPDATE cognition_session SET started_at = ? WHERE id = ?",
            (_past_utc(100), sess.id),
        )
        conn.commit()
        conn.close()
        issues = detect_stale_sessions(db, warning_days=30, critical_days=90)
        assert len(issues) == 1
        assert issues[0].severity == 'critical'

    def test_closed_session_not_detected(self, tmp_path):
        db = _mem_db(tmp_path)
        sess = open_cognition_session(db, _default_policy(), 'tester')
        close_cognition_session(db, sess.id, 'done', 'tester')
        conn = sqlite3.connect(db)
        conn.execute(
            "UPDATE cognition_session SET started_at = ? WHERE id = ?",
            (_past_utc(40), sess.id),
        )
        conn.commit()
        conn.close()
        assert detect_stale_sessions(db, warning_days=30) == []


# ---------------------------------------------------------------------------
# detect_abandoned_sessions
# ---------------------------------------------------------------------------

class TestDetectAbandonedSessions:
    def test_returns_empty_on_db_without_table(self, tmp_path):
        db = _v9_db(tmp_path)
        assert detect_abandoned_sessions(db) == []

    def test_no_issues_for_recent_session(self, tmp_path):
        db = _mem_db(tmp_path)
        open_cognition_session(db, _default_policy(), 'tester')
        assert detect_abandoned_sessions(db, threshold_days=7) == []

    def test_detects_session_with_stale_started_at(self, tmp_path):
        db = _mem_db(tmp_path)
        sess = open_cognition_session(db, _default_policy(), 'tester')
        conn = sqlite3.connect(db)
        conn.execute(
            "UPDATE cognition_session SET started_at = ? WHERE id = ?",
            (_past_utc(10), sess.id),
        )
        conn.commit()
        conn.close()
        issues = detect_abandoned_sessions(db, threshold_days=7)
        assert len(issues) == 1
        assert issues[0].issue_type == 'abandoned_cognition_session'
        assert issues[0].metadata['session_id'] == sess.id

    def test_closed_session_not_detected(self, tmp_path):
        db = _mem_db(tmp_path)
        sess = open_cognition_session(db, _default_policy(), 'tester')
        close_cognition_session(db, sess.id, 'done', 'tester')
        conn = sqlite3.connect(db)
        conn.execute(
            "UPDATE cognition_session SET started_at = ? WHERE id = ?",
            (_past_utc(10), sess.id),
        )
        conn.commit()
        conn.close()
        assert detect_abandoned_sessions(db, threshold_days=7) == []

    def test_session_with_recent_transition_not_detected(self, tmp_path):
        db = _mem_db(tmp_path)
        policy = _default_policy()
        sess = open_cognition_session(db, policy, 'tester')
        # Backdate started_at but not the transition
        conn = sqlite3.connect(db)
        conn.execute(
            "UPDATE cognition_session SET started_at = ? WHERE id = ?",
            (_past_utc(10), sess.id),
        )
        conn.commit()
        conn.close()
        asm_id = _insert_assembly(db, sess.session_key)
        log_assembly_transition(db, sess.id, asm_id, 'session_start', 'tester', 'init')
        # Recent transition means last_activity_at is now, so not abandoned
        assert detect_abandoned_sessions(db, threshold_days=7) == []


# ---------------------------------------------------------------------------
# detect_duplicate_active_sessions
# ---------------------------------------------------------------------------

class TestDetectDuplicateActiveSessions:
    def test_returns_empty_on_db_without_table(self, tmp_path):
        db = _v9_db(tmp_path, 'v9b.db')
        assert detect_duplicate_active_sessions(db) == []

    def test_no_issues_for_single_active_session(self, tmp_path):
        db = _mem_db(tmp_path)
        open_cognition_session(db, _default_policy(), 'tester')
        assert detect_duplicate_active_sessions(db) == []

    def test_detects_two_active_sessions_same_key(self, tmp_path):
        db = _mem_db(tmp_path)
        policy = _default_policy()
        s1 = open_cognition_session(db, policy, 'tester')
        s2 = open_cognition_session(db, policy, 'tester')
        issues = detect_duplicate_active_sessions(db)
        assert len(issues) == 1
        assert issues[0].issue_type == 'duplicate_active_cognition_session'
        assert issues[0].severity == 'warning'
        assert issues[0].metadata['session_key'] == s1.session_key
        assert sorted(issues[0].metadata['session_ids']) == sorted([s1.id, s2.id])
        assert issues[0].metadata['active_count'] == 2

    def test_closing_one_resolves_duplicate(self, tmp_path):
        db = _mem_db(tmp_path)
        policy = _default_policy()
        s1 = open_cognition_session(db, policy, 'tester')
        open_cognition_session(db, policy, 'tester')
        close_cognition_session(db, s1.id, 'resolved', 'tester')
        assert detect_duplicate_active_sessions(db) == []

    def test_two_different_keys_both_duplicated(self, tmp_path):
        db = _mem_db(tmp_path)
        p1 = _default_policy()
        p2 = _default_policy(tags=['x'])
        for _ in range(2):
            open_cognition_session(db, p1, 'tester')
            open_cognition_session(db, p2, 'tester')
        issues = detect_duplicate_active_sessions(db)
        assert len(issues) == 2
        keys = {i.metadata['session_key'] for i in issues}
        assert len(keys) == 2
        for issue in issues:
            assert issue.metadata['active_count'] == 2

    def test_no_duplicate_for_different_policies(self, tmp_path):
        db = _mem_db(tmp_path)
        open_cognition_session(db, _default_policy(), 'tester')
        open_cognition_session(db, _default_policy(tags=['y']), 'tester')
        assert detect_duplicate_active_sessions(db) == []


# ---------------------------------------------------------------------------
# CLI layer (session commands via memory.cli)
# ---------------------------------------------------------------------------

class TestSessionCLI:
    def _run(self, args):
        from memory.cli import build_parser, _COMMANDS
        parser = build_parser()
        parsed = parser.parse_args(args)
        import io
        buf = io.StringIO()
        import sys
        old_stdout = sys.stdout
        sys.stdout = buf
        try:
            _COMMANDS[parsed.command](parsed)
        finally:
            sys.stdout = old_stdout
        return buf.getvalue()

    def test_open_session_returns_json(self, tmp_path):
        db = _mem_db(tmp_path)
        out = self._run(['open-session', '--db', db, '--triggered-by', 'tester'])
        data = json.loads(out)
        assert data['status'] == 'active'
        assert data['assembly_count'] == 0

    def test_list_sessions_returns_json_list(self, tmp_path):
        db = _mem_db(tmp_path)
        self._run(['open-session', '--db', db, '--triggered-by', 'tester'])
        out = self._run(['list-sessions', '--db', db])
        data = json.loads(out)
        assert isinstance(data, list)
        assert len(data) == 1

    def test_close_session_via_cli(self, tmp_path):
        db = _mem_db(tmp_path)
        open_out = self._run(['open-session', '--db', db, '--triggered-by', 'tester'])
        sess_id = json.loads(open_out)['id']
        out = self._run([
            'close-session', '--db', db,
            '--id', str(sess_id),
            '--reason', 'done',
            '--triggered-by', 'tester',
        ])
        data = json.loads(out)
        assert data['status'] == 'closed'

    def test_show_session_returns_session_and_assemblies(self, tmp_path):
        db = _mem_db(tmp_path)
        open_out = self._run(['open-session', '--db', db, '--triggered-by', 'tester'])
        sess_id = json.loads(open_out)['id']
        out = self._run(['show-session', '--db', db, '--id', str(sess_id)])
        data = json.loads(out)
        assert 'session' in data
        assert 'assemblies' in data
        assert data['session']['id'] == sess_id

    def test_replay_session_timeline_returns_empty_list(self, tmp_path):
        db = _mem_db(tmp_path)
        open_out = self._run(['open-session', '--db', db, '--triggered-by', 'tester'])
        sess_id = json.loads(open_out)['id']
        out = self._run(['replay-session-timeline', '--db', db, '--id', str(sess_id)])
        data = json.loads(out)
        assert data == []

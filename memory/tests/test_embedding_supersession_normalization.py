"""
Tests for Phase 6C: event_embeddings supersession normalization (schema v14).

Covers:
- Schema v14: superseded_at, superseded_reason columns on event_embeddings
- mark_superseded() normalized behavior: writes superseded_at, not invalidated_at
- mark_invalidated() unchanged behavior: still writes invalidated_at
- Hard column invariants: superseded_at IS NULL for invalidated; invalidated_at IS NULL
  for post-v14 superseded rows
- Authoritative lifecycle invariant: queries use status, not timestamp presence
- Historical cohort: pre-v14 superseded rows (superseded_at IS NULL) remain valid
- detect_superseded_embeddings_without_active_replacement() governance detector
- build_governance_report() integration
- Regression: compression_artifacts supersession path unchanged
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
    mark_active,
    mark_invalidated,
    mark_superseded,
)
from memory.governance import (
    build_governance_report,
    detect_superseded_embeddings_without_active_replacement,
)
from memory.service import init_db


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / 'emb_sup_test.db')
    init_db(path)
    return path


def _add_event(db_path: str, title: str = 'Test event') -> int:
    ev = service.add_memory_event(
        db_path=db_path,
        event_type='hypothesis',
        title=title,
        summary='Test summary',
        source='test',
        confidence=3,
        status='active',
        created_by='tester',
    )
    return ev.id


def _insert_embedding(
    db_path: str,
    memory_event_id: int,
    content_hash: str = 'aabbccdd11223344',
    status: str = 'candidate',
    model_name: str = 'test-model',
    producer_version: str = '1.0.0',
    adapter_name: str = 'test-adapter',
) -> int:
    """Insert an embedding row directly and return its id."""
    now = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    cur = conn.execute(
        """INSERT INTO event_embeddings
           (memory_event_id, content_hash, vector_json, dimensions,
            model_name, model_version, model_digest, provider_name,
            adapter_name, adapter_version, producer_version,
            status, generated_at, provenance_json)
           VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, '{}')""",
        (
            memory_event_id, content_hash, '[0.1, 0.2, 0.3, 0.4]', 4,
            model_name, '1.0', 'test-provider',
            adapter_name, '1.0', producer_version,
            status, now,
        ),
    )
    emb_id = cur.lastrowid
    conn.commit()
    conn.close()
    return emb_id


def _promote_embedding(db_path: str, emb_id: int) -> None:
    """Transition embedding candidate → active."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    mark_active(conn, 'event_embeddings', emb_id)
    conn.commit()
    conn.close()


def _get_embedding_row(db_path: str, emb_id: int) -> sqlite3.Row:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM event_embeddings WHERE id=?", (emb_id,)
    ).fetchone()
    conn.close()
    return row


# ---------------------------------------------------------------------------
# Schema v14
# ---------------------------------------------------------------------------

class TestSchemaV14:
    def test_schema_version_is_15(self, db):
        conn = sqlite3.connect(db)
        row = conn.execute('SELECT version FROM memory_schema_version').fetchone()
        conn.close()
        assert row[0] == 15

    def test_event_embeddings_has_superseded_at_column(self, db):
        conn = sqlite3.connect(db)
        cols = {r[1] for r in conn.execute('PRAGMA table_info(event_embeddings)')}
        conn.close()
        assert 'superseded_at' in cols

    def test_event_embeddings_has_superseded_reason_column(self, db):
        conn = sqlite3.connect(db)
        cols = {r[1] for r in conn.execute('PRAGMA table_info(event_embeddings)')}
        conn.close()
        assert 'superseded_reason' in cols

    def test_event_embeddings_superseded_at_index_exists(self, db):
        conn = sqlite3.connect(db)
        indices = {r[1] for r in conn.execute('PRAGMA index_list(event_embeddings)')}
        conn.close()
        assert 'idx_embeddings_superseded_at' in indices

    def test_v13_db_migrates_to_v14(self, tmp_path):
        """A DB at version 13 gains v14 supersession columns on event_embeddings."""
        from memory.service import _connect
        db_path = str(tmp_path / 'v13.db')
        conn = _connect(db_path)
        # Minimal v13 DB: event_embeddings without superseded_at/superseded_reason.
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS memory_schema_version (version INTEGER NOT NULL);
            INSERT INTO memory_schema_version (version) VALUES (13);
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
        """)
        conn.commit()
        conn.close()

        init_db(db_path)

        conn = sqlite3.connect(db_path)
        version = conn.execute('SELECT version FROM memory_schema_version').fetchone()[0]
        cols = {r[1] for r in conn.execute('PRAGMA table_info(event_embeddings)')}
        conn.close()
        assert version == 15
        assert 'superseded_at' in cols
        assert 'superseded_reason' in cols

    def test_migration_is_idempotent(self, db):
        """Calling init_db() twice on a v14 DB must not raise."""
        init_db(db)
        conn = sqlite3.connect(db)
        row = conn.execute('SELECT version FROM memory_schema_version').fetchone()
        conn.close()
        assert row[0] == 15

    def test_new_embedding_columns_default_null(self, db):
        """Fresh embeddings have superseded_at IS NULL (not yet superseded)."""
        ev_id = _add_event(db)
        emb_id = _insert_embedding(db, ev_id)
        row = _get_embedding_row(db, emb_id)
        assert row['superseded_at'] is None
        assert row['superseded_reason'] is None


# ---------------------------------------------------------------------------
# mark_superseded() normalized behavior (Phase 6C core)
# ---------------------------------------------------------------------------

class TestMarkSupersededNormalized:
    def test_writes_superseded_at_not_invalidated_at(self, db):
        """mark_superseded() writes superseded_at IS NOT NULL, invalidated_at IS NULL."""
        ev_id = _add_event(db)
        emb_id = _insert_embedding(db, ev_id)
        _promote_embedding(db, emb_id)

        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        now = datetime.now(timezone.utc).isoformat()
        mark_superseded(conn, 'event_embeddings', emb_id, 'model upgrade', now)
        conn.commit()

        row = conn.execute(
            "SELECT status, superseded_at, superseded_reason, invalidated_at "
            "FROM event_embeddings WHERE id=?", (emb_id,)
        ).fetchone()
        conn.close()

        assert row['status'] == 'superseded'
        assert row['superseded_at'] is not None        # normalized: dedicated column
        assert row['superseded_reason'] == 'model upgrade'
        assert row['invalidated_at'] is None            # invariant: not written by supersession

    def test_superseded_at_matches_now_argument(self, db):
        ev_id = _add_event(db)
        emb_id = _insert_embedding(db, ev_id)
        _promote_embedding(db, emb_id)

        now = '2026-05-25T12:00:00+00:00'
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        mark_superseded(conn, 'event_embeddings', emb_id, 'test', now)
        conn.commit()
        row = conn.execute(
            "SELECT superseded_at FROM event_embeddings WHERE id=?", (emb_id,)
        ).fetchone()
        conn.close()
        assert row['superseded_at'] == now

    def test_status_is_superseded_after_call(self, db):
        ev_id = _add_event(db)
        emb_id = _insert_embedding(db, ev_id)
        _promote_embedding(db, emb_id)

        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        mark_superseded(conn, 'event_embeddings', emb_id, 'r', datetime.now(timezone.utc).isoformat())
        conn.commit()
        row = conn.execute("SELECT status FROM event_embeddings WHERE id=?", (emb_id,)).fetchone()
        conn.close()
        assert row['status'] == ArtifactStatus.SUPERSEDED

    def test_raises_on_candidate(self, db):
        ev_id = _add_event(db)
        emb_id = _insert_embedding(db, ev_id)
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        with pytest.raises(GovernanceInvalidationError, match="Only 'active'"):
            mark_superseded(conn, 'event_embeddings', emb_id, 'r', datetime.now(timezone.utc).isoformat())
        conn.close()

    def test_raises_on_already_superseded(self, db):
        ev_id = _add_event(db)
        emb_id = _insert_embedding(db, ev_id)
        _promote_embedding(db, emb_id)

        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        now = datetime.now(timezone.utc).isoformat()
        mark_superseded(conn, 'event_embeddings', emb_id, 'first', now)
        conn.commit()
        with pytest.raises(GovernanceInvalidationError, match="Only 'active'"):
            mark_superseded(conn, 'event_embeddings', emb_id, 'second', now)
        conn.close()

    def test_raises_on_nonexistent(self, db):
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        with pytest.raises(GovernanceInvalidationError, match="not found"):
            mark_superseded(conn, 'event_embeddings', 9999, 'r', datetime.now(timezone.utc).isoformat())
        conn.close()


# ---------------------------------------------------------------------------
# mark_invalidated() unchanged behavior (regression)
# ---------------------------------------------------------------------------

class TestMarkInvalidatedUnchanged:
    def test_writes_invalidated_at_not_superseded_at(self, db):
        """mark_invalidated() is unchanged: writes invalidated_at, not superseded_at."""
        ev_id = _add_event(db)
        emb_id = _insert_embedding(db, ev_id)
        _promote_embedding(db, emb_id)

        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        now = datetime.now(timezone.utc).isoformat()
        mark_invalidated(conn, 'event_embeddings', emb_id, 'anchor changed', now)
        conn.commit()

        row = conn.execute(
            "SELECT status, invalidated_at, invalidated_reason, superseded_at "
            "FROM event_embeddings WHERE id=?", (emb_id,)
        ).fetchone()
        conn.close()

        assert row['status'] == 'invalidated'
        assert row['invalidated_at'] is not None
        assert row['invalidated_reason'] == 'anchor changed'
        assert row['superseded_at'] is None            # supersession column stays NULL

    def test_invalidated_at_matches_now_argument(self, db):
        ev_id = _add_event(db)
        emb_id = _insert_embedding(db, ev_id)
        _promote_embedding(db, emb_id)

        now = '2026-05-25T12:00:00+00:00'
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        mark_invalidated(conn, 'event_embeddings', emb_id, 'test', now)
        conn.commit()
        row = conn.execute(
            "SELECT invalidated_at FROM event_embeddings WHERE id=?", (emb_id,)
        ).fetchone()
        conn.close()
        assert row['invalidated_at'] == now

    def test_candidate_can_be_invalidated(self, db):
        ev_id = _add_event(db)
        emb_id = _insert_embedding(db, ev_id)
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        mark_invalidated(conn, 'event_embeddings', emb_id, 'pre-active rejection', datetime.now(timezone.utc).isoformat())
        conn.commit()
        row = conn.execute("SELECT status FROM event_embeddings WHERE id=?", (emb_id,)).fetchone()
        conn.close()
        assert row['status'] == 'invalidated'


# ---------------------------------------------------------------------------
# Hard column invariants (cross-column mutual exclusion)
# ---------------------------------------------------------------------------

class TestHardColumnInvariants:
    def test_superseded_embedding_has_null_invalidated_at(self, db):
        """status='superseded' via mark_superseded(): invalidated_at IS NULL."""
        ev_id = _add_event(db)
        emb_id = _insert_embedding(db, ev_id)
        _promote_embedding(db, emb_id)

        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        mark_superseded(conn, 'event_embeddings', emb_id, 'r', datetime.now(timezone.utc).isoformat())
        conn.commit()

        row = _get_embedding_row(db, emb_id)
        assert row['status'] == 'superseded'
        assert row['superseded_at'] is not None
        assert row['invalidated_at'] is None

    def test_invalidated_embedding_has_null_superseded_at(self, db):
        """status='invalidated' via mark_invalidated(): superseded_at IS NULL."""
        ev_id = _add_event(db)
        emb_id = _insert_embedding(db, ev_id)
        _promote_embedding(db, emb_id)

        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        mark_invalidated(conn, 'event_embeddings', emb_id, 'anchor changed', datetime.now(timezone.utc).isoformat())
        conn.commit()

        row = _get_embedding_row(db, emb_id)
        assert row['status'] == 'invalidated'
        assert row['invalidated_at'] is not None
        assert row['superseded_at'] is None

    def test_active_embedding_all_terminal_columns_null(self, db):
        """Active embeddings have both invalidated_at and superseded_at NULL."""
        ev_id = _add_event(db)
        emb_id = _insert_embedding(db, ev_id)
        _promote_embedding(db, emb_id)

        row = _get_embedding_row(db, emb_id)
        assert row['status'] == 'active'
        assert row['invalidated_at'] is None
        assert row['superseded_at'] is None

    def test_candidate_embedding_all_terminal_columns_null(self, db):
        """Candidate embeddings have both lifecycle timestamp columns NULL."""
        ev_id = _add_event(db)
        emb_id = _insert_embedding(db, ev_id)
        row = _get_embedding_row(db, emb_id)
        assert row['status'] == 'candidate'
        assert row['invalidated_at'] is None
        assert row['superseded_at'] is None


# ---------------------------------------------------------------------------
# Authoritative lifecycle invariant tests
# ---------------------------------------------------------------------------

class TestAuthoritativeLifecycleInvariant:
    """Tests that verify governance queries use status, not timestamp presence.

    These tests instantiate the approved invariant:
      - status is authoritative
      - timestamps are informational lineage metadata only
      - superseded_at IS NULL does NOT mean non-superseded (pre-v14 cohort)
      - invalidated_at IS NOT NULL on a superseded row does NOT mean invalidated
    """

    def test_status_used_to_detect_supersession_not_superseded_at(self, db):
        """A pre-v14 superseded row (superseded_at IS NULL) is correctly identified
        by querying status='superseded', not superseded_at IS NOT NULL.
        """
        ev_id = _add_event(db)
        emb_id = _insert_embedding(db, ev_id)
        _promote_embedding(db, emb_id)

        # Simulate a pre-v14 supersession: status='superseded' but superseded_at IS NULL,
        # invalidated_at IS NOT NULL (old mark_superseded() behavior).
        now = datetime.now(timezone.utc).isoformat()
        conn = sqlite3.connect(db)
        conn.execute(
            """UPDATE event_embeddings
               SET status='superseded', invalidated_at=?, invalidated_reason='old-style'
               WHERE id=?""",
            (now, emb_id),
        )
        conn.commit()
        conn.close()

        row = _get_embedding_row(db, emb_id)
        assert row['status'] == 'superseded'        # authoritative
        assert row['superseded_at'] is None          # pre-v14 cohort: timestamp absent
        assert row['invalidated_at'] is not None     # historical artifact

        # Governance detection using status (correct):
        conn = sqlite3.connect(db)
        count_by_status = conn.execute(
            "SELECT COUNT(*) FROM event_embeddings WHERE status='superseded'"
        ).fetchone()[0]
        count_by_timestamp = conn.execute(
            "SELECT COUNT(*) FROM event_embeddings WHERE superseded_at IS NOT NULL"
        ).fetchone()[0]
        conn.close()

        assert count_by_status == 1      # correctly detects pre-v14 row
        assert count_by_timestamp == 0   # timestamp-based query misses pre-v14 cohort

    def test_status_used_to_detect_supersession_post_v14(self, db):
        """A post-v14 superseded row is also detected by status, confirming both
        cohorts are captured by status-based queries.
        """
        ev_id = _add_event(db)
        emb_id = _insert_embedding(db, ev_id)
        _promote_embedding(db, emb_id)

        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        mark_superseded(conn, 'event_embeddings', emb_id, 'model upgrade', datetime.now(timezone.utc).isoformat())
        conn.commit()

        row = _get_embedding_row(db, emb_id)
        assert row['status'] == 'superseded'
        assert row['superseded_at'] is not None     # post-v14 cohort: timestamp present
        assert row['invalidated_at'] is None

        conn = sqlite3.connect(db)
        count_by_status = conn.execute(
            "SELECT COUNT(*) FROM event_embeddings WHERE status='superseded'"
        ).fetchone()[0]
        conn.close()
        assert count_by_status == 1

    def test_invalidated_at_on_superseded_row_does_not_imply_invalidated(self, db):
        """Pre-v14 row: invalidated_at IS NOT NULL on a superseded row does not mean
        status='invalidated'. Status is authoritative.
        """
        ev_id = _add_event(db)
        emb_id = _insert_embedding(db, ev_id)
        _promote_embedding(db, emb_id)

        # Simulate pre-v14 supersession.
        now = datetime.now(timezone.utc).isoformat()
        conn = sqlite3.connect(db)
        conn.execute(
            "UPDATE event_embeddings SET status='superseded', invalidated_at=? WHERE id=?",
            (now, emb_id),
        )
        conn.commit()
        conn.close()

        row = _get_embedding_row(db, emb_id)
        # Status is superseded, not invalidated, even though invalidated_at IS NOT NULL.
        assert row['status'] == 'superseded'
        assert row['status'] != 'invalidated'


# ---------------------------------------------------------------------------
# Historical cohort: pre-v14 rows survive migration without backfill
# ---------------------------------------------------------------------------

class TestHistoricalCohortNonBackfill:
    def test_historical_superseded_row_has_null_superseded_at_after_migration(self, tmp_path):
        """A v13 DB with a superseded embedding row is migrated to v14.
        The historical row gains superseded_at column (NULL) — no backfill.
        Status='superseded' is preserved as the authoritative state.
        """
        from memory.service import _connect
        db_path = str(tmp_path / 'historical.db')
        conn = _connect(db_path)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS memory_schema_version (version INTEGER NOT NULL);
            INSERT INTO memory_schema_version VALUES (13);
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
            CREATE TABLE IF NOT EXISTS memory_events_dummy AS SELECT 1;
        """)
        # Insert a superseded embedding using old semantics (invalidated_at populated).
        now = '2026-01-01T00:00:00+00:00'
        conn.execute(
            "INSERT INTO memory_events "
            "(event_type, title, summary, source, confidence, status, "
            " tags_json, related_ids_json, created_by, created_at, updated_at) "
            "VALUES ('hypothesis','T','S','src',3,'active','[]','[]','op',?,?)",
            (now, now),
        )
        ev_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO event_embeddings "
            "(memory_event_id, content_hash, vector_json, dimensions, model_name, "
            " model_version, provider_name, adapter_name, adapter_version, "
            " producer_version, status, generated_at, invalidated_at, invalidated_reason, "
            " provenance_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                ev_id, 'deadbeef12345678', '[0.1]', 4, 'old-model',
                '1.0', 'provider', 'adapter', '1.0',
                '0.9.0', 'superseded', now, now, 'old-style supersession',
                '{}',
            ),
        )
        emb_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.commit()
        conn.close()

        # Migrate to v14.
        init_db(db_path)

        # Verify: status preserved, superseded_at NOT backfilled, invalidated_at preserved.
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        version = conn.execute('SELECT version FROM memory_schema_version').fetchone()[0]
        row = conn.execute(
            "SELECT status, invalidated_at, superseded_at FROM event_embeddings WHERE id=?",
            (emb_id,),
        ).fetchone()
        conn.close()

        assert version == 15
        assert row['status'] == 'superseded'        # authoritative: preserved
        assert row['invalidated_at'] is not None    # historical artifact: preserved
        assert row['superseded_at'] is None          # no backfill: intentional

    def test_historical_status_remains_queryable_post_migration(self, tmp_path):
        """After migration, status='superseded' still finds historical pre-v14 rows."""
        from memory.service import _connect
        db_path = str(tmp_path / 'query_test.db')
        init_db(db_path)

        ev_id = _add_event(db_path)
        emb_id = _insert_embedding(db_path, ev_id)
        _promote_embedding(db_path, emb_id)

        # Simulate pre-v14 row directly.
        now = datetime.now(timezone.utc).isoformat()
        conn = sqlite3.connect(db_path)
        conn.execute(
            "UPDATE event_embeddings SET status='superseded', invalidated_at=? WHERE id=?",
            (now, emb_id),
        )
        conn.commit()

        count = conn.execute(
            "SELECT COUNT(*) FROM event_embeddings WHERE status='superseded'"
        ).fetchone()[0]
        conn.close()
        assert count == 1


# ---------------------------------------------------------------------------
# Governance: detect_superseded_embeddings_without_active_replacement
# ---------------------------------------------------------------------------

class TestDetectSupersededEmbeddingsWithoutActiveReplacement:
    def test_empty_when_no_superseded(self, db):
        issues = detect_superseded_embeddings_without_active_replacement(db)
        assert issues == []

    def test_detects_post_v14_superseded_without_replacement(self, db):
        """Post-v14 superseded embedding with no active replacement is detected via status."""
        ev_id = _add_event(db)
        emb_id = _insert_embedding(db, ev_id, content_hash='aabbccdd11223344')
        _promote_embedding(db, emb_id)

        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        mark_superseded(conn, 'event_embeddings', emb_id, 'model upgrade', datetime.now(timezone.utc).isoformat())
        conn.commit()
        conn.close()

        issues = detect_superseded_embeddings_without_active_replacement(db)
        assert len(issues) == 1
        assert issues[0].issue_type == 'superseded_embedding_without_active_replacement'
        assert issues[0].severity == 'warning'
        assert issues[0].memory_id == ev_id
        assert issues[0].metadata['embedding_id'] == emb_id
        assert issues[0].metadata['content_hash'] == 'aabbccdd11223344'

    def test_detects_pre_v14_superseded_without_replacement(self, db):
        """Pre-v14 superseded embedding (superseded_at IS NULL) is ALSO detected.
        Confirms the detector uses status, not timestamp presence.
        """
        ev_id = _add_event(db)
        emb_id = _insert_embedding(db, ev_id)
        _promote_embedding(db, emb_id)

        # Simulate pre-v14 supersession (old-style: invalidated_at set, superseded_at NULL).
        now = datetime.now(timezone.utc).isoformat()
        conn = sqlite3.connect(db)
        conn.execute(
            "UPDATE event_embeddings SET status='superseded', invalidated_at=? WHERE id=?",
            (now, emb_id),
        )
        conn.commit()
        conn.close()

        issues = detect_superseded_embeddings_without_active_replacement(db)
        assert len(issues) == 1
        assert issues[0].issue_type == 'superseded_embedding_without_active_replacement'

    def test_no_issue_when_active_replacement_exists(self, db):
        ev_id = _add_event(db)
        content_hash = 'aabbccdd11223344'

        # Old embedding (superseded).
        old_emb = _insert_embedding(db, ev_id, content_hash=content_hash, producer_version='1.0.0')
        _promote_embedding(db, old_emb)
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        mark_superseded(conn, 'event_embeddings', old_emb, 'upgrade', datetime.now(timezone.utc).isoformat())
        conn.commit()
        conn.close()

        # New embedding (active replacement for same content_hash).
        new_emb = _insert_embedding(db, ev_id, content_hash=content_hash, producer_version='2.0.0')
        _promote_embedding(db, new_emb)

        issues = detect_superseded_embeddings_without_active_replacement(db)
        assert issues == []

    def test_different_content_hash_does_not_qualify_as_replacement(self, db):
        """An active embedding for a DIFFERENT content_hash is not a replacement."""
        ev_id = _add_event(db)

        old_emb = _insert_embedding(db, ev_id, content_hash='hash_old')
        _promote_embedding(db, old_emb)
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        mark_superseded(conn, 'event_embeddings', old_emb, 'anchor changed', datetime.now(timezone.utc).isoformat())
        conn.commit()
        conn.close()

        # New embedding for different content_hash.
        new_emb = _insert_embedding(db, ev_id, content_hash='hash_new')
        _promote_embedding(db, new_emb)

        issues = detect_superseded_embeddings_without_active_replacement(db)
        assert len(issues) == 1  # old_emb still has no active replacement for its hash

    def test_table_existence_guard(self, tmp_path):
        db_path = str(tmp_path / 'bare.db')
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE memory_events (id INTEGER PRIMARY KEY)")
        conn.commit()
        conn.close()
        issues = detect_superseded_embeddings_without_active_replacement(db_path)
        assert issues == []

    def test_multiple_events_multiple_issues(self, db):
        for i in range(3):
            ev_id = _add_event(db, title=f'Event {i}')
            emb_id = _insert_embedding(db, ev_id, content_hash=f'hash{i:016d}')
            _promote_embedding(db, emb_id)
            conn = sqlite3.connect(db)
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA foreign_keys=ON')
            mark_superseded(conn, 'event_embeddings', emb_id, 'upgrade', datetime.now(timezone.utc).isoformat())
            conn.commit()
            conn.close()
        issues = detect_superseded_embeddings_without_active_replacement(db)
        assert len(issues) == 3


# ---------------------------------------------------------------------------
# build_governance_report() integration
# ---------------------------------------------------------------------------

class TestGovernanceReportIntegration:
    def test_report_includes_embedding_supersession_detector(self, db):
        ev_id = _add_event(db)
        emb_id = _insert_embedding(db, ev_id)
        _promote_embedding(db, emb_id)

        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        mark_superseded(conn, 'event_embeddings', emb_id, 'upgrade', datetime.now(timezone.utc).isoformat())
        conn.commit()
        conn.close()

        report = build_governance_report(db)
        issue_types = {i.issue_type for i in report.issues}
        assert 'superseded_embedding_without_active_replacement' in issue_types

    def test_clean_db_no_embedding_supersession_issues(self, db):
        ev_id = _add_event(db)
        emb_id = _insert_embedding(db, ev_id, content_hash='hash1234567890ab')
        _promote_embedding(db, emb_id)
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        mark_superseded(conn, 'event_embeddings', emb_id, 'upgrade', datetime.now(timezone.utc).isoformat())
        conn.commit()
        conn.close()
        # Add active replacement.
        new_emb = _insert_embedding(db, ev_id, content_hash='hash1234567890ab', producer_version='2.0.0')
        _promote_embedding(db, new_emb)

        report = build_governance_report(db)
        embedding_issues = [
            i for i in report.issues
            if i.issue_type == 'superseded_embedding_without_active_replacement'
        ]
        assert embedding_issues == []


# ---------------------------------------------------------------------------
# Regression: compression_artifacts supersession path unchanged
# ---------------------------------------------------------------------------

class TestCompressionArtifactsRegressionUnchanged:
    def test_compression_supersession_uses_dedicated_columns(self, db):
        """Phase 6C must not change compression_artifacts supersession behavior."""
        from memory.compression import (
            create_compression_artifact,
            promote_compression_artifact,
            supersede_compression_artifact,
            get_compression_artifact,
        )

        now = datetime.now(timezone.utc).isoformat()
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        snapshot = {'governance_context': [], 'unresolved_items': [], 'active_investigations': [], 'relevant_memory': [], 'conflicting_pairs': []}
        cur = conn.execute(
            "INSERT INTO context_assembly_log "
            "(assembly_hash, session_id, assembly_version, assembled_at, db_path, "
            "policy_json, entries_accepted, char_budget_used, char_budget_limit, "
            "assembly_snapshot_json, status) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            ('reg_hash_001', 'sess', '1.0', now, db, '{}', 0, 0, 8000, json.dumps(snapshot), 'active'),
        )
        asm1 = cur.lastrowid
        cur2 = conn.execute(
            "INSERT INTO context_assembly_log "
            "(assembly_hash, session_id, assembly_version, assembled_at, db_path, "
            "policy_json, entries_accepted, char_budget_used, char_budget_limit, "
            "assembly_snapshot_json, status) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            ('reg_hash_002', 'sess', '1.0', now, db, '{}', 0, 0, 8000, json.dumps(snapshot), 'active'),
        )
        asm2 = cur2.lastrowid
        conn.commit()
        conn.close()

        old = create_compression_artifact(db, asm1, 'summary', '1.0.0', 'Old text.', 'tester')
        old = promote_compression_artifact(db, old.id, 'op', 'notes')
        new = create_compression_artifact(db, asm2, 'summary', '1.0.0', 'New text.', 'tester')
        new = promote_compression_artifact(db, new.id, 'op', 'notes')

        superseded = supersede_compression_artifact(db, old.id, new.id, 'model upgrade', 'op')

        assert superseded.status == 'superseded'
        assert superseded.superseded_at is not None
        assert superseded.superseded_reason == 'model upgrade'
        assert superseded.invalidated_at is None  # hard invariant unchanged

    def test_mark_superseded_on_compression_artifacts_writes_superseded_at(self, db):
        """After Phase 6C normalization, mark_superseded() on compression_artifacts
        also writes superseded_at (not invalidated_at). Both governed tables are now
        consistent in their supersession column semantics.
        """
        from memory.compression import (
            create_compression_artifact,
            promote_compression_artifact,
        )

        now_ts = datetime.now(timezone.utc).isoformat()
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        snapshot = {'governance_context': [], 'unresolved_items': [], 'active_investigations': [], 'relevant_memory': [], 'conflicting_pairs': []}
        cur = conn.execute(
            "INSERT INTO context_assembly_log "
            "(assembly_hash, session_id, assembly_version, assembled_at, db_path, "
            "policy_json, entries_accepted, char_budget_used, char_budget_limit, "
            "assembly_snapshot_json, status) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            ('reg_hash_003', 'sess', '1.0', now_ts, db, '{}', 0, 0, 8000, json.dumps(snapshot), 'active'),
        )
        asm_id = cur.lastrowid
        conn.commit()

        artifact = create_compression_artifact(db, asm_id, 'summary', '1.0.0', 'Text.', 'op')
        artifact = promote_compression_artifact(db, artifact.id, 'op', 'notes')

        now = datetime.now(timezone.utc).isoformat()
        mark_superseded(conn, 'compression_artifacts', artifact.id, 'via helper', now)
        conn.commit()

        row = conn.execute(
            "SELECT status, superseded_at, invalidated_at FROM compression_artifacts WHERE id=?",
            (artifact.id,),
        ).fetchone()
        conn.close()

        # Phase 6C: mark_superseded() now writes superseded_at on both tables.
        assert row['status'] == 'superseded'
        assert row['superseded_at'] is not None
        assert row['invalidated_at'] is None

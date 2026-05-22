"""Tests for memory/artifact_governance.py: governance constants, content hash,
schema validation, and invalidation helpers."""
import sqlite3

import pytest

import memory.artifact_governance as gov
from memory.artifact_governance import (
    DERIVED_ARTIFACT_GOVERNANCE_VERSION,
    EMBEDDING_VISIBLE_FIELDS,
    ArtifactStatus,
    GovernanceInvalidationError,
    GovernanceSchemaError,
    VALID_ARTIFACT_STATUSES,
    compute_content_hash,
    mark_invalidated,
    mark_superseded,
    validate_artifact_table_schema,
)


# ---------------------------------------------------------------------------
# ArtifactStatus constants
# ---------------------------------------------------------------------------

class TestArtifactStatusConstants:
    def test_candidate_value(self):
        assert ArtifactStatus.CANDIDATE == 'candidate'

    def test_active_value(self):
        assert ArtifactStatus.ACTIVE == 'active'

    def test_superseded_value(self):
        assert ArtifactStatus.SUPERSEDED == 'superseded'

    def test_invalidated_value(self):
        assert ArtifactStatus.INVALIDATED == 'invalidated'

    def test_valid_artifact_statuses_contains_all_four(self):
        assert VALID_ARTIFACT_STATUSES == {
            'candidate', 'active', 'superseded', 'invalidated'
        }

    def test_governance_version_is_semver_like(self):
        assert isinstance(DERIVED_ARTIFACT_GOVERNANCE_VERSION, str)
        assert DERIVED_ARTIFACT_GOVERNANCE_VERSION.count('.') == 2

    def test_embedding_visible_fields_is_exactly_title_and_summary(self):
        assert EMBEDDING_VISIBLE_FIELDS == frozenset({'title', 'summary'})


# ---------------------------------------------------------------------------
# compute_content_hash
# ---------------------------------------------------------------------------

class TestComputeContentHash:
    def test_hash_is_16_chars(self):
        h = compute_content_hash('hello', 'world')
        assert len(h) == 16

    def test_hash_is_valid_hex(self):
        h = compute_content_hash('hello', 'world')
        int(h, 16)  # raises if not valid hex

    def test_hash_is_deterministic(self):
        h1 = compute_content_hash('title', 'summary')
        h2 = compute_content_hash('title', 'summary')
        assert h1 == h2

    def test_nul_separator_prevents_prefix_collision(self):
        # ('ab', '') must not equal ('a', 'b') — NUL separator prevents this
        h1 = compute_content_hash('ab', '')
        h2 = compute_content_hash('a', 'b')
        assert h1 != h2

    def test_title_change_changes_hash(self):
        h1 = compute_content_hash('original title', 'summary')
        h2 = compute_content_hash('different title', 'summary')
        assert h1 != h2

    def test_summary_change_changes_hash(self):
        h1 = compute_content_hash('title', 'original summary')
        h2 = compute_content_hash('title', 'different summary')
        assert h1 != h2

    def test_empty_strings_produce_valid_hash(self):
        h = compute_content_hash('', '')
        assert len(h) == 16
        int(h, 16)

    def test_unicode_content_is_handled(self):
        h1 = compute_content_hash('résumé', 'naïve')
        h2 = compute_content_hash('résumé', 'naïve')
        assert h1 == h2
        assert len(h1) == 16


# ---------------------------------------------------------------------------
# validate_artifact_table_schema
# ---------------------------------------------------------------------------

def _make_conn(tmp_path, ddl: str) -> sqlite3.Connection:
    """Helper: create a fresh in-process SQLite DB and return an open connection."""
    conn = sqlite3.connect(str(tmp_path / 'gov_test.db'))
    conn.executescript(ddl)
    conn.commit()
    return conn


class TestValidateArtifactTableSchema:
    def test_passes_on_compliant_table(self, tmp_path):
        conn = _make_conn(tmp_path, """
            CREATE TABLE test_art (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                status TEXT NOT NULL DEFAULT 'active',
                producer_version TEXT NOT NULL,
                source_hash TEXT NOT NULL,
                generated_at TEXT NOT NULL
            );
            CREATE INDEX idx_test_art_status ON test_art(status);
        """)
        validate_artifact_table_schema(
            conn, 'test_art',
            required_columns=['id', 'status', 'producer_version', 'source_hash', 'generated_at'],
            required_indices=['idx_test_art_status'],
        )
        conn.close()

    def test_fails_on_missing_column(self, tmp_path):
        conn = _make_conn(tmp_path, """
            CREATE TABLE test_art (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                status TEXT NOT NULL DEFAULT 'active'
            );
        """)
        with pytest.raises(GovernanceSchemaError, match="missing required governance columns"):
            validate_artifact_table_schema(
                conn, 'test_art',
                required_columns=['id', 'status', 'producer_version'],
            )
        conn.close()

    def test_fails_on_missing_index(self, tmp_path):
        conn = _make_conn(tmp_path, """
            CREATE TABLE test_art (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                status TEXT NOT NULL DEFAULT 'active'
            );
        """)
        with pytest.raises(GovernanceSchemaError, match="missing required governance indices"):
            validate_artifact_table_schema(
                conn, 'test_art',
                required_columns=['id', 'status'],
                required_indices=['idx_test_art_status'],
            )
        conn.close()

    def test_passes_when_no_indices_required(self, tmp_path):
        conn = _make_conn(tmp_path, """
            CREATE TABLE test_art (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                status TEXT NOT NULL DEFAULT 'active'
            );
        """)
        validate_artifact_table_schema(
            conn, 'test_art',
            required_columns=['id', 'status'],
            required_indices=None,
        )
        conn.close()

    def test_error_message_names_missing_columns(self, tmp_path):
        conn = _make_conn(tmp_path, """
            CREATE TABLE test_art (id INTEGER PRIMARY KEY AUTOINCREMENT);
        """)
        with pytest.raises(GovernanceSchemaError) as exc_info:
            validate_artifact_table_schema(
                conn, 'test_art',
                required_columns=['status', 'producer_version'],
            )
        assert 'producer_version' in str(exc_info.value)
        conn.close()


# ---------------------------------------------------------------------------
# Helpers for invalidation tests: create a synthetic governed table
# ---------------------------------------------------------------------------

_SYNTHETIC_DDL = """
    CREATE TABLE test_governed (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        status           TEXT    NOT NULL DEFAULT 'active',
        invalidated_at   TEXT,
        invalidated_reason TEXT
    );
"""


def _governed_conn(tmp_path):
    conn = sqlite3.connect(str(tmp_path / 'gov.db'))
    conn.executescript(_SYNTHETIC_DDL)
    conn.commit()
    return conn


def _insert_row(conn, status='active'):
    cur = conn.execute(
        "INSERT INTO test_governed (status) VALUES (?)", (status,)
    )
    conn.commit()
    return cur.lastrowid


# ---------------------------------------------------------------------------
# mark_invalidated
# ---------------------------------------------------------------------------

class TestMarkInvalidated:
    def test_rejects_unknown_table(self, tmp_path):
        conn = _governed_conn(tmp_path)
        with pytest.raises(GovernanceSchemaError, match="not in the governed artifact table allowlist"):
            mark_invalidated(conn, 'retrieval_log', 1, 'reason', '2026-01-01T00:00:00Z')
        conn.close()

    def test_sets_status_to_invalidated(self, tmp_path, monkeypatch):
        monkeypatch.setattr(gov, '_GOVERNED_ARTIFACT_TABLES', frozenset({'test_governed'}))
        conn = _governed_conn(tmp_path)
        row_id = _insert_row(conn, 'active')
        mark_invalidated(conn, 'test_governed', row_id, 'source changed', '2026-01-01T00:00:00Z')
        row = conn.execute(
            "SELECT status, invalidated_at, invalidated_reason FROM test_governed WHERE id=?",
            (row_id,)
        ).fetchone()
        assert row[0] == 'invalidated'
        assert row[1] == '2026-01-01T00:00:00Z'
        assert row[2] == 'source changed'
        conn.close()

    def test_valid_from_candidate(self, tmp_path, monkeypatch):
        monkeypatch.setattr(gov, '_GOVERNED_ARTIFACT_TABLES', frozenset({'test_governed'}))
        conn = _governed_conn(tmp_path)
        row_id = _insert_row(conn, 'candidate')
        mark_invalidated(conn, 'test_governed', row_id, 'rejected', '2026-01-01T00:00:00Z')
        row = conn.execute(
            "SELECT status FROM test_governed WHERE id=?", (row_id,)
        ).fetchone()
        assert row[0] == 'invalidated'
        conn.close()

    def test_is_append_only_from_invalidated(self, tmp_path, monkeypatch):
        monkeypatch.setattr(gov, '_GOVERNED_ARTIFACT_TABLES', frozenset({'test_governed'}))
        conn = _governed_conn(tmp_path)
        row_id = _insert_row(conn, 'active')
        mark_invalidated(conn, 'test_governed', row_id, 'first', '2026-01-01T00:00:00Z')
        with pytest.raises(GovernanceInvalidationError, match="terminal"):
            mark_invalidated(conn, 'test_governed', row_id, 'second', '2026-01-02T00:00:00Z')
        conn.close()

    def test_is_append_only_from_superseded(self, tmp_path, monkeypatch):
        monkeypatch.setattr(gov, '_GOVERNED_ARTIFACT_TABLES', frozenset({'test_governed'}))
        conn = _governed_conn(tmp_path)
        row_id = _insert_row(conn, 'superseded')
        with pytest.raises(GovernanceInvalidationError, match="terminal"):
            mark_invalidated(conn, 'test_governed', row_id, 'reason', '2026-01-01T00:00:00Z')
        conn.close()

    def test_raises_on_missing_row(self, tmp_path, monkeypatch):
        monkeypatch.setattr(gov, '_GOVERNED_ARTIFACT_TABLES', frozenset({'test_governed'}))
        conn = _governed_conn(tmp_path)
        with pytest.raises(GovernanceInvalidationError, match="not found"):
            mark_invalidated(conn, 'test_governed', 9999, 'reason', '2026-01-01T00:00:00Z')
        conn.close()


# ---------------------------------------------------------------------------
# mark_superseded
# ---------------------------------------------------------------------------

class TestMarkSuperseded:
    def test_rejects_unknown_table(self, tmp_path):
        conn = _governed_conn(tmp_path)
        with pytest.raises(GovernanceSchemaError, match="not in the governed artifact table allowlist"):
            mark_superseded(conn, 'retrieval_log', 1, 'reason', '2026-01-01T00:00:00Z')
        conn.close()

    def test_sets_status_to_superseded(self, tmp_path, monkeypatch):
        monkeypatch.setattr(gov, '_GOVERNED_ARTIFACT_TABLES', frozenset({'test_governed'}))
        conn = _governed_conn(tmp_path)
        row_id = _insert_row(conn, 'active')
        mark_superseded(conn, 'test_governed', row_id, 'model upgraded', '2026-06-01T00:00:00Z')
        row = conn.execute(
            "SELECT status, invalidated_at, invalidated_reason FROM test_governed WHERE id=?",
            (row_id,)
        ).fetchone()
        assert row[0] == 'superseded'
        assert row[1] == '2026-06-01T00:00:00Z'
        assert row[2] == 'model upgraded'
        conn.close()

    def test_invalid_from_candidate(self, tmp_path, monkeypatch):
        monkeypatch.setattr(gov, '_GOVERNED_ARTIFACT_TABLES', frozenset({'test_governed'}))
        conn = _governed_conn(tmp_path)
        row_id = _insert_row(conn, 'candidate')
        with pytest.raises(GovernanceInvalidationError, match="Only 'active'"):
            mark_superseded(conn, 'test_governed', row_id, 'reason', '2026-01-01T00:00:00Z')
        conn.close()

    def test_is_append_only_from_superseded(self, tmp_path, monkeypatch):
        monkeypatch.setattr(gov, '_GOVERNED_ARTIFACT_TABLES', frozenset({'test_governed'}))
        conn = _governed_conn(tmp_path)
        row_id = _insert_row(conn, 'superseded')
        with pytest.raises(GovernanceInvalidationError, match="Only 'active'"):
            mark_superseded(conn, 'test_governed', row_id, 'again', '2026-01-02T00:00:00Z')
        conn.close()

    def test_is_append_only_from_invalidated(self, tmp_path, monkeypatch):
        monkeypatch.setattr(gov, '_GOVERNED_ARTIFACT_TABLES', frozenset({'test_governed'}))
        conn = _governed_conn(tmp_path)
        row_id = _insert_row(conn, 'invalidated')
        with pytest.raises(GovernanceInvalidationError, match="Only 'active'"):
            mark_superseded(conn, 'test_governed', row_id, 'reason', '2026-01-01T00:00:00Z')
        conn.close()

    def test_raises_on_missing_row(self, tmp_path, monkeypatch):
        monkeypatch.setattr(gov, '_GOVERNED_ARTIFACT_TABLES', frozenset({'test_governed'}))
        conn = _governed_conn(tmp_path)
        with pytest.raises(GovernanceInvalidationError, match="not found"):
            mark_superseded(conn, 'test_governed', 9999, 'reason', '2026-01-01T00:00:00Z')
        conn.close()

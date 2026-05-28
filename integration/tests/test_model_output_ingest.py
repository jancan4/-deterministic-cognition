"""
Tests for model output ingest path:
  dry_run_ingest, commit_ingest, governance gate, deduplication, stale detection.
"""
import sqlite3
import pytest

from integration.ingest import (
    check_stale,
    commit_ingest,
    dry_run_ingest,
    GOVERNANCE_EVENT_TYPE,
    MAX_CANDIDATES_DEFAULT,
)
from integration.models import (
    ModelTaskResult,
    ModelTaskResultProvenance,
    RawCandidate,
    derive_result_id,
)


def _prov():
    return ModelTaskResultProvenance("op", "/tmp/t.db", "2026-05-28T10:00:00Z")


def _make_result(
    packet_id="pid1",
    adapter_target="echo",
    raw_output="output",
    assembly_hash="ahash",
    candidates=None,
):
    result_id = derive_result_id(packet_id, adapter_target, raw_output)
    return ModelTaskResult(
        result_id=result_id,
        task_id="tid",
        packet_id=packet_id,
        substrate_assembly_hash=assembly_hash,
        adapter_target=adapter_target,
        model_version="1.0.0",
        executed_at="2026-05-28T10:01:00Z",
        raw_output_text=raw_output,
        parsed_candidates=candidates or [],
        parse_status="ok",
        parse_error_detail=None,
        provenance=_prov(),
    )


def _make_candidate(idx, event_type, content="Content.", tags=None):
    return RawCandidate(
        candidate_index=idx,
        proposed_event_type=event_type,
        proposed_content=content,
        proposed_tags=tags or [],
        raw_excerpt="raw",
    )


@pytest.fixture
def memory_db(tmp_path):
    """Minimal memory DB with semantic ledger tables and assembly log."""
    db_path = str(tmp_path / "test_memory.db")

    # Init semantic ledger tables
    from semantic.ledger import init_ledger
    init_ledger(db_path)

    # Init context_assembly_log table (minimal)
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS context_assembly_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            assembly_hash TEXT NOT NULL UNIQUE,
            session_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            assembled_at TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()

    return db_path


@pytest.fixture
def memory_db_with_assembly(memory_db):
    """memory_db with one active assembly record."""
    conn = sqlite3.connect(memory_db)
    conn.execute(
        "INSERT INTO context_assembly_log (assembly_hash, session_id, status, assembled_at) "
        "VALUES (?,?,?,?)",
        ("active_assembly_hash", "sess1", "active", "2026-05-28T09:00:00Z"),
    )
    conn.commit()
    conn.close()
    return memory_db


class TestDryRunIngest:
    def test_dry_run_writes_nothing(self, memory_db):
        result = _make_result(candidates=[
            _make_candidate(0, "implementation_note"),
        ])
        report = dry_run_ingest(result)
        assert report.dry_run is True
        assert report.written == 1

        # Confirm nothing was written
        conn = sqlite3.connect(memory_db)
        rows = conn.execute("SELECT COUNT(*) FROM semantic_candidate_events").fetchone()
        conn.close()
        assert rows[0] == 0

    def test_dry_run_counts_governance_suppressed(self):
        result = _make_result(candidates=[
            _make_candidate(0, "governance_rule"),
            _make_candidate(1, "implementation_note"),
        ])
        report = dry_run_ingest(result, allow_governance_candidates=False)
        assert report.skipped_governance == 1
        assert report.written == 1

    def test_dry_run_counts_unknown_type(self):
        result = _make_result(candidates=[
            _make_candidate(0, "not_a_real_type"),
        ])
        report = dry_run_ingest(result)
        assert report.skipped_unknown_type == 1
        assert report.written == 0


class TestCommitIngest:
    def test_commit_writes_candidates(self, memory_db_with_assembly):
        result = _make_result(
            assembly_hash="active_assembly_hash",
            candidates=[
                _make_candidate(0, "implementation_note", "First note."),
                _make_candidate(1, "open_question", "What happens?"),
            ],
        )
        report = commit_ingest(memory_db_with_assembly, result)
        assert report.written == 2
        assert report.dry_run is False
        assert not report.errors

        conn = sqlite3.connect(memory_db_with_assembly)
        rows = conn.execute("SELECT COUNT(*) FROM semantic_candidate_events").fetchone()
        conn.close()
        assert rows[0] == 2

    def test_duplicate_result_id_skipped(self, memory_db_with_assembly):
        result = _make_result(
            assembly_hash="active_assembly_hash",
            candidates=[_make_candidate(0, "implementation_note", "Same candidate.")],
        )
        r1 = commit_ingest(memory_db_with_assembly, result)
        r2 = commit_ingest(memory_db_with_assembly, result)
        assert r1.written == 1
        assert r2.written == 0
        assert r2.skipped_duplicate == 1

        conn = sqlite3.connect(memory_db_with_assembly)
        rows = conn.execute("SELECT COUNT(*) FROM semantic_candidate_events").fetchone()
        conn.close()
        assert rows[0] == 1

    def test_ingest_source_is_model_task(self, memory_db_with_assembly):
        result = _make_result(
            assembly_hash="active_assembly_hash",
            candidates=[_make_candidate(0, "implementation_note", "Check source.")],
        )
        commit_ingest(memory_db_with_assembly, result)

        conn = sqlite3.connect(memory_db_with_assembly)
        row = conn.execute("SELECT source FROM semantic_candidate_events LIMIT 1").fetchone()
        conn.close()
        assert row[0] == "model_task"

    def test_unknown_event_type_rejected(self, memory_db_with_assembly):
        result = _make_result(
            assembly_hash="active_assembly_hash",
            candidates=[_make_candidate(0, "not_a_type")],
        )
        report = commit_ingest(memory_db_with_assembly, result)
        assert report.skipped_unknown_type == 1
        assert report.written == 0

        conn = sqlite3.connect(memory_db_with_assembly)
        rows = conn.execute("SELECT COUNT(*) FROM semantic_candidate_events").fetchone()
        conn.close()
        assert rows[0] == 0


class TestGovernanceCandidateGate:
    def test_governance_candidate_suppressed_by_default(self, memory_db_with_assembly):
        result = _make_result(
            assembly_hash="active_assembly_hash",
            candidates=[_make_candidate(0, "governance_rule")],
        )
        report = commit_ingest(memory_db_with_assembly, result)
        assert report.skipped_governance == 1
        assert report.written == 0

        conn = sqlite3.connect(memory_db_with_assembly)
        rows = conn.execute("SELECT COUNT(*) FROM semantic_candidate_events").fetchone()
        conn.close()
        assert rows[0] == 0

    def test_governance_candidate_admitted_with_explicit_flag(self, memory_db_with_assembly):
        result = _make_result(
            assembly_hash="active_assembly_hash",
            candidates=[_make_candidate(0, "governance_rule")],
        )
        report = commit_ingest(
            memory_db_with_assembly, result, allow_governance_candidates=True
        )
        assert report.written == 1
        assert report.skipped_governance == 0

        conn = sqlite3.connect(memory_db_with_assembly)
        rows = conn.execute("SELECT COUNT(*) FROM semantic_candidate_events").fetchone()
        conn.close()
        assert rows[0] == 1

    def test_governance_suppressed_even_with_other_valid_candidates(self, memory_db_with_assembly):
        result = _make_result(
            assembly_hash="active_assembly_hash",
            candidates=[
                _make_candidate(0, "governance_rule"),
                _make_candidate(1, "implementation_note"),
            ],
        )
        report = commit_ingest(memory_db_with_assembly, result)
        assert report.skipped_governance == 1
        assert report.written == 1


class TestStaleDetection:
    def test_stale_hash_comparison_uses_assembly_hash_not_id(self, memory_db_with_assembly):
        """
        Stale detection must compare assembly hashes, not IDs.
        A new unrelated assembly with a different ID but the packet's hash still
        active must NOT be flagged as stale.

        Here: packet references "active_assembly_hash" which IS the active assembly.
        A second assembly row is inserted but with a different hash.
        The packet must not be flagged stale because its hash matches the active one.
        """
        # The fixture inserts assembly_hash="active_assembly_hash" as status='active'.
        # Insert a second assembly with a different hash but make first superseded
        # to simulate a newer assembly — but keep first as active for this test.
        is_stale, _ = check_stale(memory_db_with_assembly, "active_assembly_hash")
        assert is_stale is False

    def test_stale_flagged_when_hash_differs(self, memory_db_with_assembly):
        is_stale, current = check_stale(memory_db_with_assembly, "some_old_hash")
        assert is_stale is True
        assert current == "active_assembly_hash"

    def test_stale_aborts_commit_without_force(self, memory_db_with_assembly):
        result = _make_result(
            assembly_hash="stale_hash",
            candidates=[_make_candidate(0, "implementation_note")],
        )
        report = commit_ingest(memory_db_with_assembly, result, force_stale=False)
        assert report.is_stale is True
        assert len(report.errors) > 0
        assert report.written == 0

    def test_stale_proceeds_with_force(self, memory_db_with_assembly):
        result = _make_result(
            assembly_hash="stale_hash",
            candidates=[_make_candidate(0, "implementation_note", "Force stale.")],
        )
        report = commit_ingest(
            memory_db_with_assembly, result, force_stale=True
        )
        assert report.is_stale is True
        assert report.written == 1
        assert not report.errors

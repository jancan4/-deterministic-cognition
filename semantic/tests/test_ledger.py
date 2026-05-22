"""Tests for semantic/ledger.py."""
import json
import os
import sqlite3
import tempfile

import pytest

from models.adapters import EchoModelAdapter, StubModelAdapter
from semantic.ledger import (
    LedgerError,
    LedgerNotFoundError,
    SemanticCandidateEvent,
    SemanticExecutionRun,
    VALID_CANDIDATE_STATUSES,
    VALID_RUN_STATUSES,
    derive_candidate_id,
    get_candidate,
    get_run,
    init_ledger,
    list_candidates,
    list_runs,
    promote_candidate,
    record_run,
    update_candidate_status,
)
from semantic.pipeline import run_semantic_task


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def db(tmp_path):
    return str(tmp_path / 'test.db')


def _make_result(
    task_type='tagging',
    text='The Federal Reserve held rates steady.',
    adapter=None,
):
    if adapter is None:
        adapter = StubModelAdapter()
    return run_semantic_task(task_type, text, adapter)


# ---------------------------------------------------------------------------
# init_ledger
# ---------------------------------------------------------------------------

class TestInitLedger:
    def test_creates_tables(self, db):
        init_ledger(db)
        conn = sqlite3.connect(db)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        conn.close()
        assert 'semantic_execution_runs' in tables
        assert 'semantic_candidate_events' in tables

    def test_creates_indexes(self, db):
        init_ledger(db)
        conn = sqlite3.connect(db)
        indexes = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        )}
        conn.close()
        assert 'idx_sem_runs_adapter' in indexes
        assert 'idx_sem_cands_run_id' in indexes

    def test_idempotent(self, db):
        init_ledger(db)
        init_ledger(db)  # second call must not raise

    def test_no_rows_after_init(self, db):
        init_ledger(db)
        conn = sqlite3.connect(db)
        count = conn.execute('SELECT COUNT(*) FROM semantic_execution_runs').fetchone()[0]
        conn.close()
        assert count == 0


# ---------------------------------------------------------------------------
# record_run — basic
# ---------------------------------------------------------------------------

class TestRecordRunBasic:
    def test_returns_semantic_execution_run(self, db):
        r = _make_result()
        result = record_run(db, r)
        assert isinstance(result, SemanticExecutionRun)

    def test_run_id_equals_request_id(self, db):
        r = _make_result()
        result = record_run(db, r)
        assert result.run_id == r.execution_result.request_id

    def test_task_fields_preserved(self, db):
        r = _make_result(task_type='tagging', text='test input here')
        result = record_run(db, r)
        assert result.task_type == 'tagging'
        assert result.input_text == 'test input here'
        assert result.task_id == r.task.task_id

    def test_adapter_fields_preserved(self, db):
        r = _make_result()
        result = record_run(db, r)
        assert result.adapter_name == 'stub'
        assert result.adapter_version == '1.0.0'

    def test_status_completed_on_success(self, db):
        r = _make_result()
        assert r.success is True
        result = record_run(db, r)
        assert result.status == 'completed'

    def test_candidate_count_stored(self, db):
        r = _make_result()
        result = record_run(db, r)
        assert result.candidate_count == len(r.candidates)

    def test_promoted_count_zero_initially(self, db):
        r = _make_result()
        result = record_run(db, r)
        assert result.promoted_count == 0

    def test_normalized_result_stored(self, db):
        r = _make_result()
        result = record_run(db, r)
        assert isinstance(result.normalized_result, dict)
        assert 'labels' in result.normalized_result

    def test_input_hash_16_chars(self, db):
        r = _make_result()
        result = record_run(db, r)
        assert len(result.input_hash) == 16

    def test_raw_output_is_none(self, db):
        """Raw output is reserved for real adapters; stub produces None."""
        r = _make_result()
        result = record_run(db, r)
        assert result.raw_output is None

    def test_execution_policy_stored(self, db):
        r = _make_result()
        result = record_run(db, r, execution_policy={'timeout_seconds': 30})
        assert result.execution_policy['timeout_seconds'] == 30

    def test_model_metadata_stored(self, db):
        r = _make_result()
        result = record_run(db, r, model_metadata={'quantization': 'q4_k_m'})
        assert result.model_metadata['quantization'] == 'q4_k_m'

    def test_source_id_stored(self, db):
        r = run_semantic_task('tagging', 'text here', StubModelAdapter(), source_id='src-001')
        result = record_run(db, r)
        assert result.source_id == 'src-001'

    def test_source_id_none_when_absent(self, db):
        r = _make_result()
        result = record_run(db, r)
        assert result.source_id is None


# ---------------------------------------------------------------------------
# record_run — idempotency
# ---------------------------------------------------------------------------

class TestRecordRunIdempotency:
    def test_second_call_same_input_returns_existing(self, db):
        r = _make_result()
        result1 = record_run(db, r)
        result2 = record_run(db, r)
        assert result1.run_id == result2.run_id

    def test_second_call_does_not_increment_rows(self, db):
        r = _make_result()
        record_run(db, r)
        record_run(db, r)
        conn = sqlite3.connect(db)
        count = conn.execute('SELECT COUNT(*) FROM semantic_execution_runs').fetchone()[0]
        conn.close()
        assert count == 1

    def test_second_call_does_not_duplicate_candidates(self, db):
        r = _make_result()
        record_run(db, r)
        record_run(db, r)
        conn = sqlite3.connect(db)
        count = conn.execute('SELECT COUNT(*) FROM semantic_candidate_events').fetchone()[0]
        conn.close()
        assert count == len(r.candidates)


# ---------------------------------------------------------------------------
# record_run — candidate rows
# ---------------------------------------------------------------------------

class TestRecordRunCandidateRows:
    def test_one_candidate_row_per_candidate(self, db):
        r = _make_result()
        record_run(db, r)
        conn = sqlite3.connect(db)
        count = conn.execute('SELECT COUNT(*) FROM semantic_candidate_events').fetchone()[0]
        conn.close()
        assert count == len(r.candidates)

    def test_candidate_status_is_candidate(self, db):
        r = _make_result()
        record_run(db, r)
        cands = list_candidates(db)
        for c in cands:
            assert c.status == 'candidate'

    def test_candidate_promoted_memory_id_is_null(self, db):
        r = _make_result()
        record_run(db, r)
        cands = list_candidates(db)
        for c in cands:
            assert c.promoted_memory_id is None

    def test_candidate_extraction_method(self, db):
        r = _make_result()
        record_run(db, r)
        cands = list_candidates(db)
        for c in cands:
            assert 'stub' in c.extraction_method

    def test_candidate_event_type(self, db):
        r = _make_result()
        record_run(db, r)
        cands = list_candidates(db)
        for c in cands:
            assert c.event_type == 'hypothesis'  # SEMANTIC_DEFAULT_EVENT_TYPE


# ---------------------------------------------------------------------------
# derive_candidate_id
# ---------------------------------------------------------------------------

class TestDeriveCandidateId:
    def test_deterministic(self):
        id1 = derive_candidate_id('abc123', 0)
        id2 = derive_candidate_id('abc123', 0)
        assert id1 == id2

    def test_16_chars(self):
        assert len(derive_candidate_id('somerunid', 0)) == 16

    def test_index_differentiates(self):
        id0 = derive_candidate_id('same-run', 0)
        id1 = derive_candidate_id('same-run', 1)
        assert id0 != id1

    def test_run_id_differentiates(self):
        id_a = derive_candidate_id('run-a', 0)
        id_b = derive_candidate_id('run-b', 0)
        assert id_a != id_b


# ---------------------------------------------------------------------------
# input_hash independence
# ---------------------------------------------------------------------------

class TestInputHash:
    def test_same_text_different_adapters_same_hash(self, db):
        text = 'The Fed held rates.'
        r_stub = run_semantic_task('tagging', text, StubModelAdapter())
        r_echo = run_semantic_task('tagging', text, EchoModelAdapter())
        run_stub = record_run(db, r_stub)
        run_echo = record_run(db, r_echo)
        assert run_stub.input_hash == run_echo.input_hash
        assert run_stub.run_id != run_echo.run_id

    def test_different_text_different_hash(self, db):
        r1 = run_semantic_task('tagging', 'text one', StubModelAdapter())
        r2 = run_semantic_task('tagging', 'text two', StubModelAdapter())
        run1 = record_run(db, r1)
        run2 = record_run(db, r2)
        assert run1.input_hash != run2.input_hash


# ---------------------------------------------------------------------------
# get_run / list_runs
# ---------------------------------------------------------------------------

class TestGetRun:
    def test_get_existing(self, db):
        r = _make_result()
        record_run(db, r)
        fetched = get_run(db, r.execution_result.request_id)
        assert fetched is not None
        assert fetched.run_id == r.execution_result.request_id

    def test_get_nonexistent_returns_none(self, db):
        init_ledger(db)
        assert get_run(db, 'doesnotexist') is None


class TestListRuns:
    def test_empty_db(self, db):
        assert list_runs(db) == []

    def test_returns_all_by_default(self, db):
        for text in ('hello world', 'fed raised rates', 'ecb holds'):
            r = run_semantic_task('tagging', text, StubModelAdapter())
            record_run(db, r)
        assert len(list_runs(db)) == 3

    def test_filter_by_adapter(self, db):
        r_stub = run_semantic_task('tagging', 'test text', StubModelAdapter())
        r_echo = run_semantic_task('tagging', 'test text two', EchoModelAdapter())
        record_run(db, r_stub)
        record_run(db, r_echo)
        assert len(list_runs(db, adapter_name='stub')) == 1
        assert len(list_runs(db, adapter_name='echo')) == 1

    def test_filter_by_task_type(self, db):
        r1 = run_semantic_task('tagging', 'hello world', StubModelAdapter())
        r2 = run_semantic_task('entity_extraction', 'hello world two', StubModelAdapter())
        record_run(db, r1)
        record_run(db, r2)
        assert len(list_runs(db, task_type='tagging')) == 1
        assert len(list_runs(db, task_type='entity_extraction')) == 1

    def test_filter_by_status(self, db):
        r = _make_result()
        record_run(db, r)
        assert len(list_runs(db, status='completed')) == 1
        assert len(list_runs(db, status='failed')) == 0

    def test_invalid_status_raises(self, db):
        with pytest.raises(LedgerError, match='Invalid run status'):
            list_runs(db, status='nonsense')

    def test_sorted_newest_first(self, db):
        texts = ['first text', 'second text', 'third text']
        for text in texts:
            r = run_semantic_task('tagging', text, StubModelAdapter())
            record_run(db, r)
        runs = list_runs(db)
        dates = [r.started_at for r in runs]
        assert dates == sorted(dates, reverse=True)


# ---------------------------------------------------------------------------
# list_candidates
# ---------------------------------------------------------------------------

class TestListCandidates:
    def test_empty_db(self, db):
        assert list_candidates(db) == []

    def test_filter_by_run_id(self, db):
        r1 = run_semantic_task('tagging', 'text one', StubModelAdapter())
        r2 = run_semantic_task('tagging', 'text two', StubModelAdapter())
        record_run(db, r1)
        record_run(db, r2)
        cands = list_candidates(db, run_id=r1.execution_result.request_id)
        for c in cands:
            assert c.semantic_run_id == r1.execution_result.request_id

    def test_filter_by_status(self, db):
        r = _make_result()
        record_run(db, r)
        cands = list_candidates(db, status='candidate')
        assert len(cands) >= 1
        assert list_candidates(db, status='promoted') == []

    def test_invalid_status_raises(self, db):
        with pytest.raises(LedgerError, match='Invalid candidate status'):
            list_candidates(db, status='pending')

    def test_ordered_by_run_and_index(self, db):
        r = _make_result()
        record_run(db, r)
        cands = list_candidates(db, run_id=r.execution_result.request_id)
        indexes = [c.candidate_index for c in cands]
        assert indexes == sorted(indexes)


# ---------------------------------------------------------------------------
# update_candidate_status
# ---------------------------------------------------------------------------

class TestUpdateCandidateStatus:
    def _get_first_candidate_id(self, db, run_result):
        run_id = run_result.execution_result.request_id
        return derive_candidate_id(run_id, 0)

    def test_candidate_to_rejected(self, db):
        r = _make_result()
        record_run(db, r)
        cid = self._get_first_candidate_id(db, r)
        updated = update_candidate_status(db, cid, 'rejected')
        assert updated.status == 'rejected'
        assert updated.promoted_memory_id is None

    def test_candidate_to_promoted_requires_memory_id(self, db):
        r = _make_result()
        record_run(db, r)
        cid = self._get_first_candidate_id(db, r)
        with pytest.raises(LedgerError, match='promoted_memory_id is required'):
            update_candidate_status(db, cid, 'promoted')

    def test_candidate_to_promoted_with_memory_id(self, db):
        r = _make_result()
        record_run(db, r)
        cid = self._get_first_candidate_id(db, r)
        updated = update_candidate_status(db, cid, 'promoted', promoted_memory_id=42)
        assert updated.status == 'promoted'
        assert updated.promoted_memory_id == 42

    def test_rejected_with_memory_id_raises(self, db):
        r = _make_result()
        record_run(db, r)
        cid = self._get_first_candidate_id(db, r)
        with pytest.raises(LedgerError, match='must be None'):
            update_candidate_status(db, cid, 'rejected', promoted_memory_id=1)

    def test_promoted_cannot_be_transitioned_again(self, db):
        r = _make_result()
        record_run(db, r)
        cid = self._get_first_candidate_id(db, r)
        update_candidate_status(db, cid, 'promoted', promoted_memory_id=1)
        with pytest.raises(LedgerError, match="Cannot transition"):
            update_candidate_status(db, cid, 'rejected')

    def test_rejected_cannot_be_transitioned_again(self, db):
        r = _make_result()
        record_run(db, r)
        cid = self._get_first_candidate_id(db, r)
        update_candidate_status(db, cid, 'rejected')
        with pytest.raises(LedgerError, match="Cannot transition"):
            update_candidate_status(db, cid, 'rejected')

    def test_invalid_new_status_raises(self, db):
        r = _make_result()
        record_run(db, r)
        cid = self._get_first_candidate_id(db, r)
        with pytest.raises(LedgerError, match="must be 'promoted' or 'rejected'"):
            update_candidate_status(db, cid, 'candidate')

    def test_unknown_candidate_raises(self, db):
        init_ledger(db)
        with pytest.raises(LedgerNotFoundError):
            update_candidate_status(db, 'doesnotexist', 'rejected')

    def test_promoted_increments_run_promoted_count(self, db):
        r = _make_result()
        record_run(db, r)
        cid = self._get_first_candidate_id(db, r)
        update_candidate_status(db, cid, 'promoted', promoted_memory_id=7)
        run = get_run(db, r.execution_result.request_id)
        assert run.promoted_count == 1

    def test_rejected_does_not_increment_promoted_count(self, db):
        r = _make_result()
        record_run(db, r)
        cid = self._get_first_candidate_id(db, r)
        update_candidate_status(db, cid, 'rejected')
        run = get_run(db, r.execution_result.request_id)
        assert run.promoted_count == 0


# ---------------------------------------------------------------------------
# promote_candidate — the governed write boundary
# ---------------------------------------------------------------------------

class TestPromoteCandidate:
    def _setup(self, db):
        """Record a run, return (pipeline_result, candidate_id)."""
        r = _make_result()
        record_run(db, r)
        cid = derive_candidate_id(r.execution_result.request_id, 0)
        return r, cid

    def test_returns_memory_event_id(self, db):
        from memory import service as mem_service
        mem_service.init_db(db)
        _, cid = self._setup(db)
        mid = promote_candidate(db, cid, approved_by='test-operator')
        assert isinstance(mid, int)
        assert mid >= 1

    def test_memory_event_status_unresolved(self, db):
        from memory import service as mem_service
        mem_service.init_db(db)
        _, cid = self._setup(db)
        mid = promote_candidate(db, cid, approved_by='test-operator')
        event, _, _ = mem_service.get_memory_event(db, mid)
        assert event.status == 'unresolved'

    def test_evidence_contains_semantic_run_candidate(self, db):
        """
        Promoted memory event evidence must reference semantic run_id and
        candidate_id — this is the portable provenance reference preserved
        even when the ledger tables are not included in continuity bundles.
        """
        from memory import service as mem_service
        mem_service.init_db(db)
        r, cid = self._setup(db)
        mid = promote_candidate(db, cid, approved_by='test-operator')
        event, _, _ = mem_service.get_memory_event(db, mid)
        assert event.evidence is not None
        assert 'semantic:' in event.evidence
        assert r.execution_result.request_id in event.evidence
        assert cid in event.evidence

    def test_evidence_portable_without_ledger_tables(self, db):
        """
        Smoke assertion for the known limitation: evidence string is written
        into memory_events and is portable via continuity bundles, even though
        the ledger tables themselves are not included in bundles.

        Specifically: we can parse the evidence string to recover run_id and
        candidate_id, and those values are valid ledger references in the
        source system. This is the only cross-substrate provenance available
        until ledger bundle support is added.
        """
        from memory import service as mem_service
        mem_service.init_db(db)
        r, cid = self._setup(db)
        mid = promote_candidate(db, cid, approved_by='test-operator')
        event, _, _ = mem_service.get_memory_event(db, mid)

        # Parse the evidence string: "semantic:<method> | run:<run_id> | candidate:<cid>"
        parts = {
            seg.strip().split(':', 1)[0]: seg.strip().split(':', 1)[1]
            for seg in event.evidence.split('|')
            if ':' in seg
        }
        run_id_from_evidence = parts['run']
        cid_from_evidence = parts['candidate']

        # run_id references valid ledger row
        ledger_run = get_run(db, run_id_from_evidence)
        assert ledger_run is not None

        # candidate_id references valid ledger row
        ledger_cand = get_candidate(db, cid_from_evidence)
        assert ledger_cand is not None
        assert ledger_cand.status == 'promoted'
        assert ledger_cand.promoted_memory_id == mid

    def test_candidate_status_updated_to_promoted(self, db):
        from memory import service as mem_service
        mem_service.init_db(db)
        _, cid = self._setup(db)
        mid = promote_candidate(db, cid, approved_by='test-operator')
        cand = get_candidate(db, cid)
        assert cand.status == 'promoted'
        assert cand.promoted_memory_id == mid

    def test_double_promote_raises(self, db):
        from memory import service as mem_service
        mem_service.init_db(db)
        _, cid = self._setup(db)
        promote_candidate(db, cid, approved_by='test-operator')
        with pytest.raises(LedgerError, match="only 'candidate' can be promoted"):
            promote_candidate(db, cid, approved_by='test-operator')

    def test_empty_approved_by_raises(self, db):
        init_ledger(db)
        with pytest.raises(LedgerError, match='approved_by'):
            promote_candidate(db, 'any', approved_by='')

    def test_nonexistent_candidate_raises(self, db):
        init_ledger(db)
        with pytest.raises(LedgerNotFoundError):
            promote_candidate(db, 'doesnotexist', approved_by='op')

    def test_memory_event_created_by_matches_approved_by(self, db):
        from memory import service as mem_service
        mem_service.init_db(db)
        _, cid = self._setup(db)
        mid = promote_candidate(db, cid, approved_by='quant-team')
        event, _, _ = mem_service.get_memory_event(db, mid)
        assert event.created_by == 'quant-team'

    def test_no_write_without_explicit_call(self, db):
        """record_run() alone must not write to memory_events."""
        from memory import service as mem_service
        mem_service.init_db(db)
        r = _make_result()
        record_run(db, r)
        events = mem_service.list_memory_events(db)
        assert events == []

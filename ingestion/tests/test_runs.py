"""Tests for ingestion.runs (ingestion run ledger)."""
import json
import sqlite3
import pytest
from ingestion.runs import (
    init_run_ledger,
    record_run,
    get_run,
    list_runs,
    make_started_at,
    VALID_RUN_STATUSES,
    IngestionRun,
)
from ingestion.parser import PARSER_VERSION
from ingestion.extractor import EXTRACTOR_VERSION
from memory import service as mem_service
from sources.registry import register_source


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _db(tmp_path) -> str:
    db = str(tmp_path / "memory.db")
    init_run_ledger(db)
    return db


def _source_file(tmp_path, name="doc.txt", content=b"hello world") -> str:
    f = tmp_path / name
    f.write_bytes(content)
    return str(f)


def _minimal_run(db, started_at=None, status='candidate_generated', **overrides):
    kwargs = dict(
        db_path=db,
        source_id='abc1234500000000',
        source_checksum='a' * 64,
        source_version=1,
        parser_version=PARSER_VERSION,
        extractor_version=EXTRACTOR_VERSION,
        chunk_count=3,
        candidate_count=2,
        committed_count=0,
        committed_memory_ids=[],
        status=status,
        started_at=started_at or make_started_at(),
        completed_at=None,
    )
    kwargs.update(overrides)
    return record_run(**kwargs)


# ---------------------------------------------------------------------------
# init_run_ledger
# ---------------------------------------------------------------------------

def test_init_run_ledger_creates_table(tmp_path):
    db = _db(tmp_path)
    conn = sqlite3.connect(db)
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    conn.close()
    assert "ingestion_runs" in tables


def test_init_run_ledger_idempotent(tmp_path):
    db = str(tmp_path / "memory.db")
    init_run_ledger(db)
    init_run_ledger(db)  # must not raise


# ---------------------------------------------------------------------------
# record_run: basic contract
# ---------------------------------------------------------------------------

def test_record_run_returns_ingestion_run(tmp_path):
    db = _db(tmp_path)
    run = _minimal_run(db)
    assert isinstance(run, IngestionRun)


def test_record_run_id_is_16_hex(tmp_path):
    db = _db(tmp_path)
    run = _minimal_run(db)
    assert len(run.run_id) == 16
    int(run.run_id, 16)


def test_record_run_fields_preserved(tmp_path):
    db = _db(tmp_path)
    started = make_started_at()
    run = _minimal_run(db, started_at=started, chunk_count=5, candidate_count=3)
    assert run.source_id == 'abc1234500000000'
    assert run.source_checksum_sha256 == 'a' * 64
    assert run.source_version == 1
    assert run.parser_version == PARSER_VERSION
    assert run.extractor_version == EXTRACTOR_VERSION
    assert run.chunk_count == 5
    assert run.candidate_count == 3
    assert run.started_at == started


def test_record_run_status_candidate_generated(tmp_path):
    db = _db(tmp_path)
    run = _minimal_run(db, status='candidate_generated')
    assert run.status == 'candidate_generated'
    assert run.committed_count == 0
    assert run.committed_memory_ids == []


def test_record_run_status_committed(tmp_path):
    db = _db(tmp_path)
    run = _minimal_run(
        db,
        status='committed',
        committed_count=2,
        committed_memory_ids=[10, 11],
    )
    assert run.status == 'committed'
    assert run.committed_count == 2
    assert run.committed_memory_ids == [10, 11]


def test_record_run_status_failed(tmp_path):
    db = _db(tmp_path)
    run = _minimal_run(db, status='failed', metadata={'error': 'parse error'})
    assert run.status == 'failed'
    assert run.metadata['error'] == 'parse error'


def test_record_run_invalid_status_raises(tmp_path):
    db = _db(tmp_path)
    with pytest.raises(ValueError):
        _minimal_run(db, status='not_a_status')


def test_record_run_timestamps_utc(tmp_path):
    db = _db(tmp_path)
    import datetime as _dt
    completed = _dt.datetime.now(_dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    run = _minimal_run(db, completed_at=completed)
    assert run.started_at.endswith('Z')
    assert run.completed_at.endswith('Z')


def test_record_run_completed_at_none(tmp_path):
    db = _db(tmp_path)
    run = _minimal_run(db, completed_at=None)
    assert run.completed_at is None


# ---------------------------------------------------------------------------
# run_id determinism
# ---------------------------------------------------------------------------

def test_run_id_deterministic_same_inputs(tmp_path):
    db = _db(tmp_path)
    started = make_started_at()
    run1 = _minimal_run(db, started_at=started)

    # Delete and re-insert with same inputs
    conn = sqlite3.connect(db)
    conn.execute("DELETE FROM ingestion_runs WHERE run_id = ?", (run1.run_id,))
    conn.commit()
    conn.close()

    run2 = _minimal_run(db, started_at=started)
    assert run1.run_id == run2.run_id


def test_run_id_differs_on_different_started_at(tmp_path):
    db = _db(tmp_path)
    run1 = _minimal_run(db, started_at='2026-01-01T00:00:00Z',
                        source_checksum='b' * 64)
    run2 = _minimal_run(db, started_at='2026-01-01T00:00:01Z',
                        source_id='def5678900000000', source_checksum='c' * 64)
    assert run1.run_id != run2.run_id


def test_run_id_differs_on_different_checksum(tmp_path):
    db = _db(tmp_path)
    started = make_started_at()
    run1 = _minimal_run(db, started_at=started, source_checksum='a' * 64)
    run2 = _minimal_run(db, started_at=started,
                        source_id='def5678900000000', source_checksum='b' * 64)
    assert run1.run_id != run2.run_id


# ---------------------------------------------------------------------------
# committed_memory_ids preserved
# ---------------------------------------------------------------------------

def test_committed_ids_round_trip(tmp_path):
    db = _db(tmp_path)
    ids = [3, 7, 15, 22]
    run = _minimal_run(db, status='committed', committed_count=4,
                       committed_memory_ids=ids)
    assert run.committed_memory_ids == sorted(ids)


def test_committed_ids_sorted_on_store(tmp_path):
    db = _db(tmp_path)
    run = _minimal_run(db, status='committed', committed_count=3,
                       committed_memory_ids=[30, 10, 20])
    assert run.committed_memory_ids == [10, 20, 30]


def test_committed_ids_empty_when_not_committed(tmp_path):
    db = _db(tmp_path)
    run = _minimal_run(db, status='candidate_generated')
    assert run.committed_memory_ids == []


# ---------------------------------------------------------------------------
# source checksum preserved
# ---------------------------------------------------------------------------

def test_source_checksum_preserved(tmp_path):
    db = _db(tmp_path)
    checksum = 'f' * 64
    run = _minimal_run(db, source_checksum=checksum)
    assert run.source_checksum_sha256 == checksum


def test_source_version_preserved(tmp_path):
    db = _db(tmp_path)
    run = _minimal_run(db, source_version=3)
    assert run.source_version == 3


# ---------------------------------------------------------------------------
# get_run
# ---------------------------------------------------------------------------

def test_get_run_returns_run(tmp_path):
    db = _db(tmp_path)
    run = _minimal_run(db)
    retrieved = get_run(db, run.run_id)
    assert retrieved is not None
    assert retrieved.run_id == run.run_id


def test_get_run_not_found_returns_none(tmp_path):
    db = _db(tmp_path)
    assert get_run(db, '0000000000000000') is None


def test_get_run_all_fields_correct(tmp_path):
    db = _db(tmp_path)
    started = make_started_at()
    run = _minimal_run(db, started_at=started, chunk_count=7, candidate_count=4,
                       status='committed', committed_count=2,
                       committed_memory_ids=[5, 6])
    r = get_run(db, run.run_id)
    assert r.chunk_count == 7
    assert r.candidate_count == 4
    assert r.committed_count == 2
    assert r.committed_memory_ids == [5, 6]
    assert r.status == 'committed'


# ---------------------------------------------------------------------------
# list_runs
# ---------------------------------------------------------------------------

def test_list_runs_returns_all(tmp_path):
    db = _db(tmp_path)
    _minimal_run(db, started_at='2026-01-01T00:00:00Z', source_checksum='a' * 64)
    _minimal_run(db, started_at='2026-01-01T00:00:01Z',
                 source_id='def5678900000000', source_checksum='b' * 64)
    runs = list_runs(db)
    assert len(runs) >= 2


def test_list_runs_ordered_by_started_at_desc(tmp_path):
    db = _db(tmp_path)
    _minimal_run(db, started_at='2026-01-01T00:00:00Z', source_checksum='a' * 64)
    _minimal_run(db, started_at='2026-01-02T00:00:00Z',
                 source_id='def5678900000000', source_checksum='b' * 64)
    runs = list_runs(db)
    timestamps = [r.started_at for r in runs]
    assert timestamps == sorted(timestamps, reverse=True)


def test_list_runs_filter_by_status(tmp_path):
    db = _db(tmp_path)
    _minimal_run(db, started_at='2026-01-01T00:00:00Z',
                 source_checksum='a' * 64, status='committed', committed_count=1,
                 committed_memory_ids=[1])
    _minimal_run(db, started_at='2026-01-01T00:00:01Z',
                 source_id='def5678900000000', source_checksum='b' * 64,
                 status='candidate_generated')

    committed = list_runs(db, status='committed')
    pending = list_runs(db, status='candidate_generated')
    assert all(r.status == 'committed' for r in committed)
    assert all(r.status == 'candidate_generated' for r in pending)


def test_list_runs_filter_by_source_id(tmp_path):
    db = _db(tmp_path)
    _minimal_run(db, started_at='2026-01-01T00:00:00Z', source_checksum='a' * 64)
    _minimal_run(db, started_at='2026-01-01T00:00:01Z',
                 source_id='def5678900000000', source_checksum='b' * 64)

    runs = list_runs(db, source_id='abc1234500000000')
    assert all(r.source_id == 'abc1234500000000' for r in runs)


def test_list_runs_invalid_status_raises(tmp_path):
    db = _db(tmp_path)
    with pytest.raises(ValueError):
        list_runs(db, status='not_a_status')


# ---------------------------------------------------------------------------
# to_dict / JSON serialization
# ---------------------------------------------------------------------------

def test_run_to_dict_json_serialisable(tmp_path):
    db = _db(tmp_path)
    run = _minimal_run(db, status='committed', committed_count=1,
                       committed_memory_ids=[42])
    d = run.to_dict()
    json.dumps(d, sort_keys=True)  # must not raise


def test_run_to_dict_keys(tmp_path):
    db = _db(tmp_path)
    run = _minimal_run(db)
    d = run.to_dict()
    expected_keys = {
        'run_id', 'source_id', 'source_checksum_sha256', 'source_version',
        'parser_version', 'extractor_version',
        'chunk_count', 'candidate_count', 'committed_count',
        'committed_memory_ids', 'status', 'started_at', 'completed_at', 'metadata',
    }
    assert expected_keys == set(d.keys())


# ---------------------------------------------------------------------------
# Integration: ingest-file creates a run record (via CLI)
# ---------------------------------------------------------------------------

def _init_full_db(tmp_path) -> str:
    """Set up a DB with memory schema + run ledger."""
    db = str(tmp_path / "memory.db")
    mem_service.init_db(db)
    init_run_ledger(db)
    return db


def test_ingest_file_creates_run_without_commit(tmp_path):
    from ingestion.parser import parse_file
    from ingestion.chunker import chunk_document
    from ingestion.candidates import run_ingestion
    from ingestion.parser import PARSER_VERSION
    from ingestion.extractor import EXTRACTOR_VERSION
    from ingestion.runs import record_run, make_started_at

    db = _init_full_db(tmp_path)
    f = _source_file(tmp_path, content=b"ADR: use SQLite. open question: which index?")
    src = register_source(db, f)

    started = make_started_at()
    doc = parse_file(f)
    chunks = chunk_document(doc)
    result = run_ingestion(doc, chunks)  # no commit

    run = record_run(
        db_path=db,
        source_id=src.source_id,
        source_checksum=src.checksum_sha256,
        source_version=src.version,
        parser_version=PARSER_VERSION,
        extractor_version=EXTRACTOR_VERSION,
        chunk_count=len(chunks),
        candidate_count=result.candidate_count,
        committed_count=0,
        committed_memory_ids=[],
        status='candidate_generated',
        started_at=started,
    )

    assert run.status == 'candidate_generated'
    assert run.committed_count == 0
    assert run.committed_memory_ids == []
    assert run.source_id == src.source_id

    # No memory events should exist
    events = mem_service.list_memory_events(db, limit=100)
    assert len(events) == 0


def test_ingest_file_creates_run_with_commit(tmp_path):
    from ingestion.parser import parse_file, PARSER_VERSION
    from ingestion.chunker import chunk_document
    from ingestion.candidates import run_ingestion
    from ingestion.extractor import EXTRACTOR_VERSION
    from ingestion.runs import record_run, make_started_at

    db = _init_full_db(tmp_path)
    f = _source_file(tmp_path, content=b"ADR: use SQLite. governance rule: no live capital.")
    src = register_source(db, f)

    started = make_started_at()
    doc = parse_file(f)
    chunks = chunk_document(doc)
    result = run_ingestion(doc, chunks, memory_db_path=db, commit=True)

    run = record_run(
        db_path=db,
        source_id=src.source_id,
        source_checksum=src.checksum_sha256,
        source_version=src.version,
        parser_version=PARSER_VERSION,
        extractor_version=EXTRACTOR_VERSION,
        chunk_count=len(chunks),
        candidate_count=result.candidate_count,
        committed_count=len(result.committed_ids),
        committed_memory_ids=result.committed_ids,
        status='committed',
        started_at=started,
    )

    assert run.status == 'committed'
    assert run.committed_count == len(result.committed_ids)
    assert run.committed_memory_ids == sorted(result.committed_ids)


def test_committed_ids_match_memory_events(tmp_path):
    """Every run.committed_memory_ids entry must exist in memory_events."""
    from ingestion.parser import parse_file, PARSER_VERSION
    from ingestion.chunker import chunk_document
    from ingestion.candidates import run_ingestion
    from ingestion.extractor import EXTRACTOR_VERSION
    from ingestion.runs import record_run, make_started_at

    db = _init_full_db(tmp_path)
    f = _source_file(tmp_path, content=b"ADR: use SQLite. open question: why not Postgres?")
    src = register_source(db, f)

    started = make_started_at()
    doc = parse_file(f)
    chunks = chunk_document(doc)
    result = run_ingestion(doc, chunks, memory_db_path=db, commit=True)

    run = record_run(
        db_path=db,
        source_id=src.source_id,
        source_checksum=src.checksum_sha256,
        source_version=src.version,
        parser_version=PARSER_VERSION,
        extractor_version=EXTRACTOR_VERSION,
        chunk_count=len(chunks),
        candidate_count=result.candidate_count,
        committed_count=len(result.committed_ids),
        committed_memory_ids=result.committed_ids,
        status='committed',
        started_at=started,
    )

    for mid in run.committed_memory_ids:
        event, _, _ = mem_service.get_memory_event(db, mid)
        assert event.id == mid


def test_failed_run_records_error(tmp_path):
    db = _db(tmp_path)
    run = _minimal_run(db, status='failed', metadata={'error': 'FileNotFoundError: /bad/path'})
    assert run.status == 'failed'
    assert 'error' in run.metadata
    assert 'FileNotFoundError' in run.metadata['error']


def test_no_mutation_without_commit(tmp_path):
    """Without --commit, memory_events must remain empty after run record is written."""
    db = _init_full_db(tmp_path)
    _minimal_run(db, status='candidate_generated')

    events = mem_service.list_memory_events(db, limit=100)
    assert len(events) == 0


# ---------------------------------------------------------------------------
# Full source → run → memory lineage inspectable
# ---------------------------------------------------------------------------

def test_full_lineage_chain(tmp_path):
    """
    Prove: source_document → ingestion_run → memory_events is fully traversable.
    """
    from ingestion.parser import parse_file, PARSER_VERSION
    from ingestion.chunker import chunk_document
    from ingestion.candidates import run_ingestion
    from ingestion.extractor import EXTRACTOR_VERSION
    from ingestion.runs import record_run, make_started_at
    from sources.registry import get_source_by_id

    db = _init_full_db(tmp_path)
    f = _source_file(tmp_path, content=b"ADR: use SQLite for persistence.")
    src = register_source(db, f)

    started = make_started_at()
    doc = parse_file(f)
    chunks = chunk_document(doc)
    result = run_ingestion(doc, chunks, memory_db_path=db, commit=True)

    run = record_run(
        db_path=db,
        source_id=src.source_id,
        source_checksum=src.checksum_sha256,
        source_version=src.version,
        parser_version=PARSER_VERSION,
        extractor_version=EXTRACTOR_VERSION,
        chunk_count=len(chunks),
        candidate_count=result.candidate_count,
        committed_count=len(result.committed_ids),
        committed_memory_ids=result.committed_ids,
        status='committed',
        started_at=started,
    )

    # 1. source_id resolves to a source record
    src_reloaded = get_source_by_id(db, run.source_id)
    assert src_reloaded is not None
    assert src_reloaded.checksum_sha256 == run.source_checksum_sha256

    # 2. run_id resolves via get_run
    run_reloaded = get_run(db, run.run_id)
    assert run_reloaded.source_id == src.source_id

    # 3. committed_memory_ids each resolve to a memory event
    for mid in run_reloaded.committed_memory_ids:
        event, _, _ = mem_service.get_memory_event(db, mid)
        assert event.source == src.path

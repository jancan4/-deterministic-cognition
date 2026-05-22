"""Tests for ingestion.candidates."""
import json
import sqlite3
import pytest
from ingestion.candidates import (
    extract_candidates,
    commit_candidates,
    run_ingestion,
    MIN_CANDIDATE_CONFIDENCE,
)
from ingestion.chunker import chunk_document
from ingestion.parser import parse_text
from ingestion.models import CandidateMemoryEvent, IngestionResult


def _doc(text: str):
    return parse_text(text, source_path="<test>")


def _ingest(text: str):
    doc = _doc(text)
    chunks = chunk_document(doc)
    return doc, chunks, extract_candidates(doc, chunks)


# ---------------------------------------------------------------------------
# extract_candidates: basic contract
# ---------------------------------------------------------------------------

def test_extract_candidates_returns_list():
    doc, chunks, cands = _ingest("ADR: use SQLite for persistence.")
    assert isinstance(cands, list)


def test_extract_candidates_returns_candidate_events():
    doc, chunks, cands = _ingest("ADR: use SQLite for persistence.")
    for c in cands:
        assert isinstance(c, CandidateMemoryEvent)


def test_extract_candidates_confidence_floor():
    doc, chunks, cands = _ingest("hypothesis: markets trend after news.")
    for c in cands:
        assert c.confidence >= MIN_CANDIDATE_CONFIDENCE


def test_extract_candidates_no_candidates_plain_text():
    # Text with no triggering signals; may produce 0 or more candidates
    doc, chunks, cands = _ingest("The weather is nice today in London.")
    # Just ensure no crash and list returned
    assert isinstance(cands, list)


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

def test_extract_candidates_deduplicates_same_event_type_same_chunk():
    # Multiple rules may fire for the same event_type on one chunk.
    # After dedup, only one candidate per (source, chunk_index, event_type).
    text = "open question: tbd: what threshold to use?"
    doc, chunks, cands = _ingest(text)
    oq = [c for c in cands if c.event_type == "open_question"]
    # Per chunk, at most one open_question
    if len(chunks) == 1 and oq:
        assert len(oq) == 1


def test_extract_candidates_keeps_highest_confidence_on_dedup():
    # Feed text that triggers both a low-confidence keyword rule and a
    # high-confidence pattern rule for the same event_type on the same chunk.
    text = "ADR: We decided to use SQLite. Architecture decision record confirmed."
    doc, chunks, cands = _ingest(text)
    arch = [c for c in cands if c.event_type == "architecture_decision"]
    if len(chunks) == 1 and len(arch) == 1:
        # Should have the higher confidence candidate
        assert arch[0].confidence >= 3


# ---------------------------------------------------------------------------
# Source attribution
# ---------------------------------------------------------------------------

def test_extract_candidates_source_attribution_preserved():
    doc = parse_text("We decided to use SQLite.", source_path="my/arch.txt")
    chunks = chunk_document(doc)
    cands = extract_candidates(doc, chunks)
    for c in cands:
        assert c.source == "my/arch.txt"
        assert c.source_span is not None
        assert isinstance(c.source_span.start, int)
        assert isinstance(c.source_span.end, int)
        assert isinstance(c.source_span.text, str)


def test_extract_candidates_source_span_within_document():
    text = "ADR: use WAL mode for SQLite to avoid writer starvation."
    doc = _doc(text)
    chunks = chunk_document(doc)
    cands = extract_candidates(doc, chunks)
    for c in cands:
        assert c.source_span.start >= 0
        assert c.source_span.end <= len(doc.raw_text)


# ---------------------------------------------------------------------------
# Ordering and determinism
# ---------------------------------------------------------------------------

def test_extract_candidates_sorted_by_start_char():
    text = (
        "ADR: use SQLite.\n\n"
        "open question: why not Postgres?\n\n"
        "governance rule: no live capital without validation."
    )
    doc, chunks, cands = _ingest(text)
    starts = [c.source_span.start for c in cands]
    assert starts == sorted(starts)


def test_extract_candidates_deterministic():
    text = "ADR: SQLite. Hypothesis: markets trend. open question: which signal?"
    doc = _doc(text)
    chunks = chunk_document(doc)
    cands1 = extract_candidates(doc, chunks)
    cands2 = extract_candidates(doc, chunks)
    assert [c.event_type for c in cands1] == [c.event_type for c in cands2]
    assert [c.source_span.start for c in cands1] == [c.source_span.start for c in cands2]


# ---------------------------------------------------------------------------
# JSON serialisation
# ---------------------------------------------------------------------------

def test_extract_candidates_to_dict_json_serialisable():
    text = "ADR: we use SQLite. open question: what index strategy?"
    doc, chunks, cands = _ingest(text)
    for c in cands:
        d = c.to_dict()
        json.dumps(d, sort_keys=True)  # should not raise


def test_extract_candidates_from_dict_roundtrip():
    text = "governance rule: no live capital without quant validation."
    doc, chunks, cands = _ingest(text)
    for c in cands:
        d = c.to_dict()
        restored = CandidateMemoryEvent.from_dict(d)
        assert restored.event_type == c.event_type
        assert restored.title == c.title
        assert restored.confidence == c.confidence
        assert restored.source_span.start == c.source_span.start


# ---------------------------------------------------------------------------
# No mutation without --commit
# ---------------------------------------------------------------------------

def test_run_ingestion_no_commit_does_not_write_db(tmp_path):
    db = tmp_path / "memory.db"
    conn = sqlite3.connect(str(db))
    conn.execute("""
        CREATE TABLE memory_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT, title TEXT, summary TEXT, evidence TEXT,
            source TEXT, confidence INTEGER, status TEXT,
            tags TEXT, created_by TEXT
        )
    """)
    conn.commit()
    conn.close()

    text = "ADR: use SQLite. governance rule: no live capital."
    doc = _doc(text)
    chunks = chunk_document(doc)
    result = run_ingestion(doc, chunks, memory_db_path=str(db), commit=False)

    assert isinstance(result, IngestionResult)
    assert result.committed_ids == []

    # Database must not have been written
    conn2 = sqlite3.connect(str(db))
    count = conn2.execute("SELECT COUNT(*) FROM memory_events").fetchone()[0]
    conn2.close()
    assert count == 0


# ---------------------------------------------------------------------------
# commit_candidates writes to database
# ---------------------------------------------------------------------------

def _setup_memory_db(path: str) -> None:
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE memory_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT, title TEXT, summary TEXT, evidence TEXT,
            source TEXT, confidence INTEGER, status TEXT,
            tags TEXT, created_by TEXT
        )
    """)
    conn.commit()
    conn.close()


def test_commit_candidates_inserts_rows(tmp_path):
    db = tmp_path / "memory.db"
    _setup_memory_db(str(db))

    text = "ADR: use SQLite. governance rule: no live capital."
    doc, chunks, cands = _ingest(text)
    assert len(cands) > 0

    ids = commit_candidates(cands, str(db))
    assert len(ids) == len(cands)
    assert all(isinstance(i, int) for i in ids)

    conn = sqlite3.connect(str(db))
    count = conn.execute("SELECT COUNT(*) FROM memory_events").fetchone()[0]
    conn.close()
    assert count == len(cands)


def test_commit_candidates_sets_committed_id(tmp_path):
    db = tmp_path / "memory.db"
    _setup_memory_db(str(db))

    text = "ADR: use SQLite."
    doc, chunks, cands = _ingest(text)
    commit_candidates(cands, str(db))
    for c in cands:
        assert c.committed_id is not None
        assert isinstance(c.committed_id, int)


def test_run_ingestion_with_commit(tmp_path):
    db = tmp_path / "memory.db"
    _setup_memory_db(str(db))

    text = "hypothesis: markets mean-revert over 5-day windows."
    doc = _doc(text)
    chunks = chunk_document(doc)
    result = run_ingestion(doc, chunks, memory_db_path=str(db), commit=True)

    assert isinstance(result, IngestionResult)
    assert len(result.committed_ids) == len(result.candidates)

    conn = sqlite3.connect(str(db))
    count = conn.execute("SELECT COUNT(*) FROM memory_events").fetchone()[0]
    conn.close()
    assert count == len(result.candidates)


# ---------------------------------------------------------------------------
# IngestionResult
# ---------------------------------------------------------------------------

def test_run_ingestion_result_structure():
    text = "ADR: use SQLite. open question: what index?"
    doc = _doc(text)
    chunks = chunk_document(doc)
    result = run_ingestion(doc, chunks)
    assert result.document is doc
    assert result.chunks == chunks
    assert isinstance(result.candidates, list)
    assert isinstance(result.committed_ids, list)


def test_run_ingestion_result_to_dict():
    text = "ADR: use SQLite."
    doc = _doc(text)
    chunks = chunk_document(doc)
    result = run_ingestion(doc, chunks)
    d = result.to_dict()
    assert "document" in d
    assert "chunk_count" in d
    assert "candidates" in d
    assert "committed_ids" in d


def test_candidate_count_property():
    text = "ADR: use SQLite. open question: which index strategy to use?"
    doc, chunks, cands = _ingest(text)
    result = IngestionResult(document=doc, chunks=chunks, candidates=cands)
    assert result.candidate_count == len(cands)

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
from memory import service as mem_service


def _doc(text: str):
    return parse_text(text, source_path="<test>")


def _ingest(text: str):
    doc = _doc(text)
    chunks = chunk_document(doc)
    return doc, chunks, extract_candidates(doc, chunks)


def _init_db(path) -> str:
    """Initialise a real memory DB using the governed service schema."""
    db = str(path / "memory.db")
    mem_service.init_db(db)
    return db


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
    assert isinstance(cands, list)


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

def test_extract_candidates_deduplicates_same_event_type_same_chunk():
    text = "open question: tbd: what threshold to use?"
    doc, chunks, cands = _ingest(text)
    oq = [c for c in cands if c.event_type == "open_question"]
    if len(chunks) == 1 and oq:
        assert len(oq) == 1


def test_extract_candidates_keeps_highest_confidence_on_dedup():
    text = "ADR: We decided to use SQLite. Architecture decision record confirmed."
    doc, chunks, cands = _ingest(text)
    arch = [c for c in cands if c.event_type == "architecture_decision"]
    if len(chunks) == 1 and len(arch) == 1:
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
    db = _init_db(tmp_path)

    text = "ADR: use SQLite. governance rule: no live capital."
    doc = _doc(text)
    chunks = chunk_document(doc)
    result = run_ingestion(doc, chunks, memory_db_path=db, commit=False)

    assert isinstance(result, IngestionResult)
    assert result.committed_ids == []

    # Real schema: verify via service, not raw SQL
    events = mem_service.list_memory_events(db, limit=100)
    assert len(events) == 0


# ---------------------------------------------------------------------------
# commit_candidates routes through memory.service
# ---------------------------------------------------------------------------

def test_commit_candidates_inserts_rows(tmp_path):
    db = _init_db(tmp_path)

    text = "ADR: use SQLite. governance rule: no live capital."
    doc, chunks, cands = _ingest(text)
    assert len(cands) > 0

    ids = commit_candidates(cands, db)
    assert len(ids) == len(cands)
    assert all(isinstance(i, int) for i in ids)

    events = mem_service.list_memory_events(db, limit=100)
    assert len(events) == len(cands)


def test_commit_candidates_sets_committed_id(tmp_path):
    db = _init_db(tmp_path)

    text = "ADR: use SQLite."
    doc, chunks, cands = _ingest(text)
    commit_candidates(cands, db)
    for c in cands:
        assert c.committed_id is not None
        assert isinstance(c.committed_id, int)


def test_commit_candidates_ids_match_service_ids(tmp_path):
    """IDs returned by commit_candidates must match IDs in the memory service."""
    db = _init_db(tmp_path)

    text = "ADR: use SQLite. open question: what index strategy?"
    doc, chunks, cands = _ingest(text)
    ids = commit_candidates(cands, db)

    service_ids = {e.id for e in mem_service.list_memory_events(db, limit=100)}
    for committed_id in ids:
        assert committed_id in service_ids


def test_commit_candidates_service_stores_correct_fields(tmp_path):
    """Fields written by the service must match candidate values exactly."""
    db = _init_db(tmp_path)

    text = "ADR: use SQLite for the primary store."
    doc, chunks, cands = _ingest(text)
    assert len(cands) > 0

    commit_candidates(cands, db)
    events = mem_service.list_memory_events(db, limit=100)
    assert len(events) == len(cands)

    for cand, event in zip(cands, reversed(events)):  # list_memory_events returns DESC
        assert event.event_type == cand.event_type
        assert event.title == cand.title
        assert event.confidence == cand.confidence
        assert event.status == cand.status
        assert event.source == cand.source
        assert event.created_by == cand.created_by


def test_commit_candidates_service_stores_tags(tmp_path):
    """Tags written through the service must be readable back via the service."""
    db = _init_db(tmp_path)

    text = "ADR: use SQLite."
    doc, chunks, cands = _ingest(text)
    arch = [c for c in cands if c.event_type == "architecture_decision"]
    if not arch:
        pytest.skip("no architecture_decision candidate extracted")

    commit_candidates(arch, db)
    events = mem_service.list_memory_events(db, event_type="architecture_decision", limit=10)
    assert len(events) == len(arch)
    for event in events:
        assert isinstance(event.tags, list)


def test_commit_candidates_service_assigns_version(tmp_path):
    """The service must assign version=1 on initial insert."""
    db = _init_db(tmp_path)

    text = "ADR: use SQLite."
    doc, chunks, cands = _ingest(text)
    ids = commit_candidates(cands, db)

    for mid in ids:
        event, _, _ = mem_service.get_memory_event(db, mid)
        assert event.version == 1


def test_commit_candidates_service_assigns_timestamps(tmp_path):
    """The service must populate created_at and updated_at."""
    db = _init_db(tmp_path)

    text = "governance rule: no live capital without validation."
    doc, chunks, cands = _ingest(text)
    ids = commit_candidates(cands, db)

    for mid in ids:
        event, _, _ = mem_service.get_memory_event(db, mid)
        assert event.created_at
        assert event.updated_at
        # UTC ISO-8601 format
        assert event.created_at.endswith("Z")
        assert event.updated_at.endswith("Z")


def test_commit_candidates_preserves_ordering(tmp_path):
    """Candidates are committed in extraction order; IDs ascend."""
    db = _init_db(tmp_path)

    text = (
        "ADR: use SQLite.\n\n"
        "open question: what index strategy?\n\n"
        "governance rule: no live capital without validation."
    )
    doc, chunks, cands = _ingest(text)
    ids = commit_candidates(cands, db)

    # IDs from an AUTOINCREMENT column must be strictly ascending
    assert ids == sorted(ids)


# ---------------------------------------------------------------------------
# Service validation is enforced for ingestion commits
# ---------------------------------------------------------------------------

def test_commit_rejects_invalid_event_type_via_service(tmp_path):
    """
    Verify the service boundary rejects invalid event_type.
    CandidateMemoryEvent.__post_init__ already validates, so we test
    that the service's own validation also fires when called directly.
    """
    db = _init_db(tmp_path)
    with pytest.raises(Exception):  # mem_service.ValidationError or ingestion.ValidationError
        mem_service.add_memory_event(
            db_path=db,
            event_type="not_a_real_type",
            title="T",
            summary="S",
            source="s",
            confidence=3,
            status="proposed",
            created_by="test",
        )


def test_commit_rejects_invalid_confidence_via_service(tmp_path):
    """Service must reject out-of-range confidence."""
    db = _init_db(tmp_path)
    with pytest.raises(Exception):
        mem_service.add_memory_event(
            db_path=db,
            event_type="hypothesis",
            title="T",
            summary="S",
            source="s",
            confidence=99,
            status="proposed",
            created_by="test",
        )


def test_commit_rejects_empty_title_via_service(tmp_path):
    """Service must reject empty title."""
    db = _init_db(tmp_path)
    with pytest.raises(Exception):
        mem_service.add_memory_event(
            db_path=db,
            event_type="hypothesis",
            title="",
            summary="S",
            source="s",
            confidence=3,
            status="proposed",
            created_by="test",
        )


# ---------------------------------------------------------------------------
# run_ingestion with commit
# ---------------------------------------------------------------------------

def test_run_ingestion_with_commit(tmp_path):
    db = _init_db(tmp_path)

    text = "hypothesis: markets mean-revert over 5-day windows."
    doc = _doc(text)
    chunks = chunk_document(doc)
    result = run_ingestion(doc, chunks, memory_db_path=db, commit=True)

    assert isinstance(result, IngestionResult)
    assert len(result.committed_ids) == len(result.candidates)

    events = mem_service.list_memory_events(db, limit=100)
    assert len(events) == len(result.candidates)


def test_run_ingestion_committed_ids_are_valid_memory_ids(tmp_path):
    """Every ID in committed_ids must be retrievable via the service."""
    db = _init_db(tmp_path)

    text = "ADR: use SQLite. governance rule: no live capital."
    doc = _doc(text)
    chunks = chunk_document(doc)
    result = run_ingestion(doc, chunks, memory_db_path=db, commit=True)

    for mid in result.committed_ids:
        event, _, _ = mem_service.get_memory_event(db, mid)
        assert event.id == mid


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

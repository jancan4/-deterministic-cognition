"""
High-level candidate pipeline: orchestrates parsing, chunking, and extraction.

Entry point: extract_candidates(doc, chunks) → List[CandidateMemoryEvent]

Deduplication:
  Candidates from multiple rules on the same chunk may share an event_type.
  We deduplicate by (chunk source_id, chunk_index, event_type): keep the
  highest-confidence candidate; on ties keep the first (registration order).

Confidence floor:
  Candidates with confidence < MIN_CANDIDATE_CONFIDENCE are discarded.

Commit:
  commit_candidates(candidates, memory_db_path) writes accepted candidates
  to the memory database via memory.service.add_memory_event — the single
  governed write boundary for all memory_events writes.

  memory.service owns: column mapping, schema validation, timestamp
  assignment, version tracking, and CHECK constraint enforcement.
  ingestion never touches the memory schema directly.

  All functions except commit_candidates are read-only and produce no
  side effects. The caller (CLI ingest-file) decides whether to commit.
"""
from typing import List, Optional, Tuple

from .models import (
    CandidateMemoryEvent,
    Chunk,
    IngestionResult,
    ParsedDocument,
)
from .extractor import extract_from_chunks

# Candidates below this confidence are discarded before presenting to operator
MIN_CANDIDATE_CONFIDENCE = 2


def _dedup_key(cand: CandidateMemoryEvent, chunk_index: int) -> Tuple:
    return (cand.source, chunk_index, cand.event_type)


def _deduplicate(
    candidates: List[CandidateMemoryEvent],
    chunks: List[Chunk],
) -> List[CandidateMemoryEvent]:
    """
    Deduplicate candidates that share (source, chunk_index, event_type).

    Builds a map from chunk start_char → chunk_index so we can recover the
    chunk index from the candidate's source_span without coupling the two
    data structures.
    """
    # Map start_char → chunk_index
    start_to_index = {c.start_char: c.chunk_index for c in chunks}

    seen: dict = {}
    for cand in candidates:
        ci = start_to_index.get(cand.source_span.start)
        if ci is None:
            # Span is mid-chunk (pattern match inside chunk); use span start
            # to find the owning chunk
            for chunk in chunks:
                if chunk.start_char <= cand.source_span.start < chunk.end_char:
                    ci = chunk.chunk_index
                    break
            if ci is None:
                ci = -1

        key = _dedup_key(cand, ci)
        if key not in seen:
            seen[key] = cand
        else:
            # Keep higher confidence
            if cand.confidence > seen[key].confidence:
                seen[key] = cand

    # Return in stable order (preserve insertion order from seen dict)
    return list(seen.values())


def extract_candidates(
    doc: ParsedDocument,
    chunks: List[Chunk],
) -> List[CandidateMemoryEvent]:
    """
    Run the full extraction pipeline on a parsed document and its chunks.

    Steps:
      1. Apply all extraction rules to every chunk.
      2. Discard candidates below MIN_CANDIDATE_CONFIDENCE.
      3. Deduplicate by (source, chunk_index, event_type).
      4. Return candidates sorted by (chunk start_char, event_type).

    No database writes. Deterministic for the same document and chunks.
    """
    raw = extract_from_chunks(chunks)
    filtered = [c for c in raw if c.confidence >= MIN_CANDIDATE_CONFIDENCE]
    deduped = _deduplicate(filtered, chunks)
    deduped.sort(key=lambda c: (c.source_span.start, c.event_type))
    return deduped


def commit_candidates(
    candidates: List[CandidateMemoryEvent],
    memory_db_path: str,
) -> List[int]:
    """
    Write accepted candidates to the memory database.

    All writes go through memory.service.add_memory_event — the single
    governed write boundary. The service enforces schema validation,
    column mapping, timestamp assignment, and version tracking.
    ingestion never constructs SQL against memory_events directly.

    Returns the list of inserted memory_event IDs in insertion order.
    Sets committed_id on each candidate in-place.

    Only called when the operator passes --commit.
    """
    from memory import service as mem_service

    inserted_ids: List[int] = []

    for cand in candidates:
        event = mem_service.add_memory_event(
            db_path=memory_db_path,
            event_type=cand.event_type,
            title=cand.title,
            summary=cand.summary,
            source=cand.source,
            confidence=cand.confidence,
            status=cand.status,
            created_by=cand.created_by,
            evidence=cand.evidence,
            tags=list(cand.tags),
        )
        inserted_ids.append(event.id)
        cand.committed_id = event.id

    return inserted_ids


def run_ingestion(
    doc: ParsedDocument,
    chunks: List[Chunk],
    memory_db_path: Optional[str] = None,
    commit: bool = False,
) -> IngestionResult:
    """
    Full ingestion run: extract candidates, optionally commit to database.

    Returns an IngestionResult with document, chunks, candidates, and
    committed_ids (empty unless commit=True and memory_db_path is set).
    """
    candidates = extract_candidates(doc, chunks)
    committed_ids: List[int] = []

    if commit and memory_db_path:
        committed_ids = commit_candidates(candidates, memory_db_path)

    return IngestionResult(
        document=doc,
        chunks=chunks,
        candidates=candidates,
        committed_ids=committed_ids,
    )

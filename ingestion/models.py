"""
Data models for the signal extraction and memory ingestion pipeline.

A CandidateMemoryEvent is a proposed memory event derived from source text.
It is not a memory_event until an operator explicitly commits it.

The pipeline is:
  source text → ParsedDocument → List[Chunk] → List[CandidateMemoryEvent]
                                                     ↓ operator review
                                              memory_events (committed)
"""
import json
from dataclasses import dataclass, field
from typing import List, Optional

from memory.models import VALID_EVENT_TYPES, CONFIDENCE_MIN, CONFIDENCE_MAX

# The subset of valid event types that extraction rules may produce.
# Mirrors memory.models.VALID_EVENT_TYPES — kept explicit for extraction safety.
EXTRACTABLE_EVENT_TYPES = (
    'architecture_decision',
    'governance_rule',
    'hypothesis',
    'experiment',
    'validation_result',
    'adaptation',
    'regime_observation',
    'implementation_note',
    'open_question',
    'rejected_idea',
    'incident',
    'source_reference',
)

# Extraction method identifiers — used in CandidateMemoryEvent.extraction_method
EXTRACTION_METHOD_KEYWORD = 'keyword'
EXTRACTION_METHOD_PATTERN = 'pattern'
EXTRACTION_METHOD_HEURISTIC = 'heuristic'
EXTRACTION_METHOD_MANUAL = 'manual'


class ValidationError(ValueError):
    pass


def _validate_confidence(confidence: int) -> None:
    if isinstance(confidence, bool) or not isinstance(confidence, int):
        raise ValidationError(
            f"confidence must be an integer, got {type(confidence).__name__}"
        )
    if not CONFIDENCE_MIN <= confidence <= CONFIDENCE_MAX:
        raise ValidationError(
            f"confidence must be {CONFIDENCE_MIN}–{CONFIDENCE_MAX}, got {confidence}"
        )


def _validate_event_type(event_type: str) -> None:
    if event_type not in EXTRACTABLE_EVENT_TYPES:
        raise ValidationError(
            f"Invalid event_type {event_type!r}. Must be one of: {EXTRACTABLE_EVENT_TYPES}"
        )


@dataclass
class SourceSpan:
    """
    The byte/character range within the source text from which a candidate was extracted.
    Preserved for source attribution and operator review.
    """
    start: int      # inclusive start character offset in the source text
    end: int        # exclusive end character offset
    text: str       # the exact extracted text slice

    def to_dict(self) -> dict:
        return {'start': self.start, 'end': self.end, 'text': self.text}

    @classmethod
    def from_dict(cls, d: dict) -> 'SourceSpan':
        return cls(start=d['start'], end=d['end'], text=d['text'])


@dataclass
class ParsedDocument:
    """
    A source document after initial parsing.

    source_path: the originating file path or identifier.
    source_id: a stable identifier derived from path + content hash.
    raw_text: the full text as read (encoding-normalised).
    metadata: key-value pairs parsed from the document header/frontmatter.
    """
    source_path: str
    source_id: str        # deterministic: sha256(raw_text)[:16]
    raw_text: str
    metadata: dict = field(default_factory=dict)
    line_count: int = 0
    char_count: int = 0

    def to_dict(self) -> dict:
        return {
            'source_path': self.source_path,
            'source_id': self.source_id,
            'char_count': self.char_count,
            'line_count': self.line_count,
            'metadata': self.metadata,
        }


@dataclass
class Chunk:
    """
    A coherent segment of a ParsedDocument passed to the extraction rules.

    Chunks are produced deterministically: same document → same chunks.
    Each chunk carries its parent document's source information.
    """
    source_path: str
    source_id: str
    chunk_index: int
    text: str
    start_char: int   # offset into the document's raw_text
    end_char: int

    def to_dict(self) -> dict:
        return {
            'source_path': self.source_path,
            'source_id': self.source_id,
            'chunk_index': self.chunk_index,
            'text': self.text,
            'start_char': self.start_char,
            'end_char': self.end_char,
        }


@dataclass
class CandidateMemoryEvent:
    """
    A candidate memory_event produced by extraction rules.

    Not written to the database unless the operator explicitly commits it.
    All fields that would be required by memory.service.add_memory_event are
    present, plus source attribution fields for review.

    source_span: the exact text range from which this candidate was extracted.
    extraction_method: which rule class produced this candidate.
    committed_id: set to the inserted memory_event.id after --commit.
    """
    event_type: str
    title: str
    summary: str
    evidence: Optional[str]
    source: str               # source_path (attribution)
    confidence: int           # 1–5
    status: str               # 'proposed' or 'unresolved'
    tags: List[str]
    created_by: str           # 'ingestion-pipeline'
    source_span: SourceSpan
    extraction_method: str
    committed_id: Optional[int] = None  # populated after commit

    def __post_init__(self) -> None:
        _validate_event_type(self.event_type)
        _validate_confidence(self.confidence)
        if self.status not in ('proposed', 'unresolved'):
            raise ValidationError(
                f"CandidateMemoryEvent status must be 'proposed' or 'unresolved', "
                f"got {self.status!r}"
            )
        if not self.title or not self.title.strip():
            raise ValidationError("title must not be empty")
        if not self.summary or not self.summary.strip():
            raise ValidationError("summary must not be empty")
        if not self.source or not self.source.strip():
            raise ValidationError("source must not be empty")

    def to_dict(self) -> dict:
        return {
            'event_type': self.event_type,
            'title': self.title,
            'summary': self.summary,
            'evidence': self.evidence,
            'source': self.source,
            'confidence': self.confidence,
            'status': self.status,
            'tags': list(self.tags),
            'created_by': self.created_by,
            'source_span': self.source_span.to_dict(),
            'extraction_method': self.extraction_method,
            'committed_id': self.committed_id,
        }

    @classmethod
    def from_dict(cls, d: dict) -> 'CandidateMemoryEvent':
        return cls(
            event_type=d['event_type'],
            title=d['title'],
            summary=d['summary'],
            evidence=d.get('evidence'),
            source=d['source'],
            confidence=d['confidence'],
            status=d['status'],
            tags=list(d.get('tags', [])),
            created_by=d.get('created_by', 'ingestion-pipeline'),
            source_span=SourceSpan.from_dict(d['source_span']),
            extraction_method=d['extraction_method'],
            committed_id=d.get('committed_id'),
        )


@dataclass
class IngestionResult:
    """
    The complete output of one ingestion run: the parsed document, chunks,
    and all candidate memory events extracted from it.
    """
    document: ParsedDocument
    chunks: List[Chunk]
    candidates: List[CandidateMemoryEvent]
    committed_ids: List[int] = field(default_factory=list)

    @property
    def candidate_count(self) -> int:
        return len(self.candidates)

    @property
    def committed_count(self) -> int:
        return len(self.committed_ids)

    def to_dict(self) -> dict:
        return {
            'document': self.document.to_dict(),
            'chunk_count': len(self.chunks),
            'candidates': [c.to_dict() for c in self.candidates],
            'committed_ids': list(self.committed_ids),
        }

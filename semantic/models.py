"""
Semantic extraction data models.

This module defines the stable data contracts for semantic extraction tasks
and results. No model execution, embeddings, or network access occurs here.

Design principles:
  - Every structure is a plain Python dataclass.
  - Every structure serializes to deterministic JSON via to_dict().
  - Confidence is always explicit (integer 1–5, compatible with memory layer).
  - Provenance is always explicit (extraction_method required; source_id and
    source_span carried through when available).
  - Results are candidates only — they never write directly to memory_events.

Span representation:
  SemanticSpan(start, end) are inclusive-start, exclusive-end character
  offsets into the SemanticTask.input_text. The text can always be recovered
  with input_text[span.start:span.end].
"""
import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional, Tuple

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SEMANTIC_TASK_TYPES = (
    'tagging',
    'polarity_classification',
    'entity_extraction',
    'claim_extraction',
    'relation_extraction',
    'summary_extraction',
    'clustering_hint',
    'memory_candidate_classification',
)

EXTRACTION_POLARITY_VALUES = ('positive', 'negative', 'neutral', 'uncertain')

CONFIDENCE_MIN = 1
CONFIDENCE_MAX = 5

# Approved extraction_method identifiers.
# Future local-model adapters must register under 'local_model:<model_id>'.
EXTRACTION_METHOD_RULE_BASED = 'rule_based'
EXTRACTION_METHOD_KEYWORD    = 'keyword'
EXTRACTION_METHOD_PATTERN    = 'pattern'
EXTRACTION_METHOD_HEURISTIC  = 'heuristic'
EXTRACTION_METHOD_MANUAL     = 'manual'
# Reserved prefix for future local model adapters — not enforced, just documented
EXTRACTION_METHOD_LOCAL_MODEL_PREFIX = 'local_model'


# ---------------------------------------------------------------------------
# SemanticSpan
# ---------------------------------------------------------------------------

@dataclass
class SemanticSpan:
    """
    Inclusive-start, exclusive-end character offsets into a source text.

    Recoverable: source_text[span.start:span.end] == original slice.
    """
    start: int  # inclusive
    end: int    # exclusive

    def __post_init__(self) -> None:
        if not isinstance(self.start, int) or isinstance(self.start, bool):
            raise TypeError(f"SemanticSpan.start must be int, got {type(self.start).__name__}")
        if not isinstance(self.end, int) or isinstance(self.end, bool):
            raise TypeError(f"SemanticSpan.end must be int, got {type(self.end).__name__}")

    def slice_text(self, text: str) -> str:
        return text[self.start:self.end]

    def to_dict(self) -> dict:
        return {'start': self.start, 'end': self.end}

    @classmethod
    def from_dict(cls, d: dict) -> 'SemanticSpan':
        return cls(start=d['start'], end=d['end'])


# ---------------------------------------------------------------------------
# SemanticProvenance
# ---------------------------------------------------------------------------

@dataclass
class SemanticProvenance:
    """
    Explicit attribution for every extraction result.

    extraction_method is always required.
    source_id and source_span are required when the task is source-bound
    (i.e., when SemanticTask.source_id is set).
    model_id is reserved for future local-model adapters.
    """
    extraction_method: str
    source_id: Optional[str] = None
    source_span: Optional[SemanticSpan] = None
    model_id: Optional[str] = None     # future: 'phi3-mini-4k', etc.

    def to_dict(self) -> dict:
        return {
            'extraction_method': self.extraction_method,
            'source_id': self.source_id,
            'source_span': self.source_span.to_dict() if self.source_span else None,
            'model_id': self.model_id,
        }

    @classmethod
    def from_dict(cls, d: dict) -> 'SemanticProvenance':
        span = SemanticSpan.from_dict(d['source_span']) if d.get('source_span') else None
        return cls(
            extraction_method=d['extraction_method'],
            source_id=d.get('source_id'),
            source_span=span,
            model_id=d.get('model_id'),
        )


# ---------------------------------------------------------------------------
# SemanticTask
# ---------------------------------------------------------------------------

@dataclass
class SemanticTask:
    """
    A single semantic extraction request.

    task_id is derived deterministically from (task_type, input_text, source_id).
    Two tasks with identical inputs always produce the same task_id.

    source_id and source_span identify where input_text came from; both
    should be set when the text is a slice of a registered source document.
    """
    task_id: str
    task_type: str
    input_text: str
    source_id: Optional[str] = None
    source_span: Optional[SemanticSpan] = None
    metadata: dict = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: _now())

    @property
    def is_source_bound(self) -> bool:
        return self.source_id is not None

    def to_dict(self) -> dict:
        return {
            'task_id': self.task_id,
            'task_type': self.task_type,
            'input_text': self.input_text,
            'source_id': self.source_id,
            'source_span': self.source_span.to_dict() if self.source_span else None,
            'metadata': self.metadata,
            'created_at': self.created_at,
        }


# ---------------------------------------------------------------------------
# SemanticLabel
# ---------------------------------------------------------------------------

@dataclass
class SemanticLabel:
    """A categorical tag or label assigned to the input text."""
    label: str
    confidence: int          # 1–5
    rationale: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            'label': self.label,
            'confidence': self.confidence,
            'rationale': self.rationale,
        }

    @classmethod
    def from_dict(cls, d: dict) -> 'SemanticLabel':
        return cls(
            label=d['label'],
            confidence=d['confidence'],
            rationale=d.get('rationale'),
        )


# ---------------------------------------------------------------------------
# ExtractedEntity
# ---------------------------------------------------------------------------

@dataclass
class ExtractedEntity:
    """A named entity located in the input text."""
    text: str
    entity_type: str         # e.g. 'person', 'org', 'currency', 'country', 'instrument'
    confidence: int          # 1–5
    span: Optional[SemanticSpan] = None
    rationale: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            'text': self.text,
            'entity_type': self.entity_type,
            'confidence': self.confidence,
            'span': self.span.to_dict() if self.span else None,
            'rationale': self.rationale,
        }

    @classmethod
    def from_dict(cls, d: dict) -> 'ExtractedEntity':
        span = SemanticSpan.from_dict(d['span']) if d.get('span') else None
        return cls(
            text=d['text'],
            entity_type=d['entity_type'],
            confidence=d['confidence'],
            span=span,
            rationale=d.get('rationale'),
        )


# ---------------------------------------------------------------------------
# ExtractedClaim
# ---------------------------------------------------------------------------

@dataclass
class ExtractedClaim:
    """A factual or evaluative claim extracted from the input text."""
    text: str
    polarity: str            # positive / negative / neutral / uncertain
    confidence: int          # 1–5
    span: Optional[SemanticSpan] = None
    rationale: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            'text': self.text,
            'polarity': self.polarity,
            'confidence': self.confidence,
            'span': self.span.to_dict() if self.span else None,
            'rationale': self.rationale,
        }

    @classmethod
    def from_dict(cls, d: dict) -> 'ExtractedClaim':
        span = SemanticSpan.from_dict(d['span']) if d.get('span') else None
        return cls(
            text=d['text'],
            polarity=d['polarity'],
            confidence=d['confidence'],
            span=span,
            rationale=d.get('rationale'),
        )


# ---------------------------------------------------------------------------
# ExtractedRelation
# ---------------------------------------------------------------------------

@dataclass
class ExtractedRelation:
    """A directed relationship between two entities in the input text."""
    subject: str
    predicate: str
    object_: str             # 'object' is a Python builtin
    confidence: int          # 1–5
    span: Optional[SemanticSpan] = None
    rationale: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            'subject': self.subject,
            'predicate': self.predicate,
            'object': self.object_,
            'confidence': self.confidence,
            'span': self.span.to_dict() if self.span else None,
            'rationale': self.rationale,
        }

    @classmethod
    def from_dict(cls, d: dict) -> 'ExtractedRelation':
        span = SemanticSpan.from_dict(d['span']) if d.get('span') else None
        return cls(
            subject=d['subject'],
            predicate=d['predicate'],
            object_=d['object'],
            confidence=d['confidence'],
            span=span,
            rationale=d.get('rationale'),
        )


# ---------------------------------------------------------------------------
# SemanticExtractionResult
# ---------------------------------------------------------------------------

@dataclass
class SemanticExtractionResult:
    """
    The complete output of one semantic extraction task.

    extraction_method is always required.
    provenance carries source attribution and must be explicit.
    overall_confidence summarises the extractor's confidence in the result set.

    Results are candidates only — they are never written directly to
    memory_events. Use semantic.contracts.result_to_candidate() to convert
    to a CandidateMemoryEvent for operator review.
    """
    task_id: str
    task_type: str
    extraction_method: str
    provenance: SemanticProvenance
    overall_confidence: int          # 1–5
    extracted_at: str
    labels: List[SemanticLabel] = field(default_factory=list)
    entities: List[ExtractedEntity] = field(default_factory=list)
    claims: List[ExtractedClaim] = field(default_factory=list)
    relations: List[ExtractedRelation] = field(default_factory=list)
    summary: Optional[str] = None
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            'task_id': self.task_id,
            'task_type': self.task_type,
            'extraction_method': self.extraction_method,
            'provenance': self.provenance.to_dict(),
            'overall_confidence': self.overall_confidence,
            'extracted_at': self.extracted_at,
            'labels': [lb.to_dict() for lb in self.labels],
            'entities': [e.to_dict() for e in self.entities],
            'claims': [c.to_dict() for c in self.claims],
            'relations': [r.to_dict() for r in self.relations],
            'summary': self.summary,
            'metadata': self.metadata,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, ensure_ascii=True)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def derive_task_id(task_type: str, input_text: str, source_id: str = '') -> str:
    """Deterministic task_id: sha256(task_type + NUL + input_text + NUL + source_id)[:16]."""
    raw = f"{task_type}\x00{input_text}\x00{source_id}".encode('utf-8')
    return hashlib.sha256(raw).hexdigest()[:16]

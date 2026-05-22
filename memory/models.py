import json
from dataclasses import dataclass
from typing import List, Optional

VALID_EVENT_TYPES = (
    'architecture_decision', 'governance_rule', 'hypothesis', 'experiment',
    'validation_result', 'adaptation', 'regime_observation', 'implementation_note',
    'open_question', 'rejected_idea', 'incident', 'source_reference',
)

VALID_STATUSES = (
    'proposed', 'accepted', 'rejected', 'superseded',
    'active', 'archived', 'unresolved', 'deprecated',
)

VALID_RELATIONSHIPS = (
    'supports', 'contradicts', 'supersedes', 'refines',
    'derived_from', 'related_to', 'blocks', 'depends_on',
)

REVIEW_STATUSES = ('proposed', 'unresolved', 'active')

CONFIDENCE_MIN = 1
CONFIDENCE_MAX = 5


@dataclass
class MemoryEvent:
    id: int
    event_type: str
    title: str
    summary: str
    evidence: Optional[str]
    source: str
    confidence: int
    status: str
    tags: List[str]
    related_ids: List[int]
    created_by: str
    created_at: str
    updated_at: str
    version: int

    def to_dict(self) -> dict:
        return {
            'id': self.id,
            'event_type': self.event_type,
            'title': self.title,
            'summary': self.summary,
            'evidence': self.evidence,
            'source': self.source,
            'confidence': self.confidence,
            'status': self.status,
            'tags': self.tags,
            'related_ids': self.related_ids,
            'created_by': self.created_by,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
            'version': self.version,
        }

    @classmethod
    def from_row(cls, row) -> 'MemoryEvent':
        return cls(
            id=row['id'],
            event_type=row['event_type'],
            title=row['title'],
            summary=row['summary'],
            evidence=row['evidence'],
            source=row['source'],
            confidence=row['confidence'],
            status=row['status'],
            tags=json.loads(row['tags_json'] or '[]'),
            related_ids=json.loads(row['related_ids_json'] or '[]'),
            created_by=row['created_by'],
            created_at=row['created_at'],
            updated_at=row['updated_at'],
            version=row['version'],
        )


@dataclass
class MemoryRevision:
    id: int
    memory_id: int
    old_value_json: str
    new_value_json: str
    reason: str
    created_at: str
    created_by: str

    def to_dict(self) -> dict:
        return {
            'id': self.id,
            'memory_id': self.memory_id,
            'old_value': json.loads(self.old_value_json),
            'new_value': json.loads(self.new_value_json),
            'reason': self.reason,
            'created_at': self.created_at,
            'created_by': self.created_by,
        }

    @classmethod
    def from_row(cls, row) -> 'MemoryRevision':
        return cls(
            id=row['id'],
            memory_id=row['memory_id'],
            old_value_json=row['old_value_json'],
            new_value_json=row['new_value_json'],
            reason=row['reason'],
            created_at=row['created_at'],
            created_by=row['created_by'],
        )


@dataclass
class MemoryLink:
    id: int
    source_id: int
    target_id: int
    relationship: str
    created_at: str
    created_by: Optional[str] = None
    reason: Optional[str] = None
    link_confidence: Optional[int] = None
    link_metadata_json: Optional[str] = None
    status: str = 'active'
    retracted_at: Optional[str] = None
    retracted_reason: Optional[str] = None
    retracted_by: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            'id': self.id,
            'source_id': self.source_id,
            'target_id': self.target_id,
            'relationship': self.relationship,
            'created_at': self.created_at,
            'created_by': self.created_by,
            'reason': self.reason,
            'link_confidence': self.link_confidence,
            'link_metadata_json': self.link_metadata_json,
            'status': self.status,
            'retracted_at': self.retracted_at,
            'retracted_reason': self.retracted_reason,
            'retracted_by': self.retracted_by,
        }

    @classmethod
    def from_row(cls, row) -> 'MemoryLink':
        def _get(key, default=None):
            try:
                return row[key]
            except IndexError:
                return default

        return cls(
            id=row['id'],
            source_id=row['source_id'],
            target_id=row['target_id'],
            relationship=row['relationship'],
            created_at=row['created_at'],
            created_by=_get('created_by'),
            reason=_get('reason'),
            link_confidence=_get('link_confidence'),
            link_metadata_json=_get('link_metadata_json'),
            status=_get('status', 'active'),
            retracted_at=_get('retracted_at'),
            retracted_reason=_get('retracted_reason'),
            retracted_by=_get('retracted_by'),
        )
